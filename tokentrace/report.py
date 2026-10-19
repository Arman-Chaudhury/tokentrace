"""Render an Attribution plus Findings as text, JSON, or a self-contained HTML page."""

from __future__ import annotations

import html
import json
from typing import Any

from .attribution import Attribution, Node
from .checks import Finding


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #

def _fmt_tokens(n: int) -> str:
    return f"{n:,}"


def _fmt_usd(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def _fmt_dur(s: float | None) -> str:
    if s is None:
        return "?"
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}m"
    return f"{s / 3600:.1f}h"


def render_text(attr: Attribution, findings: list[Finding], max_rows: int = 12) -> str:
    L: list[str] = []
    t = attr.totals
    L.append(f"tokentrace report  session {attr.session_id}")
    if attr.title:
        L.append(f"title: {attr.title}")
    L.append(f"span: {attr.start_ts or '?'} .. {attr.end_ts or '?'} ({_fmt_dur(attr.duration_s)} wall, "
             f"{_fmt_dur(attr.active_s)} active)   pricing table {attr.pricing_version}")
    L.append("")
    L.append(f"TOTAL  {_fmt_usd(attr.total_cost_usd)}   {_fmt_tokens(t.total)} tokens   "
             f"(input {_fmt_tokens(t.input_tokens)}, cache read {_fmt_tokens(t.cache_read_tokens)}, "
             f"cache write {_fmt_tokens(t.cache_write_tokens)}, output {_fmt_tokens(t.output_tokens)})")
    L.append("")

    L.append("BY MODEL")
    L.append(f"  {'model':<28} {'calls':>6} {'cost':>10} {'tokens':>14} {'output':>10}")
    for m, d in sorted(attr.models.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True):
        flag = "" if d["priced"] else "  (no price)"
        L.append(f"  {m:<28} {d['calls']:>6} {_fmt_usd(d['cost_usd']):>10} "
                 f"{_fmt_tokens(d['usage']['total']):>14} {_fmt_tokens(d['usage']['output_tokens']):>10}{flag}")
    L.append("")

    n_agents = len(attr.agents)
    L.append(f"BY AGENT  ({f'top {max_rows} of {n_agents}; ' if n_agents > max_rows else ''}"
             "inclusive cost rolls subagents into their parent)")
    L.append(f"  {'agent':<46} {'calls':>6} {'tools':>6} {'own cost':>10} {'incl cost':>10} {'time':>6}")
    for a in attr.agents[:max_rows]:
        label = a["label"] if len(a["label"]) <= 46 else a["label"][:43] + "..."
        L.append(f"  {label:<46} {a['calls']:>6} {a['tool_calls']:>6} {_fmt_usd(a['cost_usd']):>10} "
                 f"{_fmt_usd(a['inclusive_cost_usd']):>10} {_fmt_dur(a['duration_s']):>6}")
    rest = attr.agents[max_rows:]
    if rest:
        L.append(f"  ... {len(rest)} more agents, {sum(a['calls'] for a in rest)} calls, "
                 f"{_fmt_usd(sum(a['cost_usd'] for a in rest))} own cost")
    L.append("")

    n_turns = len(attr.turns)
    L.append(f"BY TURN  ({f'top {max_rows} of {n_turns} ' if n_turns > max_rows else ''}by cost, subagents included)")
    L.append(f"  {'turn':<58} {'calls':>6} {'tools':>6} {'cost':>10} {'share':>6}")
    total = attr.total_cost_usd or 1.0
    for tr in sorted(attr.turns, key=lambda d: d["cost_usd"], reverse=True)[:max_rows]:
        label = tr["label"] if len(tr["label"]) <= 58 else tr["label"][:55] + "..."
        L.append(f"  {label:<58} {tr['calls']:>6} {tr['tool_calls']:>6} {_fmt_usd(tr['cost_usd']):>10} "
                 f"{tr['cost_usd'] / total:>6.0%}")
    L.append("")

    L.append("BY TOOL  (issue cost = share of the API calls that emitted the tool call;"
             " incl = plus spawned subagents)")
    L.append(f"  {'tool':<16} {'calls':>6} {'errors':>6} {'result chars':>13} {'~ctx tokens':>12} "
             f"{'issue cost':>11} {'incl cost':>10}")
    for r in attr.tools[:max_rows]:
        L.append(f"  {r['tool']:<16} {r['calls']:>6} {r['errors']:>6} {r['result_chars']:>13,} "
                 f"{r['est_context_tokens']:>12,} {_fmt_usd(r['issue_cost_usd']):>11} "
                 f"{_fmt_usd(r['inclusive_cost_usd']):>10}")
    L.append("")

    L.append("LARGEST TOOL RESULTS")
    for r in attr.top_results[:8]:
        where = f"turn {r['turn']}" + (f", agent {r['agent_id']}" if r["agent_id"] != "main" else "")
        L.append(f"  {r['chars']:>9,} chars  ~{r['est_tokens']:>7,} tok  {r['label']}  ({where})")
    L.append("")

    if attr.compactions:
        L.append("COMPACTIONS")
        for c in attr.compactions:
            L.append(f"  turn {c.get('turn')}: {c.get('pre_tokens') or 0:,} -> {c.get('post_tokens') or 0:,} "
                     f"tokens ({c.get('trigger') or '?'})")
        L.append("")

    L.append(f"FINDINGS ({len(findings)})")
    for f in findings:
        L.append(f"  [{f.severity:<5}] {f.code:<24} {f.message}")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #

