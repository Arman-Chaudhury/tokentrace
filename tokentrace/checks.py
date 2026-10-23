"""Waste detectors, sanity rules, and reconciliation against the source tool's own totals.

Every check returns ``Finding`` records.  Severity:

* ``info``   worth knowing, nothing wrong
* ``warn``   a likely waste pattern or a parser gap
* ``error``  the numbers cannot be trusted (totals disagree, tree inconsistent)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import pricing
from .attribution import Attribution, Node, parse_ts
from .schema import Event, Usage


@dataclass
class Thresholds:
    repeated_read_min: int = 2          # same Read/Grep/Glob/Bash key this many times
    oversized_result_chars: int = 15_000
    cache_rewarm_gap_s: float = 300.0   # 5-minute ephemeral cache TTL
    cache_rewarm_min_tokens: int = 20_000
    context_jump_tokens: int = 30_000
    reconcile_token_tolerance: float = 0.02   # 2 percent
    reconcile_cost_tolerance: float = 0.10    # 10 percent (price table may lag)


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "message": self.message, "data": self.data}


def run_all(events: list[Event], attr: Attribution, th: Thresholds | None = None) -> list[Finding]:
    th = th or Thresholds()
    out: list[Finding] = []
    out += repeated_calls(events, th)
    out += oversized_results(events, attr, th)
    out += cache_rewarms(events, th)
    out += context_jumps(events, th)
    out += compaction_notes(attr)
    out += sanity(events, attr)
    out += reconcile(events, attr, th)
    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: order.get(f.severity, 3))
    return out


# --------------------------------------------------------------------------- #
# Waste patterns
# --------------------------------------------------------------------------- #

def repeated_calls(events: list[Event], th: Thresholds) -> list[Finding]:
    seen: dict[tuple[str, str], list[Event]] = {}
    results = {e.parent_id: e for e in events if e.kind == "tool_result"}
    for e in events:
        if e.kind != "tool_use" or e.tool_name not in ("Read", "Grep", "Glob", "Bash"):
            continue
        key = e.payload.get("dedupe_key")
        if not key:
            continue
        seen.setdefault((e.tool_name, str(key)), []).append(e)
    out = []
    for (tool, key), evs in seen.items():
        if len(evs) < th.repeated_read_min:
            continue
        chars = sum(results[e.id].payload.get("chars", 0) for e in evs if e.id in results)
        wasted = chars - (chars // len(evs))  # everything after the first occurrence
        agents = sorted({e.agent_id or "main" for e in evs})
        out.append(Finding(
            "warn" if wasted > 4000 else "info", "repeated_call",
            f"{tool} on {key[:80]!r} ran {len(evs)} times (~{wasted // 4:,} tokens of repeat results)"
            + (f" across agents {', '.join(agents)}" if len(agents) > 1 else ""),
            {"tool": tool, "key": key, "count": len(evs), "repeat_chars": wasted,
             "est_repeat_tokens": wasted // 4, "agents": agents,
             "turns": sorted({e.turn for e in evs})},
        ))
    out.sort(key=lambda f: f.data["repeat_chars"], reverse=True)
    return out


def oversized_results(events: list[Event], attr: Attribution, th: Thresholds) -> list[Finding]:
    out = []
    for r in attr.top_results:
        if r["chars"] < th.oversized_result_chars:
            continue
        out.append(Finding(
            "warn", "oversized_result",
            f"{r['label']} returned {r['chars']:,} chars (~{r['est_tokens']:,} tokens) into context"
            + (f" of agent {r['agent_id']}" if r["agent_id"] != "main" else ""),
            r,
        ))
    return out


def cache_rewarms(events: list[Event], th: Thresholds) -> list[Finding]:
    """Large cache writes after an idle gap: the 5-minute cache expired and was rebuilt."""
    out = []
    last_by_agent: dict[str | None, Event] = {}
    for e in events:
        if e.kind != "api_call" or e.payload.get("synthetic"):
            continue
        prev = last_by_agent.get(e.agent_id)
        last_by_agent[e.agent_id] = e
        if prev is None:
            continue
        a, b = parse_ts(prev.ts), parse_ts(e.ts)
        if a is None or b is None:
            continue
        gap = (b - a).total_seconds()
        if gap >= th.cache_rewarm_gap_s and e.cache_write_tokens >= th.cache_rewarm_min_tokens:
            p = pricing.lookup(e.model)
            cost = (e.cache_write_tokens * (p.cache_write - p.cache_read) / 1e6) if p else None
            out.append(Finding(
                "info", "cache_rewarm",
                f"{e.cache_write_tokens:,} tokens re-written to cache after a {gap / 60:.0f}-minute gap"
                f" (turn {e.turn}" + (f", agent {e.agent_id}" if e.agent_id else "") + ")"
                + (f", ~${cost:.2f} more than a cache hit" if cost is not None else ""),
                {"ts": e.ts, "gap_s": round(gap), "cache_write_tokens": e.cache_write_tokens,
                 "agent_id": e.agent_id or "main", "turn": e.turn, "extra_cost_usd": cost},
            ))
    return out


def context_jumps(events: list[Event], th: Thresholds) -> list[Finding]:
    """Single-step context growth larger than the threshold on the main thread."""
    out = []
    prev: Event | None = None
    for e in events:
        if e.kind != "api_call" or e.agent_id is not None or e.payload.get("synthetic"):
            continue
        if prev is not None:
            delta = e.context_tokens - prev.context_tokens
            if delta >= th.context_jump_tokens:
                out.append(Finding(
                    "info", "context_jump",
                    f"context grew by {delta:,} tokens in one step at turn {e.turn} "
                    f"({prev.context_tokens:,} -> {e.context_tokens:,})",
                    {"ts": e.ts, "turn": e.turn, "delta": delta, "before": prev.context_tokens,
                     "after": e.context_tokens},
                ))
        prev = e
    return out


def compaction_notes(attr: Attribution) -> list[Finding]:
    return [
        Finding(
            "info", "compaction",
            f"context compacted ({c.get('trigger') or '?'}): {c.get('pre_tokens') or 0:,} -> "
            f"{c.get('post_tokens') or 0:,} tokens at turn {c.get('turn')}",
            c,
        )
        for c in attr.compactions
    ]


# --------------------------------------------------------------------------- #
# Sanity rules over the tree
# --------------------------------------------------------------------------- #

def sanity(events: list[Event], attr: Attribution) -> list[Finding]:
    out = []
    tool_uses = {e.id for e in events if e.kind == "tool_use"}
    results = {e.parent_id for e in events if e.kind == "tool_result"}
    missing = tool_uses - results
    if missing:
        out.append(Finding("info", "tool_use_without_result",
                           f"{len(missing)} tool calls have no recorded result (denied, interrupted, or still running)",
                           {"count": len(missing), "ids": sorted(missing)[:10]}))
    orphan = results - tool_uses - {None, ""}
    if orphan:
        out.append(Finding("warn", "result_without_tool_use",
                           f"{len(orphan)} tool results reference an unknown tool call (parser gap?)",
                           {"count": len(orphan), "ids": sorted(orphan)[:10]}))

    no_usage = [e for e in events if e.kind == "api_call" and not e.payload.get("has_usage")
                and not e.payload.get("synthetic")]
    if no_usage:
        out.append(Finding("warn", "api_call_without_usage",
                           f"{len(no_usage)} API calls carry no usage block; their cost is unknown",
                           {"count": len(no_usage)}))
    errors = [e for e in events if e.kind == "api_call" and e.payload.get("api_error")]
    if errors:
        out.append(Finding("info", "api_errors", f"{len(errors)} API error responses recorded",
                           {"count": len(errors)}))

    unlinked = [n for n in attr.tree.children if n.kind == "agent" and n.meta.get("unlinked")]
    if unlinked:
        out.append(Finding("warn", "unlinked_subagent",
                           f"{len(unlinked)} subagent transcripts could not be linked to a spawning Agent call; "
                           "their cost is counted under the session root",
                           {"agents": [n.meta.get("agent_id") for n in unlinked]}))

    if attr.unknown_models:
        out.append(Finding("warn", "unknown_model_price",
                           f"no price for model(s) {', '.join(attr.unknown_models)}; their cost is counted as $0",
                           {"models": attr.unknown_models}))

    # Tree invariant: inclusive numbers must equal the sum of events.
    tree_total = attr.tree.usage.total
    ev_total = attr.totals.total
    if abs(tree_total - ev_total) > max(50, 0.001 * ev_total):
        out.append(Finding("error", "tree_mismatch",
                           f"attribution tree totals {tree_total:,} tokens but events sum to {ev_total:,}",
                           {"tree": tree_total, "events": ev_total}))
    if abs(attr.tree.cost_usd - attr.total_cost_usd) > 0.01:
        out.append(Finding("error", "tree_cost_mismatch",
                           f"tree cost ${attr.tree.cost_usd:.4f} differs from event cost ${attr.total_cost_usd:.4f}",
                           {"tree": attr.tree.cost_usd, "events": attr.total_cost_usd}))
    _check_children(attr.tree, out)
    return out


def _check_children(node: Node, out: list[Finding]) -> None:
    child_sum = Usage()
    for c in node.children:
        child_sum.merge(c.usage)
        _check_children(c, out)
    if child_sum.total > node.usage.total + 1:
        out.append(Finding("error", "child_exceeds_parent",
                           f"children of {node.label!r} total {child_sum.total:,} tokens, parent {node.usage.total:,}",
                           {"node": node.id}))


# --------------------------------------------------------------------------- #
# Reconciliation against the source's own totals
# --------------------------------------------------------------------------- #

_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")


def _source_usage(mu: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=int(mu.get("inputTokens") or 0),
        cache_read_tokens=int(mu.get("cacheReadInputTokens") or 0),
        cache_write_tokens=int(mu.get("cacheCreationInputTokens") or 0),
        output_tokens=int(mu.get("outputTokens") or 0),
    )


def reconcile(events: list[Event], attr: Attribution, th: Thresholds) -> list[Finding]:
    """Compare parsed usage with the source tool's own running totals.

    Two facts about Claude Code's ``cost-state`` shape the comparison:

    * It only covers the current process lifetime (its ``startTime``), while a
      transcript may span many resumes.  Parsed events are restricted to that
      window before comparing.
    * It keys usage by the model active when the snapshot was written, so a
      session that changed model mid-way lumps everything under one name.  The
      aggregate across models is therefore the primary check; per-model drift
      is reported but downgraded when the aggregate agrees.
    """
    snaps = [e for e in events if e.kind == "cost_snapshot"]
    if not snaps:
        return [Finding("info", "no_source_totals", "source recorded no running totals; nothing to reconcile against")]
    snap = snaps[-1].payload
    source_usage: dict[str, dict[str, Any]] = snap.get("model_usage") or {}
    if not any(_source_usage(mu).total for mu in source_usage.values()):
        return [Finding("info", "no_source_totals",
                        "source running totals are empty (session never finished a turn in its last process?); "
                        "nothing to reconcile against")]
    start_ms = snap.get("start_ms")
    window_start = ""
    if start_ms:
        from datetime import datetime, timezone
        window_start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")
        window_start = window_start.replace("+00:00", "Z")
    # The snapshot is written periodically, so a live or interrupted session has
    # events after it; those cannot be in the source totals.
    window_end = snaps[-1].ts or ""

    ours: dict[str, Usage] = {}
    ours_total = Usage()
    ours_cost = 0.0
    n_window = 0
    n_all = 0
    for e in events:
        if e.kind != "api_call" or e.payload.get("synthetic"):
            continue
        n_all += 1
        if window_start and e.ts and e.ts < window_start:
            continue
        if window_end and e.ts and e.ts > window_end:
            continue
        n_window += 1
        key = pricing.normalize_model(e.model)
        ours.setdefault(key, Usage()).add(e)
        ours_total.add(e)
        c = pricing.cost_usd(e.model, e.input_tokens, e.cache_read_tokens, e.cache_write_tokens, e.output_tokens)
        ours_cost += c or 0.0

    out = []
    theirs_total = Usage()
    theirs_cost = 0.0
    unattributable_cost = 0.0
    for raw_model, mu in source_usage.items():
        theirs = _source_usage(mu)
        cost = float(mu.get("costUSD") or 0.0)
        if pricing.normalize_model(raw_model) in ours:
            theirs_total.merge(theirs)
            theirs_cost += cost
        else:
            unattributable_cost += cost
            out.append(Finding(
                "info", "unattributable_model",
                f"{raw_model}: {theirs.total:,} tokens (${cost:.2f}) appear in the source totals but in no "
                "transcript line (background calls such as title generation)",
                {"model": raw_model, "source": theirs.to_dict(), "cost_usd": cost},
            ))

    # Primary check: aggregate across models inside the snapshot window.
    field_status: dict[str, dict[str, Any]] = {}
    aggregate_ok = True
    for fld in _FIELDS:
        a, b = getattr(ours_total, fld), getattr(theirs_total, fld)
        diff = abs(a - b) / max(b, 1) if (a or b) else 0.0
        field_status[fld] = {"parsed": a, "source": b, "ratio": round(diff, 4),
                             "ok": diff <= th.reconcile_token_tolerance}
    # If the source is a superset of the transcript on every field, the gap is
    # calls Claude Code made on the same model without writing an assistant
    # message: compaction summaries (which re-read the whole context), away
    # summaries, classifier and advisor calls.  Real, billed, not attributable
    # to a step.  Counting MORE than the source is the parser-bug direction and
    # stays an error.
    source_superset = all(
        field_status[f]["source"] >= field_status[f]["parsed"] * (1 - th.reconcile_token_tolerance)
        for f in _FIELDS
    )
    for fld in _FIELDS:
        st = field_status[fld]
        if st["ok"]:
            continue
        a, b, diff = st["parsed"], st["source"], st["ratio"]
        if source_superset and b > a:
            out.append(Finding(
                "warn", "unrecorded_source_usage",
                f"source counts {b - a:,} more {fld.replace('_tokens', '')} tokens than the transcript records "
                "(compaction summaries, away summaries, classifier or advisor calls on the same model); "
                "these are billed but cannot be attributed to a step",
                {"field": fld, "parsed": a, "source": b, "excess": b - a, "scope": "aggregate"},
            ))
            continue
        aggregate_ok = False
        out.append(Finding(
            "warn" if diff < 0.25 else "error", "token_mismatch",
            f"{fld} across models: parsed {a:,} vs source {b:,} ({(a - b) / max(b, 1):+.1%}) "
            "inside the snapshot window",
            {"field": fld, "parsed": a, "source": b, "ratio": diff, "scope": "aggregate"},
        ))

    # Secondary: per-model drift, informational when the aggregate agrees.
    source_keys = {pricing.normalize_model(m) for m in source_usage}
    for raw_model, mu in source_usage.items():
        key = pricing.normalize_model(raw_model)
        mine = ours.get(key)
        if mine is None:
            continue
        theirs = _source_usage(mu)
        for fld in _FIELDS:
            a, b = getattr(mine, fld), getattr(theirs, fld)
            if not (a or b):
                continue
            diff = abs(a - b) / max(b, 1)
            if diff > th.reconcile_token_tolerance and (aggregate_ok or len(ours) > 1):
                out.append(Finding(
                    "info", "model_attribution_drift",
                    f"{raw_model} {fld}: parsed {a:,} vs source {b:,}; the source keys usage by the model "
                    "active at snapshot time, so this is expected after a model change",
                    {"model": raw_model, "field": fld, "parsed": a, "source": b, "ratio": diff},
                ))
    for key, mine in ours.items():
        if key not in source_keys:
            out.append(Finding(
                "info", "model_missing_in_source",
                f"{key}: parsed {mine.total:,} tokens in the window but the source totals never name it "
                "(usually attributed to the model active at snapshot time)",
                {"model": key, "parsed": mine.to_dict()},
            ))

    if theirs_cost:
        diff = abs(ours_cost - theirs_cost) / theirs_cost
        if diff > th.reconcile_cost_tolerance:
            out.append(Finding(
                "warn", "cost_mismatch",
                f"inside the snapshot window our price table gives ${ours_cost:.2f}, source says ${theirs_cost:.2f} "
                f"({diff:.0%} apart; "
                + ("recorded token counts agree or the source is a superset, so the gap is unrecorded "
                   "background calls or pricing.py" if aggregate_ok else "token counts also disagree")
                + f", version {pricing.PRICING_VERSION})",
                {"parsed_cost": ours_cost, "source_cost": theirs_cost, "ratio": diff, "tokens_agree": aggregate_ok},
            ))

    matched = [f for f, s in field_status.items() if s["ok"] and (s["parsed"] or s["source"])]
    out.append(Finding(
        "info", "reconciliation",
        f"snapshot window covers {n_window} of {n_all} API calls"
        + (f" ({window_start[:19]}Z to {window_end[:19]}Z)" if window_start and window_end else "")
        + f": parsed ${ours_cost:.2f} vs source ${theirs_cost:.2f}; "
        + (f"{', '.join(f.replace('_tokens', '') for f in matched)} match within "
           f"{th.reconcile_token_tolerance:.0%}" if matched else "no token field matches")
        + f". Whole session parsed ${attr.total_cost_usd:.2f}; source grand total "
          f"${float(snap.get('total_cost_usd') or 0):.2f} incl. ${unattributable_cost:.2f} unattributable.",
        {"window_start": window_start or None, "window_end": window_end or None,
         "calls_in_window": n_window, "calls_total": n_all,
         "parsed_cost_usd": ours_cost, "source_cost_usd": theirs_cost, "fields": field_status,
         "session_parsed_cost_usd": attr.total_cost_usd, "source_grand_total_usd": snap.get("total_cost_usd"),
         "unattributable_cost_usd": unattributable_cost,
         "source_duration_ms": snap.get("total_duration_ms"), "source_api_duration_ms": snap.get("api_duration_ms"),
         "source_tool_duration_ms": snap.get("tool_duration_ms")},
    ))
    return out
