"""Build a self-contained HTML dashboard over the SQLite store.

One index page (spend per day, flow of cost from models through activities to
tools, token mix, sessions, recurring findings) plus one report page per
session, all static files in one directory.
"""

from __future__ import annotations

import html
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import attribution, checks, pricing, report, store, viz
from .adapters import claude_code
from .schema import Usage


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def sync(conn: sqlite3.Connection, root: Path | str | None = None) -> tuple[int, int]:
    """Ingest new or changed Claude Code sessions.  Returns (ingested, skipped)."""
    known = {s["session_id"]: s["ingested_at"] for s in store.sessions(conn)}
    done = skipped = 0
    for r in claude_code.list_sessions(root or claude_code.DEFAULT_ROOT):
        if r["bytes"] == 0:
            continue
        if r["session_id"] in known and known[r["session_id"]] >= r["modified"]:
            skipped += 1
            continue
        events = claude_code.parse_session(r["path"])
        if not events:
            continue
        attr = attribution.build(events)
        store.ingest(conn, events, path=r["path"], title=attr.title)
        done += 1
    return done, skipped


def build(conn: sqlite3.Connection, out_dir: Path | str, session_limit: int = 60, days: int = 45,
          per_session: bool = True) -> Path:
    out = Path(out_dir)
    (out / "sessions").mkdir(parents=True, exist_ok=True)

    rows = store.sessions(conn)
    sessions: list[dict[str, Any]] = []
    by_model_tool: dict[tuple[str, str | None], float] = defaultdict(float)
    tool_rows: dict[str, dict[str, Any]] = {}
    finding_counts: Counter[str] = Counter()
    rewarm_cost = 0.0
    oversized_tokens = 0
    grand = Usage()
    grand_cost = 0.0
    calls = 0

    for s in rows[:session_limit]:
        events = store.load_events(conn, s["session_id"])
        if not events:
            continue
        attr = attribution.build(events)
        findings = checks.run_all(events, attr)
        warn = sum(1 for f in findings if f.severity in ("warn", "error"))
        for f in findings:
            finding_counts[f.code] += 1
            if f.code == "cache_rewarm":
                rewarm_cost += f.data.get("extra_cost_usd") or 0.0
            if f.code == "oversized_result":
                oversized_tokens += f.data.get("est_tokens") or 0
        grand.merge(attr.totals)
        grand_cost += attr.total_cost_usd
        calls += sum(m["calls"] for m in attr.models.values())

        # model -> tool flows from the tree (tool shares) and reasoning leaves
        for turn in attr.tree.children:
            for c in turn.children:
                if c.kind not in ("tool", "reasoning"):
                    continue
                tool = c.meta.get("tool") if c.kind == "tool" else None
                model = _dominant_model(attr)
                by_model_tool[(model, tool)] += c.own_cost_usd
        for t in attr.tools:
            r = tool_rows.setdefault(t["tool"], {"tool": t["tool"], "calls": 0, "errors": 0, "result_chars": 0,
                                                 "issue_cost_usd": 0.0, "inclusive_cost_usd": 0.0, "sessions": 0})
            r["calls"] += t["calls"]
            r["errors"] += t["errors"]
            r["result_chars"] += t["result_chars"]
            r["issue_cost_usd"] += t["issue_cost_usd"]
            r["inclusive_cost_usd"] += t["inclusive_cost_usd"]
            r["sessions"] += 1

        page = f"sessions/{s['session_id']}.html"
        if per_session:
            (out / page).write_text(report.render_html(attr, findings), encoding="utf-8")
        sessions.append({
            "session_id": s["session_id"], "title": attr.title or s["title"] or s["session_id"][:8],
            "start": (attr.start_ts or "")[:10], "active_s": attr.active_s, "calls": sum(m["calls"] for m in attr.models.values()),
            "tokens": attr.totals.total, "cost_usd": attr.total_cost_usd, "agents": len(attr.agents) - 1,
            "turns": len(attr.turns), "warnings": warn, "page": page if per_session else None,
            "project": Path(s["path"]).parent.name if s.get("path") else "",
        })

    daily = store.daily_totals(conn)
    day_list = sorted({d["day"] for d in daily if d["day"]})[-days:]
    models = [m for m, _ in Counter({d["model"]: 1 for d in daily if d["model"] and d["model"] != "<synthetic>"}).most_common()]
    cost_by_day_model: dict[tuple[str, str], float] = defaultdict(float)
    for d in daily:
        if d["model"] and d["cost_usd"]:
            cost_by_day_model[(d["day"], d["model"])] += d["cost_usd"]
    model_totals = Counter()
    for (day, m), c in cost_by_day_model.items():
        model_totals[m] += c
    models = [m for m, _ in model_totals.most_common()]
    series = {m: [cost_by_day_model.get((day, m), 0.0) for day in day_list] for m in models}

    index = _render_index(sessions, grand, grand_cost, calls, day_list, series, by_model_tool, tool_rows,
                          finding_counts, rewarm_cost, oversized_tokens)
    (out / "index.html").write_text(index, encoding="utf-8")
    return out / "index.html"


