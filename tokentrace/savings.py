"""Turn findings into concrete ways to spend less, each with an estimated saving.

Every tip answers three questions: what to change, how much it would have
saved in this session, and what evidence the estimate rests on.  Tips come in
two kinds, following the usual order for cost work:

* ``free``      the same work for less money (cache hygiene, smaller tool output)
* ``tradeoff``  cheaper, but it gives something up (fidelity after compaction,
                a less capable model); the report says what

Estimates replay the recorded calls with one thing changed and re-price them.
They assume the model would have made the same calls otherwise, so treat them
as an upper bound on what the change buys, not a promise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import pricing
from .attribution import Attribution
from .checks import Finding
from .schema import Event

# Context size a fresh session starts from when the session itself gives no
# better number (system prompt + tool definitions in Claude Code).
DEFAULT_BASE_CONTEXT = 20_000
# Context left after a /compact, when the session recorded no compaction.
DEFAULT_POST_COMPACT = 30_000
# Summary length written by a compaction call, in output tokens.
COMPACT_SUMMARY_TOKENS = 8_000
# A trimmed tool result: roughly what `| head -100` or a Read with a limit returns.
TRIMMED_RESULT_TOKENS = 1_000

# One step down in price within the current generation, for the model tip.
CHEAPER_MODEL = {
    "claude-fable-5-1": "claude-opus-5-5",
    "claude-mythos-5-1": "claude-opus-5-5",
    "claude-fable-5": "claude-opus-5-5",
    "claude-opus-5": "claude-opus-5-5",
    "claude-opus-4-8": "claude-opus-5-5",
    "claude-opus-4-7": "claude-opus-5-5",
    "claude-opus-4-6": "claude-opus-5-5",
    "claude-opus-5-5": "claude-sonnet-5-5",
}


@dataclass
class Tip:
    code: str
    kind: str                 # "free" or "tradeoff"
    title: str
    saved_usd: float
    fix: str
    evidence: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "kind": self.kind, "title": self.title, "saved_usd": round(self.saved_usd, 4),
                "fix": self.fix, "evidence": self.evidence, "data": self.data}


def advise(events: list[Event], attr: Attribution, findings: list[Finding],
           context_cap: int = 200_000) -> list[Tip]:
    calls = [e for e in events if e.kind == "api_call" and not e.payload.get("synthetic")]
    if not calls:
        return []
    base = _base_context(calls)
    post = _post_compact(attr)
    tips: list[Tip] = []
    for tip in (
        idle_resumes(findings, base),
        cache_breaks(findings),
        oversized_output(events, calls),
        long_context(calls, attr, context_cap, post),
        cheaper_model(calls, attr),
    ):
        if tip is not None and tip.saved_usd >= 0.01:
            tips.append(tip)
    tips.sort(key=lambda t: (t.kind != "free", -t.saved_usd))
    return tips


# --------------------------------------------------------------------------- #
# Free wins
# --------------------------------------------------------------------------- #

def idle_resumes(findings: list[Finding], base: int) -> Tip | None:
    """Coming back after the cache expired rewrites the whole context."""
    rw = [f for f in findings if f.code == "cache_rewarm"]
    if not rw:
        return None
    paid = sum(f.data.get("extra_cost_usd") or 0.0 for f in rw)
    saved = 0.0
    for f in rw:
        rate = _write_rate_for(f)
        if rate is None:
            continue
        saved += max(f.data["rewritten_tokens"] - base, 0) * rate / 1e6
    biggest = max(rw, key=lambda f: f.data["rewritten_tokens"])
    ttl = biggest.data.get("ttl_s") or 300
    return Tip(
        "idle_resume", "free",
        "Clear or compact before a long break",
        saved,
        f"After more than {_fmt_s(ttl)} away the prompt cache has expired, and the first message back rewrites the "
        "whole context at the cache-write price. Before stepping away from a large session, run /compact if you "
        "will continue the same task, or /clear (or a new session with a short handoff note) if you will not.",
        f"{len(rw)} resume(s) after an expired cache rewrote up to {biggest.data['rewritten_tokens']:,} tokens "
        f"(${paid:.2f} more than cache hits). Starting again from a ~{base:,}-token base would have written "
        f"${saved:.2f} less.",
        {"count": len(rw), "extra_cost_usd": paid, "base_context": base},
    )


def cache_breaks(findings: list[Finding]) -> Tip | None:
    """The prefix changed while the cache was warm (model switch, prompt edit)."""
    br = [f for f in findings if f.code == "cache_break" and f.data.get("cause") != "compaction"]
    if not br:
        return None
    saved = sum(f.data.get("extra_cost_usd") or 0.0 for f in br)
    switches = [f for f in br if f.data.get("cause") == "model_switch"]
    edits = len(br) - len(switches)
    parts = []
    if switches:
        parts.append(f"{len(switches)} after a model switch")
    if edits:
        parts.append(f"{edits} after the prompt prefix changed")
    return Tip(
        "cache_break", "free",
        "Keep the model and the prompt prefix fixed mid-session",
        saved,
        "Caches are per model and match the prompt byte for byte from the start. Choose the model when the session "
        "starts instead of switching with /model halfway, and avoid mid-session edits to CLAUDE.md or memory files "
        "and toggling MCP servers or plugins, which change the system prompt or tool list.",
        f"{len(br)} full rewrites while the cache was still warm ({', '.join(parts)}), ${saved:.2f} more than "
        "cache hits. Causes other than a model switch are inferred.",
        {"count": len(br), "model_switches": len(switches)},
    )


def oversized_output(events: list[Event], calls: list[Event]) -> Tip | None:
    """Large tool results stay in context and are paid for on every later call."""
    by_agent: dict[str | None, list[Event]] = {}
    for e in calls:
        by_agent.setdefault(e.agent_id, []).append(e)
    uses = {e.id: e for e in events if e.kind == "tool_use"}
    rows = []
    for r in events:
        if r.kind != "tool_result":
            continue
        tokens = int(r.payload.get("chars") or 0) // 4
        if tokens <= TRIMMED_RESULT_TOKENS * 4:
            continue
        later = [c for c in by_agent.get(r.agent_id, []) if c.ts and r.ts and c.ts > r.ts]
        carried = _calls_until_reset(later)
        if not carried:
            continue
        first = carried[0]
        p = pricing.lookup(first.model)
        if p is None:
            continue
        trimmed = tokens - TRIMMED_RESULT_TOKENS
        w1h = int(first.payload.get("cache_write_1h_tokens") or 0)
        write = p.cache_write_1h if w1h * 2 >= max(first.cache_write_tokens, 1) else p.cache_write
        cost = trimmed * (write + p.cache_read * (len(carried) - 1)) / 1e6
        use = uses.get(r.parent_id)
        rows.append({"tool": use.tool_name if use else "?", "label": _label(use), "tokens": tokens,
                     "carried_calls": len(carried), "saved_usd": cost, "agent_id": r.agent_id or "main"})
    if not rows:
        return None
    rows.sort(key=lambda x: -x["saved_usd"])
    saved = sum(x["saved_usd"] for x in rows)
    top = rows[0]
    return Tip(
        "oversized_output", "free",
        "Trim large tool output before it enters the context",
        saved,
        "A tool result stays in the context and is re-read on every later call until the next compaction. Ask for "
        "less up front: pipe Bash output through head, tail, or grep; give Read an offset and limit; use Grep with "
        "files-only output first; and hand broad searches to a subagent so only its summary comes back.",
        f"{len(rows)} results over {TRIMMED_RESULT_TOKENS * 4:,} tokens. The largest, {top['label']} "
        f"(~{top['tokens']:,} tokens), was carried through {top['carried_calls']} later calls. Cutting each to "
        f"~{TRIMMED_RESULT_TOKENS:,} tokens would have saved about ${saved:.2f}.",
        {"results": rows[:10]},
    )


# --------------------------------------------------------------------------- #
# Tradeoffs
# --------------------------------------------------------------------------- #

def long_context(calls: list[Event], attr: Attribution, cap: int, post: int) -> Tip | None:
    """Replay the main thread with a /compact whenever context passes ``cap``."""
    main = [e for e in calls if e.agent_id is None]
    if not main or max(e.context_tokens for e in main) <= cap:
        return None
    offset = 0           # tokens the simulated session no longer carries
    saved = 0.0
    compactions = 0
    prev_ctx = 0
    for e in main:
        ctx = e.context_tokens
        if ctx < prev_ctx * 0.5:          # a real compaction or /clear already shrank it
            offset = min(offset, max(ctx - post, 0))
        prev_ctx = ctx
        sim = ctx - offset
        p = pricing.lookup(e.model)
        if p is None:
            continue
        if sim > cap:
            compactions += 1
            # one call that reads everything and writes a summary, then the summary is cached
            saved -= (sim * p.cache_read + COMPACT_SUMMARY_TOKENS * p.output + post * p.cache_write_1h) / 1e6
            offset += sim - post
        saved += min(offset, e.cache_read_tokens) * p.cache_read / 1e6
    if compactions == 0 or saved <= 0:
        return None
    peak = max(e.context_tokens for e in main)
    over = sum(1 for e in main if e.context_tokens > cap)
    return Tip(
        "long_context", "tradeoff",
        f"Compact when the context passes {cap // 1000}k tokens",
        saved,
        f"Every call re-reads the whole context, so a long session gets more expensive per step. Run /compact when "
        f"the context passes ~{cap // 1000}k tokens, or start a new session for an unrelated task. The trade-off: "
        "the model keeps a summary instead of the full history.",
        f"The main thread peaked at {peak:,} tokens and {over} calls ran above {cap:,}. Replaying it with "
        f"{compactions} compaction(s) down to ~{post:,} tokens (their own cost subtracted) would have saved "
        f"about ${saved:.2f}.",
        {"peak_context": peak, "calls_over_cap": over, "simulated_compactions": compactions, "cap": cap},
    )


def cheaper_model(calls: list[Event], attr: Attribution) -> Tip | None:
    """Re-price the session one model tier down."""
    actual = 0.0
    alt = 0.0
    moved: dict[str, str] = {}
    for e in calls:
        key = pricing.normalize_model(e.model)
        target = CHEAPER_MODEL.get(key)
        c = pricing.event_cost(e)
        if c is None:
            continue
        actual += c
        if target is None:
            alt += c
            continue
        moved[key] = target
        alt += pricing.cost_usd(target, e.input_tokens, e.cache_read_tokens, e.cache_write_tokens,
                                e.output_tokens, int(e.payload.get("cache_write_1h_tokens") or 0)) or 0.0
    if not moved or actual <= 0:
        return None
    saved = actual - alt
    pairs = ", ".join(f"{a} -> {b}" for a, b in sorted(moved.items()))
    return Tip(
        "cheaper_model", "tradeoff",
        "Use a cheaper model for routine turns",
        saved,
        "Switch with /model at the start of a session that is mostly routine edits, reads, and test runs, and keep "
        "the most capable model for hard design or debugging work. Switch at the start, not midway: a mid-session "
        "switch rewrites the whole context to a new cache.",
        f"The same calls priced as {pairs} would have cost ${alt:.2f} instead of ${actual:.2f} "
        f"({saved / actual:.0%} less), assuming the same token counts. A different model may need more or fewer "
        "turns.",
        {"models": moved, "actual_usd": actual, "alternative_usd": alt},
    )


# --------------------------------------------------------------------------- #
# Cross-session summary
# --------------------------------------------------------------------------- #

def summarize(per_session: list[tuple[str, list[Tip]]]) -> list[dict[str, Any]]:
    """Add up tips by code across sessions, largest saving first."""
    out: dict[str, dict[str, Any]] = {}
    for sid, tips in per_session:
        for t in tips:
            row = out.setdefault(t.code, {"code": t.code, "kind": t.kind, "title": t.title, "fix": t.fix,
                                          "saved_usd": 0.0, "sessions": 0, "top_session": None, "top_usd": 0.0})
            row["saved_usd"] += t.saved_usd
            row["sessions"] += 1
            if t.saved_usd > row["top_usd"]:
                row["top_usd"], row["top_session"] = t.saved_usd, sid
    return sorted(out.values(), key=lambda r: (r["kind"] != "free", -r["saved_usd"]))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _base_context(calls: list[Event]) -> int:
    main = [e for e in calls if e.agent_id is None and e.context_tokens]
    return main[0].context_tokens if main else DEFAULT_BASE_CONTEXT


def _post_compact(attr: Attribution) -> int:
    posts = sorted(int(c.get("post_tokens") or 0) for c in attr.compactions if c.get("post_tokens"))
    return posts[len(posts) // 2] if posts else DEFAULT_POST_COMPACT


def _write_rate_for(f: Finding) -> float | None:
    model = f.data.get("model")
    return pricing.write_rate(model, (f.data.get("ttl_s") or 0) >= 3600)


def _calls_until_reset(later: list[Event]) -> list[Event]:
    """Later calls in one agent, stopping where the context shrank (compaction or /clear)."""
    out: list[Event] = []
    prev = None
    for c in later:
        if prev is not None and c.context_tokens < prev * 0.5:
            break
        out.append(c)
        prev = c.context_tokens
    return out


def _label(use: Event | None) -> str:
    if use is None:
        return "a tool call"
    p = use.payload
    detail = str(p.get("file_path") or p.get("command") or p.get("pattern") or p.get("url") or "").replace("\n", " ")
    if len(detail) > 50:
        detail = detail[:47] + "..."
    return f"{use.tool_name}({detail})" if detail else str(use.tool_name)


def _fmt_s(s: float) -> str:
    return f"{s / 3600:.0f} hour" if s >= 3600 else f"{s / 60:.0f} minutes"