def render_json(attr: Attribution, findings: list[Finding]) -> str:
    return json.dumps({"attribution": attr.to_dict(), "findings": [f.to_dict() for f in findings]},
                      indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

_CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --surface-2: #f1f0ec; --line: #dedcd5;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-3: #8a8883;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --warn: #c98500; --err: #e34948; --info: #52514e;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface: #1a1a19; --surface-2: #242423; --line: #3a3936;
    --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #8f8e86;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --warn: #c98500; --err: #e66767; --info: #c3c2b7;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --surface-2: #242423; --line: #3a3936;
  --ink: #f0efec; --ink-2: #c3c2b7; --ink-3: #8f8e86;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --warn: #c98500; --err: #e66767; --info: #c3c2b7;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface); color: var(--ink);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 32px 0 10px; color: var(--ink-2);
  text-transform: uppercase; letter-spacing: .04em; }
.sub { color: var(--ink-2); margin-bottom: 16px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.tile { background: var(--surface-2); border-radius: 8px; padding: 12px 14px; }
.tile .k { color: var(--ink-2); font-size: 12px; } .tile .v { font-size: 22px; font-weight: 600; margin-top: 2px; }
.tile .d { color: var(--ink-3); font-size: 12px; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--ink-2); font-weight: 500; font-size: 12px; } td.n, th.n { text-align: right; }
.bar { position: relative; height: 18px; background: var(--surface-2); border-radius: 4px; min-width: 120px; }
.bar > i { position: absolute; left: 0; top: 0; bottom: 0; background: var(--s1); border-radius: 4px; }
.bar > b { position: absolute; left: 8px; top: 0; line-height: 18px; font-weight: 500; font-size: 12px;
  color: var(--ink); mix-blend-mode: normal; }
