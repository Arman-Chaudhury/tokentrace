"""Rebuild the run as a tree and attribute tokens and cost to each node.

Tree shape::

    session (main agent)
      turn 1 "fix the failing test"
        Bash: pytest -q              share of the api_call that emitted it
        Read: src/foo.py
        Agent: investigate X         -> nested agent node
          turn 1 (the agent prompt)
            Grep: ...
        reasoning                    api_calls in this turn that emitted no tool
      turn 2 ...

Attribution rule: an api_call's tokens and cost are split equally among the
tool_use blocks it emitted.  A call that emitted none goes to the turn's
``reasoning`` leaf.  Subagent cost rolls up into the ``Agent`` tool node that
spawned it, so inclusive numbers at every level add up to the session total.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import pricing
from .schema import Event, Usage


@dataclass
class Node:
    id: str
    kind: str                      # session | agent | turn | tool | reasoning
    label: str
    usage: Usage = field(default_factory=Usage)       # inclusive
    cost_usd: float = 0.0                              # inclusive
    own_usage: Usage = field(default_factory=Usage)   # exclusive of children
    own_cost_usd: float = 0.0
    calls: int = 0                                     # inclusive api_call count
    own_calls: int = 0                                 # api_calls made directly at this node (turns only)
    children: list["Node"] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "label": self.label,
            "usage": self.usage.to_dict(), "cost_usd": round(self.cost_usd, 6),
            "own_usage": self.own_usage.to_dict(), "own_cost_usd": round(self.own_cost_usd, 6),
            "calls": self.calls, "meta": self.meta,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class Attribution:
    session_id: str
    title: str | None
    tree: Node
    totals: Usage
    total_cost_usd: float
    models: dict[str, dict[str, Any]]
    agents: list[dict[str, Any]]
    turns: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    top_results: list[dict[str, Any]]
    context_series: list[dict[str, Any]]
    compactions: list[dict[str, Any]]
    unknown_models: list[str]
    start_ts: str | None
    end_ts: str | None
    duration_s: float | None
    active_s: float | None = None
    pricing_version: str = pricing.PRICING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "title": self.title,
            "totals": self.totals.to_dict(), "total_cost_usd": round(self.total_cost_usd, 6),
            "models": self.models, "agents": self.agents, "turns": self.turns, "tools": self.tools,
            "top_results": self.top_results, "context_series": self.context_series,
            "compactions": self.compactions, "unknown_models": self.unknown_models,
            "start_ts": self.start_ts, "end_ts": self.end_ts, "duration_s": self.duration_s,
            "active_s": self.active_s,
            "pricing_version": self.pricing_version, "tree": self.tree.to_dict(),
        }


# --------------------------------------------------------------------------- #

def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _call_cost(e: Event, unknown: set[str]) -> float:
    c = pricing.cost_usd(e.model, e.input_tokens, e.cache_read_tokens, e.cache_write_tokens, e.output_tokens)
    if c is None:
        unknown.add(e.model or "?")
        return 0.0
    return c


def _tool_label(e: Event) -> str:
    p = e.payload
    name = e.tool_name or "?"
    detail = (
        p.get("file_path") or p.get("command") or p.get("pattern") or p.get("description")
        or p.get("url") or p.get("query") or p.get("skill") or p.get("name") or ""
    )
    detail = str(detail).replace("\n", " ")
    if len(detail) > 70:
        detail = detail[:67] + "..."
    return f"{name}: {detail}" if detail else name


def build(events: list[Event]) -> Attribution:
    session_id = events[0].session_id if events else ""
    unknown: set[str] = set()

    by_agent: dict[str | None, list[Event]] = defaultdict(list)
    results: dict[str, Event] = {}
    title = None
    for e in events:
        if e.kind == "prompt" and e.payload.get("meta_only"):
            title = e.payload.get("title") or title
            continue
        by_agent[e.agent_id].append(e)
        if e.kind == "tool_result" and e.parent_id:
            results[e.parent_id] = e

    call_cost: dict[str, float] = {}
    for e in events:
        if e.kind == "api_call":
            call_cost[e.id] = _call_cost(e, unknown)

    # tool_uses grouped by the api_call that emitted them
    tools_of_call: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        if e.kind == "tool_use" and e.parent_id:
            tools_of_call[e.parent_id].append(e)

    agent_nodes: dict[str | None, Node] = {}
    agent_rows: list[dict[str, Any]] = []
    tool_rows: dict[str, dict[str, Any]] = {}
    built: set[str | None] = set()

    def share_usage(e: Event, n: int) -> Usage:
        u = Usage()
        u.input_tokens = e.input_tokens // n
        u.cache_read_tokens = e.cache_read_tokens // n
        u.cache_write_tokens = e.cache_write_tokens // n
        u.output_tokens = e.output_tokens // n
        return u

    def build_agent(agent_id: str | None) -> Node:
        if agent_id in agent_nodes:
            return agent_nodes[agent_id]
        built.add(agent_id)
        evs = by_agent.get(agent_id, [])
        meta: dict[str, Any] = {}
        for e in evs:
            if e.kind == "prompt" and e.payload.get("agent_meta"):
                meta = e.payload["agent_meta"]
                break
        label = "session" if agent_id is None else (
            f"agent {meta.get('agent_type') or '?'}: {meta.get('description') or agent_id}"
        )
        node = Node(id=f"agent:{agent_id or 'main'}", kind="session" if agent_id is None else "agent",
                    label=label, meta={"agent_id": agent_id, **meta})
        agent_nodes[agent_id] = node

        turns: dict[int, Node] = {}
        order: list[int] = []

        def turn_node(e: Event) -> Node:
            if e.turn not in turns:
                prompt = next((p for p in evs if p.kind == "prompt" and p.turn == e.turn), None)
                text = (prompt.payload.get("text") if prompt else "") or ""
                text = text.replace("\n", " ")
                if len(text) > 80:
                    text = text[:77] + "..."
                turns[e.turn] = Node(id=f"{node.id}:turn:{e.turn}", kind="turn",
                                     label=f"turn {e.turn}: {text}" if text else f"turn {e.turn}",
                                     meta={"turn": e.turn, "start_ts": prompt.ts if prompt else e.ts})
                order.append(e.turn)
            return turns[e.turn]

        for e in evs:
            if e.kind != "api_call":
                continue
            tn = turn_node(e)
            cost = call_cost.get(e.id, 0.0)
            tus = tools_of_call.get(e.id, [])
            tn.own_calls += 1
            if not tus:
                r = next((c for c in tn.children if c.kind == "reasoning"), None)
                if r is None:
                    r = Node(id=f"{tn.id}:reasoning", kind="reasoning", label="reasoning / text only")
                    tn.children.append(r)
                r.own_usage.add(e)
                r.own_cost_usd += cost
                r.meta["calls"] = r.meta.get("calls", 0) + 1
                continue
            n = len(tus)
            for tu in tus:
                res = results.get(tu.id)
                t = Node(id=tu.id, kind="tool", label=_tool_label(tu),
                         meta={"tool": tu.tool_name, "ts": tu.ts,
                               "result_chars": res.payload.get("chars", 0) if res else 0,
                               "is_error": bool(res and res.payload.get("is_error")),
                               "no_result": res is None,
                               "dedupe_key": tu.payload.get("dedupe_key")})
                t.own_usage = share_usage(e, n)
                t.own_cost_usd = cost / n
                for spawned in tu.payload.get("spawned_agent_ids") or []:
                    if spawned not in built:
                        t.children.append(build_agent(spawned))
                if len(t.children) > 1:
                    t.meta["spawned"] = len(t.children)
                tn.children.append(t)
                row = tool_rows.setdefault(tu.tool_name or "?", {
                    "tool": tu.tool_name or "?", "calls": 0, "errors": 0, "result_chars": 0,
                    "issue_cost_usd": 0.0, "inclusive_cost_usd": 0.0, "issue_tokens": 0,
                })
                row["calls"] += 1
                row["errors"] += int(t.meta["is_error"])
                row["result_chars"] += t.meta["result_chars"]
                row["issue_cost_usd"] += t.own_cost_usd
                row["issue_tokens"] += t.own_usage.total

        for i in order:
            node.children.append(turns[i])
        _rollup(node)
        for tn in node.children:
            for t in tn.children:
                if t.kind == "tool":
                    tool_rows[t.meta["tool"] or "?"]["inclusive_cost_usd"] += t.cost_usd

        ts_list = [e.ts for e in evs if e.ts]
        start, end = (min(ts_list), max(ts_list)) if ts_list else (None, None)
        own = Usage()
        own_cost = 0.0
        own_calls = 0
        for e in evs:
            if e.kind == "api_call":
                own.add(e)
                own_cost += call_cost.get(e.id, 0.0)
                own_calls += 1
        agent_rows.append({
            "agent_id": agent_id or "main", "label": label,
            "agent_type": meta.get("agent_type"), "description": meta.get("description"),
            "parent_tool_use": meta.get("parent_tool_use"), "run_id": meta.get("run_id"),
            "calls": own_calls, "usage": own.to_dict(), "cost_usd": round(own_cost, 6),
            "inclusive_cost_usd": round(node.cost_usd, 6),
            "tool_calls": sum(1 for e in evs if e.kind == "tool_use"),
            "start_ts": start, "end_ts": end, "duration_s": _dur(start, end),
        })
        return node

    root = build_agent(None)
    # Subagents whose spawning tool_use could not be found get parked under the root.
    for agent_id in list(by_agent):
        if agent_id is not None and agent_id not in built:
            orphan = build_agent(agent_id)
            orphan.meta["unlinked"] = True
            root.children.append(orphan)
    _rollup(root)
    for row in agent_rows:
        if row["agent_id"] == "main":
            row["inclusive_cost_usd"] = round(root.cost_usd, 6)
    agent_rows.sort(key=lambda r: (r["agent_id"] != "main", -r["inclusive_cost_usd"]))

    # ---- summaries ---------------------------------------------------------
    models: dict[str, dict[str, Any]] = {}
    totals = Usage()
    total_cost = 0.0
    for e in events:
        if e.kind != "api_call":
            continue
        key = e.model or "?"
        m = models.setdefault(key, {"usage": Usage(), "calls": 0, "cost_usd": 0.0})
        m["usage"].add(e)
        m["calls"] += 1
        m["cost_usd"] += call_cost.get(e.id, 0.0)
        totals.add(e)
        total_cost += call_cost.get(e.id, 0.0)
    models_out = {
        k: {"usage": v["usage"].to_dict(), "calls": v["calls"], "cost_usd": round(v["cost_usd"], 6),
            "priced": pricing.lookup(k) is not None}
        for k, v in models.items()
    }

    turns_out = []
    for tn in root.children:
        if tn.kind != "turn":
            continue
        turns_out.append({
            "turn": tn.meta.get("turn"), "label": tn.label, "start_ts": tn.meta.get("start_ts"),
            "calls": tn.calls, "usage": tn.usage.to_dict(), "cost_usd": round(tn.cost_usd, 6),
            "tool_calls": sum(1 for c in tn.children if c.kind == "tool"),
            "subagents": sum(1 for c in tn.children if c.kind == "tool" and c.children),
        })

    tools_out = sorted(
        ({**r, "issue_cost_usd": round(r["issue_cost_usd"], 6),
          "inclusive_cost_usd": round(r["inclusive_cost_usd"], 6),
          "est_context_tokens": r["result_chars"] // 4} for r in tool_rows.values()),
        key=lambda r: r["inclusive_cost_usd"], reverse=True,
    )

    tool_uses = {e.id: e for e in events if e.kind == "tool_use"}
    top_results = sorted(
        (
            {"tool_use_id": r.parent_id, "tool": tool_uses[r.parent_id].tool_name if r.parent_id in tool_uses else "?",
             "label": _tool_label(tool_uses[r.parent_id]) if r.parent_id in tool_uses else "?",
             "chars": r.payload.get("chars", 0), "est_tokens": r.payload.get("chars", 0) // 4,
             "agent_id": r.agent_id or "main", "turn": r.turn, "is_error": r.payload.get("is_error", False)}
            for r in results.values()
        ),
        key=lambda d: d["chars"], reverse=True,
    )[:15]

    series = [
        {"ts": e.ts, "turn": e.turn, "context": e.context_tokens, "cache_read": e.cache_read_tokens,
         "cache_write": e.cache_write_tokens, "input": e.input_tokens, "output": e.output_tokens,
         "model": e.model, "cost_usd": round(call_cost.get(e.id, 0.0), 6)}
        for e in by_agent.get(None, []) if e.kind == "api_call" and not e.payload.get("synthetic")
    ]
    compactions = [
        {"ts": e.ts, "agent_id": e.agent_id or "main", "turn": e.turn, **e.payload}
        for e in events if e.kind == "compaction"
    ]

    ts_all = sorted(e.ts for e in events if e.ts)
    start, end = (ts_all[0], ts_all[-1]) if ts_all else (None, None)

    return Attribution(
        session_id=session_id, title=title, tree=root, totals=totals, total_cost_usd=total_cost,
        models=models_out, agents=agent_rows, turns=turns_out, tools=tools_out, top_results=top_results,
        context_series=series, compactions=compactions, unknown_models=sorted(unknown),
        start_ts=start, end_ts=end, duration_s=_dur(start, end), active_s=_active_seconds(ts_all),
    )


def _active_seconds(sorted_ts: list[str], idle_gap_s: float = 600.0) -> float | None:
    """Wall time excluding idle gaps longer than ``idle_gap_s`` (sessions get resumed days later)."""
    if len(sorted_ts) < 2:
        return 0.0 if sorted_ts else None
    total = 0.0
    prev = parse_ts(sorted_ts[0])
    for ts in sorted_ts[1:]:
        cur = parse_ts(ts)
        if prev is not None and cur is not None:
            gap = (cur - prev).total_seconds()
            if 0 < gap <= idle_gap_s:
                total += gap
        prev = cur
    return round(total, 1)


def _rollup(node: Node) -> None:
    """Make usage/cost/calls inclusive, bottom-up."""
    for c in node.children:
        _rollup(c)
    u = Usage()
    u.merge(node.own_usage)
    cost = node.own_cost_usd
    calls = node.own_calls
    for c in node.children:
        u.merge(c.usage)
        cost += c.cost_usd
        calls += c.calls
    node.usage = u
    node.cost_usd = cost
    node.calls = calls


def _dur(start: str | None, end: str | None) -> float | None:
    a, b = parse_ts(start), parse_ts(end)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 1)