def _dominant_model(attr: attribution.Attribution) -> str:
    priced = {m: d for m, d in attr.models.items() if m != "<synthetic>"}
    if not priced:
        return "unknown"
    return max(priced.items(), key=lambda kv: kv[1]["cost_usd"])[0]


def _fmt_dur(s: float | None) -> str:
    return report._fmt_dur(s)


def _render_index(sessions, grand: Usage, grand_cost: float, calls: int, day_list, series, by_model_tool,
                  tool_rows, finding_counts, rewarm_cost, oversized_tokens) -> str:
    active_days = len([d for d in day_list if any(series[m][i] for m in series for i in [day_list.index(d)])])
    tiles = [
        ("Estimated spend", f"${grand_cost:,.2f}", f"{len(sessions)} sessions, price table {pricing.PRICING_VERSION}"),
        ("API calls", f"{calls:,}", f"{grand.total:,} tokens"),
        ("Cache hit rate", f"{(grand.cache_read_tokens / grand.context * 100 if grand.context else 0):.0f}%",
         f"{grand.cache_write_tokens:,} tokens written"),
        ("Idle-gap cache rewrites", f"${rewarm_cost:,.2f}", "extra vs. a cache hit, across sessions"),
        ("Oversized results", f"~{oversized_tokens:,}", "tokens pushed into context in one go"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div>'
                         f'<div class="d">{_esc(d)}</div></div>' for k, v, d in tiles)

    spend_svg = viz.stacked_bars(day_list, series, aria="Estimated spend per day by model")
    flow = viz.fleet_flow(dict(by_model_tool), max_tools=40)   # render_flow folds to seven per column
    flow_svg = viz.render_flow(flow, columns=["Model", "Activity", "Tool"],
                               aria="Cost flow from models to activities to tools")
    flow_rows = "".join(
        f"<tr><td>{_esc(r['from'])}</td><td>{_esc(r['to'])}</td><td class='n'>${r['value']:,.2f}</td>"
        f"<td class='n'>{r['share']:.0%}</td></tr>" for r in viz.top_flows(viz.fold(flow, 7))
    )
    mix = viz.composition_bar({"fresh input": grand.input_tokens, "cache read": grand.cache_read_tokens,
                               "cache write": grand.cache_write_tokens, "output": grand.output_tokens})

    max_cost = max((s["cost_usd"] for s in sessions), default=0)
    sess_rows = "".join(
        f"<tr><td>{'<a href=' + chr(34) + s['page'] + chr(34) + '>' if s['page'] else ''}{_esc(s['title'])}"
        f"{'</a>' if s['page'] else ''}<div class='d'>{_esc(s['project'])}</div></td>"
        f"<td>{_esc(s['start'])}</td><td class='n'>{_fmt_dur(s['active_s'])}</td><td class='n'>{s['turns']}</td>"
        f"<td class='n'>{s['calls']}</td><td class='n'>{s['agents']}</td>"
        f"<td>{report._usd_bar(s['cost_usd'], max_cost)}</td><td class='n'>{s['warnings'] or ''}</td></tr>"
        for s in sorted(sessions, key=lambda s: s["start"], reverse=True)
    )
    tools = sorted(tool_rows.values(), key=lambda r: -r["inclusive_cost_usd"])
    max_tool = max((t["inclusive_cost_usd"] for t in tools), default=0)
    tool_html = "".join(
        f"<tr><td>{_esc(t['tool'])}</td><td class='n'>{t['sessions']}</td><td class='n'>{t['calls']:,}</td>"
        f"<td class='n'>{t['errors']}</td><td class='n'>~{t['result_chars'] // 4:,}</td>"
        f"<td>{report._usd_bar(t['inclusive_cost_usd'], max_tool)}</td></tr>" for t in tools[:14]
    )
    find_html = "".join(f"<tr><td>{_esc(code)}</td><td class='n'>{n}</td></tr>"
                        for code, n in finding_counts.most_common(12))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>tokentrace dashboard</title><style>{report._CSS}</style></head>
<body><main>
<h1>tokentrace dashboard</h1>
<div class="sub">generated {time.strftime('%Y-%m-%d %H:%M')} from the local SQLite store. Costs are list-price estimates.</div>
<div class="tiles">{tiles_html}</div>

<nav class="tabs">
<a href="#overview" data-tab="overview" class="on">Overview</a>
<a href="#flow" data-tab="flow">Flow</a>
<a href="#sessions" data-tab="sessions">Sessions</a>
<a href="#tools" data-tab="tools">Tools and findings</a>
</nav>

<section class="panel on" id="overview">
<h2>Spend per day</h2>
{spend_svg}
<h2>Token mix</h2>
{mix}
</section>

<section class="panel" id="flow">
<h2>Where the money flows</h2>
<p class="sub">Arrow width follows estimated cost. Models on the left, what the model was doing in the middle,
the specific tool on the right. The small bar in each box is its share of its column. Hover an arrow or a box
for the number. Columns keep their seven largest entries and fold the rest into "others".</p>
{flow_svg}
<h2>Largest flows</h2>
<table><thead><tr><th>from</th><th>to</th><th class="n">cost</th><th class="n">share</th></tr></thead>
<tbody>{flow_rows}</tbody></table>
</section>

<section class="panel" id="sessions">
<h2>Sessions</h2>
<table><thead><tr><th>session</th><th>date</th><th class="n">active</th><th class="n">turns</th>
<th class="n">calls</th><th class="n">agents</th><th>estimated cost</th><th class="n">warnings</th></tr></thead>
<tbody>{sess_rows}</tbody></table>
</section>

<section class="panel" id="tools">
<h2>Tools across sessions</h2>
<table><thead><tr><th>tool</th><th class="n">sessions</th><th class="n">calls</th><th class="n">errors</th>
<th class="n">~tokens read back</th><th>inclusive cost</th></tr></thead><tbody>{tool_html}</tbody></table>
<h2>Recurring findings</h2>
<table><thead><tr><th>finding</th><th class="n">sessions</th></tr></thead><tbody>{find_html}</tbody></table>
</section>

<footer>tokentrace. Open a session for its own flow diagram, context chart, and attribution tree.</footer>
</main>
<script>
(function () {{
  var links = document.querySelectorAll('.tabs a'), panels = document.querySelectorAll('.panel');
  function show(name) {{
    links.forEach(function (a) {{ a.classList.toggle('on', a.dataset.tab === name); }});
    panels.forEach(function (p) {{ p.classList.toggle('on', p.id === name); }});
  }}
  links.forEach(function (a) {{ a.addEventListener('click', function (e) {{
    e.preventDefault(); history.replaceState(null, '', '#' + a.dataset.tab); show(a.dataset.tab); }}); }});
  var h = location.hash.replace('#', '');
  if (document.getElementById(h)) show(h);
}})();
</script>
</body></html>
"""
