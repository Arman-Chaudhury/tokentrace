"""Adapter for Claude Code session transcripts.

Layout on disk (``~/.claude/projects/<project-slug>/``)::

    <session-id>.jsonl                      main thread
    <session-id>/subagents/agent-<id>.jsonl one file per subagent
    <session-id>/subagents/agent-<id>.meta.json {"toolUseId": ..., "agentType": ...}
    <session-id>/subagents/workflows/...    workflow-spawned agents, same shape

Facts about the format that this parser depends on (verified against
Claude Code 2.1.216 through 2.1.301 transcripts):

* One API response is written as several ``assistant`` lines that share
  ``message.id`` and repeat the same ``usage`` block.  Usage is counted once
  per message id.
* ``user`` lines whose content is a list of ``tool_result`` blocks are tool
  results; the ``toolUseResult`` field carries structured data (stdout, file
  path, patch, ...).
* ``system`` lines with ``subtype == "compact_boundary"`` mark a compaction;
  ``compactMetadata`` has ``preTokens`` / ``postTokens``.
* ``cost-state`` lines are Claude Code's own running totals for the session.
* A model of ``<synthetic>`` marks locally generated messages, not API calls.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Any, Iterator

from ..schema import Event

SOURCE = "claude_code"
ATTRIBUTION_DEPTH = "steps"
FORMAT_VERSIONS_SEEN = ("2.1.216", "2.1.295", "2.1.296", "2.1.301")

DEFAULT_ROOT = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"


# --------------------------------------------------------------------------- #
# Session discovery
# --------------------------------------------------------------------------- #

def list_sessions(root: Path | str = DEFAULT_ROOT) -> list[dict[str, Any]]:
    """Return every main-thread transcript under ``root`` with light metadata."""
    root = Path(root)
    out: list[dict[str, Any]] = []
    for p in sorted(root.glob("*/*.jsonl")):
        if p.parent.name == "subagents":
            continue
        meta = _peek_metadata(p)
        st = p.stat()
        out.append({
            "session_id": p.stem,
            "path": str(p),
            "project": p.parent.name,
            "bytes": st.st_size,
            "modified": st.st_mtime,
            "title": meta.get("title"),
            "subagents": len(_subagent_files(p)),
        })
    out.sort(key=lambda d: d["modified"], reverse=True)
    return out


def resolve_session(target: str, root: Path | str = DEFAULT_ROOT) -> Path:
    """Accept a file path, a directory, a session id (or prefix), or ``latest``."""
    p = Path(target).expanduser()
    if p.is_file():
        return p
    if p.is_dir():
        cands = sorted(p.glob("*.jsonl"), key=lambda q: q.stat().st_mtime, reverse=True)
        if cands:
            return cands[0]
        raise FileNotFoundError(f"no .jsonl transcripts in {p}")
    sessions = list_sessions(root)
    if target == "latest":
        if not sessions:
            raise FileNotFoundError(f"no sessions under {root}")
        return Path(sessions[0]["path"])
    matches = [s for s in sessions if s["session_id"].startswith(target)]
    if len(matches) == 1:
        return Path(matches[0]["path"])
    if not matches:
        raise FileNotFoundError(f"no session matching {target!r} under {root}")
    raise ValueError(f"{len(matches)} sessions match {target!r}; give more characters")


def _peek_metadata(path: Path) -> dict[str, Any]:
    """Cheap scan for title and version without building events."""
    title = None
    version = None
    for line in _iter_lines(path):
        t = line.get("type")
        if t == "ai-title":
            title = line.get("aiTitle")
        elif t == "agent-name" and not title:
            title = line.get("agentName")
        if version is None and line.get("version"):
            version = line["version"]
    return {"title": title, "version": version}


def _subagent_files(main_path: Path) -> list[Path]:
    sub = main_path.parent / main_path.stem / "subagents"
    if not sub.is_dir():
        return []
    return sorted(Path(p) for p in glob.glob(str(sub / "**" / "agent-*.jsonl"), recursive=True))


def _iter_lines(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                yield d


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def parse_session(path: Path | str) -> list[Event]:
    """Parse a main transcript plus all of its subagent transcripts."""
    main = Path(path)
    session_id = main.stem
    events: list[Event] = []
    events.extend(_parse_file(main, session_id, agent_id=None, parent_tool_use=None))

    # Workflow runs record their run id in the Workflow tool's result; their agents
    # live under subagents/workflows/<run_id>/ and carry no toolUseId of their own.
    run_to_tool_use: dict[str, str] = {}
    for e in events:
        rid = e.payload.get("run_id") if e.kind == "tool_result" else None
        if rid and rid not in run_to_tool_use and e.parent_id:
            run_to_tool_use[rid] = e.parent_id

    for sub in _subagent_files(main):
        agent_id = sub.stem.removeprefix("agent-")
        meta_path = sub.with_name(sub.stem + ".meta.json")
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        parent_tool_use = meta.get("toolUseId")
        run_id = None
        if not parent_tool_use and sub.parent.parent.name == "workflows":
            run_id = sub.parent.name
            parent_tool_use = run_to_tool_use.get(run_id)
        sub_events = _parse_file(sub, session_id, agent_id=agent_id, parent_tool_use=parent_tool_use)
        for e in sub_events:
            if e.kind == "prompt":
                e.payload["agent_meta"] = {
                    "agent_type": meta.get("agentType"),
                    "description": meta.get("description"),
                    "spawn_depth": meta.get("spawnDepth"),
                    "parent_tool_use": parent_tool_use,
                    "run_id": run_id,
                }
                break
        events.extend(sub_events)

    # Link Agent / Workflow tool_use events to the subagent(s) they spawned.
    by_parent_tool: dict[str, list[str]] = {}
    for e in events:
        meta = e.payload.get("agent_meta") if e.kind == "prompt" else None
        if e.agent_id and meta and meta.get("parent_tool_use"):
            by_parent_tool.setdefault(meta["parent_tool_use"], []).append(e.agent_id)
    for e in events:
        if e.kind == "tool_use" and e.id in by_parent_tool:
            e.payload["spawned_agent_ids"] = by_parent_tool[e.id]

    events.sort(key=lambda e: (e.ts or "9999", e.kind != "prompt"))
    return events


def _parse_file(path: Path, session_id: str, agent_id: str | None, parent_tool_use: str | None) -> list[Event]:
    events: list[Event] = []
    seen_msg_ids: set[str] = set()
    turn = 0
    last_ts = ""
    prefix = f"{agent_id}:" if agent_id else ""
    first = True

    for d in _iter_lines(path):
        t = d.get("type")
        ts = d.get("timestamp") or last_ts
        if d.get("timestamp"):
            last_ts = d["timestamp"]
        msg = d.get("message") or {}
        content = msg.get("content")

        if t == "user":
            if _is_tool_result(content):
                for block in content:
                    if block.get("type") != "tool_result":
                        continue
                    tu = block.get("tool_use_id") or ""
                    text = _result_text(block.get("content"))
                    payload: dict[str, Any] = {
                        "chars": len(text),
                        "is_error": bool(block.get("is_error")),
                    }
                    tur = d.get("toolUseResult")
                    if isinstance(tur, dict):
                        fp = tur.get("filePath") or (tur.get("file") or {}).get("filePath")
                        if fp:
                            payload["file_path"] = fp
                        if "stdout" in tur:
                            payload["chars"] = len(tur.get("stdout") or "") + len(tur.get("stderr") or "")
                        if tur.get("backgroundTaskId"):
                            payload["background"] = True
                        if tur.get("runId"):
                            payload["run_id"] = tur["runId"]
                    events.append(Event(
                        id=f"res:{tu}", session_id=session_id, source=SOURCE, kind="tool_result",
                        ts=ts, agent_id=agent_id, turn=turn, parent_id=tu, payload=payload,
                    ))
                continue
            if d.get("isMeta"):
                continue
            text = _prompt_text(content)
            is_compact = bool(d.get("isCompactSummary"))
            if not is_compact:
                turn += 1
            payload = {"text": text[:200], "chars": len(text), "compact_summary": is_compact}
            events.append(Event(
                id=f"{prefix}prompt:{d.get('uuid') or len(events)}", session_id=session_id, source=SOURCE,
                kind="prompt", ts=ts, agent_id=agent_id, turn=turn,
                parent_id=parent_tool_use if first else None, payload=payload,
            ))
            first = False

        elif t == "assistant":
            mid = msg.get("id") or d.get("uuid") or f"line{len(events)}"
            model = msg.get("model")
            call_id = f"{prefix}call:{mid}"
            if mid not in seen_msg_ids:
                seen_msg_ids.add(mid)
                u = msg.get("usage") or {}
                events.append(Event(
                    id=call_id, session_id=session_id, source=SOURCE, kind="api_call", ts=ts,
                    agent_id=agent_id, turn=turn, model=model,
                    input_tokens=int(u.get("input_tokens") or 0),
                    cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
                    cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
                    output_tokens=int(u.get("output_tokens") or 0),
                    payload={
                        "stop_reason": msg.get("stop_reason"),
                        "request_id": d.get("requestId"),
                        "effort": d.get("effort"),
                        "synthetic": model == "<synthetic>",
                        "api_error": bool(d.get("isApiErrorMessage")),
                        "has_usage": bool(u),
                    },
                ))
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    events.append(Event(
                        id=block.get("id") or f"{call_id}:tu{len(events)}", session_id=session_id,
                        source=SOURCE, kind="tool_use", ts=ts, agent_id=agent_id, turn=turn,
                        parent_id=call_id, model=model, tool_name=block.get("name"),
                        payload=_summarize_input(block.get("name"), block.get("input") or {}),
                    ))

        elif t == "system" and d.get("subtype") == "compact_boundary":
            cm = d.get("compactMetadata") or {}
            events.append(Event(
                id=f"{prefix}compact:{d.get('uuid') or len(events)}", session_id=session_id, source=SOURCE,
                kind="compaction", ts=ts, agent_id=agent_id, turn=turn,
                payload={
                    "trigger": cm.get("trigger"),
                    "pre_tokens": cm.get("preTokens"),
                    "post_tokens": cm.get("postTokens"),
                    "dropped_tokens": cm.get("cumulativeDroppedTokens"),
                },
            ))

        elif t == "cost-state" and agent_id is None:
            events.append(Event(
                id=f"cost:{len(events)}", session_id=session_id, source=SOURCE, kind="cost_snapshot",
                ts=ts, agent_id=None, turn=turn,
                payload={
                    "total_cost_usd": d.get("totalCostUSD"),
                    "model_usage": d.get("modelUsage") or {},
                    "start_ms": d.get("startTime"),
                    "total_duration_ms": d.get("totalDuration"),
                    "api_duration_ms": d.get("totalAPIDuration"),
                    "tool_duration_ms": d.get("totalToolDuration"),
                    "has_unknown_model_cost": d.get("hasUnknownModelCost"),
                },
            ))

        elif t == "ai-title" and agent_id is None:
            # Titles are rewritten as the session goes on; keep one event with the latest.
            existing = next((e for e in events if e.id == "meta:title"), None)
            if existing is not None:
                existing.payload["title"] = d.get("aiTitle")
            else:
                events.append(Event(
                    id="meta:title", session_id=session_id, source=SOURCE, kind="prompt", ts=ts,
                    turn=turn, payload={"title": d.get("aiTitle"), "meta_only": True},
                ))

    return events


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _is_tool_result(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _prompt_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[image]")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content)


_KEEP_KEYS = {
    "Read": ("file_path", "offset", "limit"),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "Grep": ("pattern", "path", "glob"),
    "Glob": ("pattern", "path"),
    "Bash": ("command", "description", "run_in_background"),
    "Agent": ("description", "subagent_type", "model"),
    "Workflow": ("name", "scriptPath"),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Skill": ("skill",),
}


def _summarize_input(name: str | None, inp: dict[str, Any]) -> dict[str, Any]:
    keys = _KEEP_KEYS.get(name or "", ())
    out: dict[str, Any] = {}
    for k in keys:
        if k in inp:
            v = inp[k]
            out[k] = v[:300] if isinstance(v, str) else v
    out["input_chars"] = len(json.dumps(inp, ensure_ascii=False))
    # A stable key for "the same call again" detection.
    if name == "Read":
        out["dedupe_key"] = inp.get("file_path")
    elif name in ("Grep", "Glob"):
        out["dedupe_key"] = f"{inp.get('pattern')}@{inp.get('path') or ''}"
    elif name == "Bash":
        out["dedupe_key"] = (inp.get("command") or "").strip()
    return out
