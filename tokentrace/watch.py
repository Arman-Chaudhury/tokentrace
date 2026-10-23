"""Follow a live session and print cost per turn as it happens.

The transcript is re-parsed whenever any of its files change.  Parsing a
multi-megabyte session takes well under a second, so polling is simpler and
more robust than an incremental parser that must track partial lines.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from . import attribution, checks
from .adapters import claude_code
from .schema import Event

# Findings worth interrupting a live view with; reconciliation noise is not.
LIVE_FINDINGS = ("oversized_result", "cache_rewarm", "compaction", "repeated_call", "context_jump")


@dataclass
class Snapshot:
    session_id: str
    title: str | None
    total_cost_usd: float
    calls: int
    turns: dict[int, dict[str, Any]]          # turn -> {cost_usd, calls, label}
    last_context: int                          # context tokens of the last main-thread call
    last_cache_hit: float                      # cache_read / context of the last call
    last_ts: str
    finding_keys: set[str] = field(default_factory=set)
    findings: list[checks.Finding] = field(default_factory=list)


def take_snapshot(events: list[Event]) -> Snapshot:
    attr = attribution.build(events)
    th = checks.Thresholds()
    found = (checks.repeated_calls(events, th) + checks.oversized_results(events, attr, th)
             + checks.cache_rewarms(events, th) + checks.context_jumps(events, th)
             + checks.compaction_notes(attr))
    turns = {t["turn"]: {"cost_usd": t["cost_usd"], "calls": t["calls"], "label": t["label"]} for t in attr.turns}
    last = attr.context_series[-1] if attr.context_series else None
    ctx = last["context"] if last else 0
    hit = (last["cache_read"] / ctx) if last and ctx else 0.0
    last_call_ts = max((e.ts for e in events if e.kind == "api_call" and e.ts), default="")
    return Snapshot(
        session_id=attr.session_id, title=attr.title, total_cost_usd=attr.total_cost_usd,
        calls=sum(m["calls"] for m in attr.models.values()), turns=turns, last_context=ctx,
        last_cache_hit=hit, last_ts=last_call_ts,
        finding_keys={_finding_key(f) for f in found}, findings=found,
    )


def _finding_key(f: checks.Finding) -> str:
    d = f.data
    ident = {k: d.get(k) for k in ("tool", "key", "count", "tool_use_id", "ts", "turn", "pre_tokens") if k in d}
    return f.code + ":" + json.dumps(ident, sort_keys=True)


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def _clock(ts: str) -> str:
    return ts[11:19] if len(ts) >= 19 else "--:--:--"


def header(s: Snapshot) -> str:
    title = f" {s.title}" if s.title else ""
    return (f"watching {s.session_id[:8]}{title}: {len(s.turns)} turns, {s.calls} calls, "
            f"${s.total_cost_usd:,.2f} so far. Ctrl-C to stop.")


def diff_lines(prev: Snapshot | None, cur: Snapshot) -> list[str]:
    """Human lines describing what changed between two snapshots."""
    lines: list[str] = []
    if prev is None or cur.calls == prev.calls and cur.total_cost_usd == prev.total_cost_usd:
        new_calls = 0
    else:
        new_calls = cur.calls - prev.calls
    if prev is not None and new_calls > 0:
        delta = cur.total_cost_usd - prev.total_cost_usd
        current_turn = max(cur.turns) if cur.turns else 0
        turn = cur.turns.get(current_turn, {"cost_usd": 0.0, "calls": 0, "label": ""})
        label = turn["label"].split(": ", 1)[-1] if ": " in turn["label"] else ""
        label = f'  "{label[:40]}"' if label else ""
        lines.append(
            f"{_clock(cur.last_ts)}  turn {current_turn}  +{new_calls} call{'s' if new_calls != 1 else ''}  "
            f"+${delta:,.2f}  turn ${turn['cost_usd']:,.2f}  ctx {_fmt_k(cur.last_context)} "
            f"(cache {cur.last_cache_hit:.0%})  session ${cur.total_cost_usd:,.2f}{label}"
        )
    seen = prev.finding_keys if prev is not None else set()
    for f in cur.findings:
        if _finding_key(f) not in seen:
            lines.append(f"          ! {f.code}: {f.message}")
    return lines


def _mtime(main: Path) -> float:
    latest = main.stat().st_mtime
    for p in claude_code._subagent_files(main):
        try:
            latest = max(latest, p.stat().st_mtime)
        except FileNotFoundError:
            pass
    return latest


def watch(target: str, root: Path | str | None = None, interval: float = 2.0, once: bool = False,
          out: TextIO | None = None) -> int:
    out = out or sys.stdout
    main = claude_code.resolve_session(target, root or claude_code.DEFAULT_ROOT)
    events = claude_code.parse_session(main)
    cur = take_snapshot(events)
    print(header(cur), file=out, flush=True)
    for f in cur.findings[-3:]:
        print(f"          ! {f.code}: {f.message}", file=out, flush=True)
    if once:
        return 0
    last_mtime = _mtime(main)
    try:
        while True:
            time.sleep(interval)
            m = _mtime(main)
            if m == last_mtime:
                continue
            last_mtime = m
            try:
                events = claude_code.parse_session(main)
            except OSError:
                continue
            nxt = take_snapshot(events)
            for line in diff_lines(cur, nxt):
                print(line, file=out, flush=True)
            cur = nxt
    except KeyboardInterrupt:
        print(f"\nstopped: {len(cur.turns)} turns, {cur.calls} calls, ${cur.total_cost_usd:,.2f}", file=out)
        return 0