.stack { display: flex; height: 14px; border-radius: 4px; overflow: hidden; gap: 2px; background: var(--surface); }
.stack > i { display: block; height: 100%; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; color: var(--ink-2); font-size: 12px; margin: 6px 0 12px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
details { margin-left: 14px; } details > summary { cursor: pointer; padding: 3px 0; list-style: none; }
details > summary::before { content: "\\25B8"; display: inline-block; width: 14px; color: var(--ink-3); }
details[open] > summary::before { content: "\\25BE"; }
.leaf { margin-left: 28px; padding: 2px 0; color: var(--ink-2); }
.node-cost { color: var(--ink-3); font-size: 12px; margin-left: 6px; }
.node-cost .w { display: inline-block; height: 8px; background: var(--s1); border-radius: 2px; vertical-align: middle; margin-right: 6px; }
.err-tag { color: var(--err); } .tree { font-size: 13px; }
.f { padding: 6px 8px; border-left: 3px solid var(--info); margin: 6px 0; background: var(--surface-2); border-radius: 0 6px 6px 0; }
.f.warn { border-color: var(--warn); } .f.error { border-color: var(--err); }
.f code { color: var(--ink-3); font-size: 12px; margin-right: 8px; }
svg text { fill: var(--ink-2); font-size: 11px; } svg .axis { stroke: var(--line); }
svg .ctx { fill: none; stroke: var(--s1); stroke-width: 2; } svg .cmp { stroke: var(--s2); stroke-dasharray: 3 3; }
svg .hit { fill: transparent; } .chart { overflow-x: auto; }
footer { color: var(--ink-3); font-size: 12px; margin-top: 40px; }
"""


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _bar(value: float, maximum: float, label: str) -> str:
    w = 0 if maximum <= 0 else max(0.0, min(100.0, 100.0 * value / maximum))
    return f'<div class="bar"><i style="width:{w:.1f}%"></i><b>{_esc(label)}</b></div>'


def _stack(u: dict[str, int]) -> str:
    total = u.get("total") or 1
    parts = [("input_tokens", "--s1"), ("cache_read_tokens", "--s3"), ("cache_write_tokens", "--s4"),
             ("output_tokens", "--s2")]
    segs = "".join(
        f'<i style="width:{100 * u.get(k, 0) / total:.2f}%;background:var({c})" title="{k.replace("_", " ")}: {u.get(k, 0):,}"></i>'
        for k, c in parts if u.get(k, 0) > 0
    )
    return f'<div class="stack">{segs}</div>'


def _tree(node: Node, total_cost: float, depth: int = 0) -> str:
    share = 0 if total_cost <= 0 else node.cost_usd / total_cost
    cost = (f'<span class="node-cost"><span class="w" style="width:{max(2, 120 * share):.0f}px"></span>'
            f'${node.cost_usd:,.2f} · {node.usage.total:,} tok'
            + (f' · {node.calls} calls' if node.calls else '') + '</span>')
    err = ' <span class="err-tag">error</span>' if node.meta.get("is_error") else ""
    label = _esc(node.label)
    if not node.children:
        extra = f' <span class="node-cost">{node.meta["result_chars"]:,} chars back</span>' \
            if node.meta.get("result_chars") else ""
        return f'<div class="leaf">{label}{err}{cost}{extra}</div>'
    kids = "".join(_tree(c, total_cost, depth + 1) for c in node.children)
    open_attr = " open" if depth < 1 else ""
    return f'<details{open_attr}><summary>{label}{err}{cost}</summary>{kids}</details>'


def _context_svg(series: list[dict[str, Any]], compactions: list[dict[str, Any]]) -> str:
    pts = [s for s in series if s.get("context")]
    if len(pts) < 2:
        return '<p class="sub">Not enough API calls on the main thread to draw context growth.</p>'
    W, H, padl, padb, padt = 1040, 220, 56, 28, 12
    n = len(pts)
    ymax = max(p["context"] for p in pts) * 1.05
    def x(i: int) -> float: return padl + (W - padl - 8) * i / max(1, n - 1)
    def y(v: float) -> float: return padt + (H - padt - padb) * (1 - v / ymax)
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(p['context']):.1f}" for i, p in enumerate(pts))
    ticks = ""
    for k in range(5):
        v = ymax * k / 4
        ticks += (f'<line class="axis" x1="{padl}" x2="{W - 8}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                  f'<text x="{padl - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{int(v / 1000)}k</text>')
    # compaction markers: place at the first call index after the compaction timestamp
    marks = ""
    for c in compactions:
        if c.get("agent_id") != "main":
            continue
        idx = next((i for i, p in enumerate(pts) if p["ts"] >= (c.get("ts") or "")), None)
        if idx is not None:
            marks += (f'<line class="axis cmp" x1="{x(idx):.1f}" x2="{x(idx):.1f}" y1="{padt}" y2="{H - padb}"/>'
                      f'<text x="{x(idx) + 4:.1f}" y="{padt + 10}">compacted</text>')
    hits = "".join(
        f'<rect class="hit" x="{x(i) - 4:.1f}" y="{padt}" width="8" height="{H - padt - padb}">'
        f'<title>call {i + 1} · turn {p["turn"]} · {p["model"]}\ncontext {p["context"]:,} '
        f'(cache read {p["cache_read"]:,}, write {p["cache_write"]:,}, fresh {p["input"]:,})\n'
        f'output {p["output"]:,} · ${p["cost_usd"]:.4f}</title></rect>'
        for i, p in enumerate(pts)
    )
    return (f'<div class="chart"><svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
            f'aria-label="Context size per API call on the main thread">{ticks}'
            f'<path class="ctx" d="{path}"/>{marks}{hits}'
            f'<text x="{padl}" y="{H - 6}">API call 1</text>'
            f'<text x="{W - 8}" y="{H - 6}" text-anchor="end">API call {n}</text></svg></div>')


def render_html(attr: Attribution, findings: list[Finding]) -> str:
    t = attr.totals
    tiles = [
        ("Estimated cost", f"${attr.total_cost_usd:,.2f}", f"price table {attr.pricing_version}"),
        ("Tokens", f"{t.total:,}", f"{t.output_tokens:,} output"),
        ("Cache hit rate", f"{(t.cache_read_tokens / t.context * 100 if t.context else 0):.0f}%",
         f"{t.cache_write_tokens:,} written"),
        ("API calls", f"{sum(m['calls'] for m in attr.models.values()):,}", f"{len(attr.agents) - 1} subagents"),
        ("Active time", _fmt_dur(attr.active_s), f"{_fmt_dur(attr.duration_s)} wall, {len(attr.turns)} turns"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="k">{_esc(k)}</div><div class="v">{_esc(v)}</div>'
                         f'<div class="d">{_esc(d)}</div></div>' for k, v, d in tiles)

    max_agent = max((a["inclusive_cost_usd"] for a in attr.agents), default=0)
    shown_agents = attr.agents[:25]
    agents_rows = "".join(
        f"<tr><td>{_esc(a['label'])}</td><td class='n'>{a['calls']}</td><td class='n'>{a['tool_calls']}</td>"
        f"<td>{_bar(a['inclusive_cost_usd'], max_agent, f'${a['inclusive_cost_usd']:,.2f}')}</td>"
        f"<td class='n'>${a['cost_usd']:,.2f}</td><td class='n'>{_fmt_dur(a['duration_s'])}</td></tr>"
        for a in shown_agents
    )
    rest = attr.agents[25:]
    if rest:
        agents_rows += (f"<tr><td>... {len(rest)} more agents</td><td class='n'>{sum(a['calls'] for a in rest)}</td>"
                        f"<td class='n'>{sum(a['tool_calls'] for a in rest)}</td><td></td>"
                        f"<td class='n'>${sum(a['cost_usd'] for a in rest):,.2f}</td><td></td></tr>")
    max_turn = max((x["cost_usd"] for x in attr.turns), default=0)
    turns_rows = "".join(
        f"<tr><td>{_esc(x['label'])}</td><td class='n'>{x['calls']}</td><td class='n'>{x['tool_calls']}</td>"
        f"<td>{_bar(x['cost_usd'], max_turn, f'${x['cost_usd']:,.2f}')}</td><td>{_stack(x['usage'])}</td></tr>"
        for x in attr.turns
    )
    max_tool = max((x["inclusive_cost_usd"] for x in attr.tools), default=0)
    tools_rows = "".join(
        f"<tr><td>{_esc(x['tool'])}</td><td class='n'>{x['calls']}</td><td class='n'>{x['errors']}</td>"
        f"<td class='n'>{x['result_chars']:,}</td><td class='n'>~{x['est_context_tokens']:,}</td>"
        f"<td class='n'>${x['issue_cost_usd']:,.2f}</td>"
        f"<td>{_bar(x['inclusive_cost_usd'], max_tool, f'${x['inclusive_cost_usd']:,.2f}')}</td></tr>"
        for x in attr.tools
    )
    models_rows = "".join(
        f"<tr><td>{_esc(m)}{'' if d['priced'] else ' <span class=err-tag>(no price)</span>'}</td>"
        f"<td class='n'>{d['calls']}</td><td class='n'>${d['cost_usd']:,.2f}</td>"
        f"<td class='n'>{d['usage']['total']:,}</td><td>{_stack(d['usage'])}</td></tr>"
        for m, d in sorted(attr.models.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)
    )
    results_rows = "".join(
        f"<tr><td>{_esc(r['label'])}</td><td class='n'>{r['chars']:,}</td><td class='n'>~{r['est_tokens']:,}</td>"
        f"<td>turn {r['turn']}{'' if r['agent_id'] == 'main' else ', agent ' + _esc(r['agent_id'])}</td></tr>"
        for r in attr.top_results[:10]
    )
    findings_html = "".join(
        f'<div class="f {f.severity}"><code>{_esc(f.severity)} · {_esc(f.code)}</code>{_esc(f.message)}</div>'
        for f in findings
    ) or '<p class="sub">No findings.</p>'
    legend = ('<div class="legend"><span><i style="background:var(--s1)"></i>fresh input</span>'
              '<span><i style="background:var(--s3)"></i>cache read</span>'
              '<span><i style="background:var(--s4)"></i>cache write</span>'
              '<span><i style="background:var(--s2)"></i>output</span></div>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>tokentrace {_esc(attr.session_id[:8])}</title><style>{_CSS}</style></head>
<body><main>
<h1>{_esc(attr.title or 'Session ' + attr.session_id)}</h1>
<div class="sub">session {_esc(attr.session_id)} · {_esc(attr.start_ts or '?')} to {_esc(attr.end_ts or '?')}</div>
<div class="tiles">{tiles_html}</div>

<h2>Context size per API call (main thread)</h2>
{_context_svg(attr.context_series, attr.compactions)}

<h2>By agent</h2>
<table><thead><tr><th>agent</th><th class="n">calls</th><th class="n">tools</th><th>inclusive cost</th>
<th class="n">own cost</th><th class="n">time</th></tr></thead><tbody>{agents_rows}</tbody></table>

<h2>By turn</h2>{legend}
<table><thead><tr><th>turn</th><th class="n">calls</th><th class="n">tools</th><th>cost (subagents included)</th>
<th>token mix</th></tr></thead><tbody>{turns_rows}</tbody></table>

<h2>By tool</h2>
<table><thead><tr><th>tool</th><th class="n">calls</th><th class="n">errors</th><th class="n">result chars</th>
<th class="n">~context tokens</th><th class="n">issue cost</th><th>inclusive cost</th></tr></thead>
<tbody>{tools_rows}</tbody></table>

<h2>By model</h2>{legend}
<table><thead><tr><th>model</th><th class="n">calls</th><th class="n">cost</th><th class="n">tokens</th><th>token mix</th></tr></thead>
<tbody>{models_rows}</tbody></table>

<h2>Largest tool results</h2>
<table><thead><tr><th>call</th><th class="n">chars</th><th class="n">~tokens</th><th>where</th></tr></thead>
<tbody>{results_rows}</tbody></table>

<h2>Findings ({len(findings)})</h2>
{findings_html}

<h2>Attribution tree</h2>
<div class="tree">{_tree(attr.tree, attr.total_cost_usd)}</div>

<footer>tokentrace · costs are estimates from a versioned list-price table; see the reconciliation finding for the
source tool's own number.</footer>
</main></body></html>
"""
