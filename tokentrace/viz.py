"""Inline SVG charts: Sankey flow diagrams and stacked bars.

Everything is computed in Python and emitted as plain SVG so the pages stay
self-contained and work offline.  Colors are CSS custom properties defined by
the page (``--s1`` .. ``--s8``, ``--ink-2``, ``--line``) so light and dark mode
come from the stylesheet, not from here.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

from .attribution import Attribution

SERIES = [f"--s{i}" for i in range(1, 9)]


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _usd(x: float) -> str:
    return f"${x:,.2f}" if x < 1000 else f"${x:,.0f}"


# --------------------------------------------------------------------------- #
# Sankey
# --------------------------------------------------------------------------- #

@dataclass
class SNode:
    id: str
    label: str
    column: int
    color: str = "--s1"
    value: float = 0.0
    y0: float = 0.0
    y1: float = 0.0
    _out_cursor: float = 0.0
    _in_cursor: float = 0.0


@dataclass
class SLink:
    src: str
    dst: str
    value: float
    title: str = ""


@dataclass
class Sankey:
    nodes: dict[str, SNode] = field(default_factory=dict)
    links: list[SLink] = field(default_factory=list)

    def node(self, id: str, label: str, column: int, color: str | None = None) -> SNode:
        if id not in self.nodes:
            idx = sum(1 for n in self.nodes.values() if n.column == column)
            self.nodes[id] = SNode(id, label, column, color or SERIES[idx % len(SERIES)])
        return self.nodes[id]

    def link(self, src: str, dst: str, value: float, title: str = "") -> None:
        if value <= 0:
            return
        for l in self.links:
            if l.src == src and l.dst == dst:
                l.value += value
                return
        self.links.append(SLink(src, dst, value, title))


def render_sankey(sk: Sankey, width: int = 1040, height: int = 440, node_w: int = 14, pad: int = 8,
                  label_w: int = 190, fmt=_usd, aria: str = "Flow diagram") -> str:
    if not sk.links:
        return '<p class="sub">Nothing to draw.</p>'
    inflow: dict[str, float] = {}
    outflow: dict[str, float] = {}
    for l in sk.links:
        outflow[l.src] = outflow.get(l.src, 0.0) + l.value
        inflow[l.dst] = inflow.get(l.dst, 0.0) + l.value
    nodes = [n for n in sk.nodes.values() if n.id in inflow or n.id in outflow]
    for n in nodes:
        n.value = max(inflow.get(n.id, 0.0), outflow.get(n.id, 0.0))
    ncols = max(n.column for n in nodes) + 1
    cols: list[list[SNode]] = [[] for _ in range(ncols)]
    for n in nodes:
        cols[n.column].append(n)
    for c in cols:
        c.sort(key=lambda n: -n.value)

    top, bottom = 10, 10
    usable = height - top - bottom
    scale = min(
        (usable - pad * (len(c) - 1)) / max(sum(n.value for n in c), 1e-9) for c in cols if c
    )
    for c in cols:
        total_h = sum(n.value for n in c) * scale + pad * (len(c) - 1)
        y = top + (usable - total_h) / 2
        for n in c:
            n.y0, n.y1 = y, y + n.value * scale
            n._out_cursor = n._in_cursor = n.y0
            y = n.y1 + pad

    x_left = label_w
    x_right = width - label_w
    def xpos(col: int) -> float:
        return x_left if ncols == 1 else x_left + (x_right - x_left) * col / (ncols - 1)

    # order links at each source by destination y to reduce crossings
    order = {n.id: n.y0 for n in nodes}
    sk.links.sort(key=lambda l: (sk.nodes[l.src].column, order.get(l.src, 0), order.get(l.dst, 0)))

    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
             f'aria-label="{_esc(aria)}" class="sankey">']
    parts.append('<defs><marker id="arrow" markerUnits="userSpaceOnUse" markerWidth="10" markerHeight="10" '
                 'refX="8" refY="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--ink-3)" opacity=".8"/>'
                 '</marker></defs>')
    for l in sk.links:
        s, d = sk.nodes[l.src], sk.nodes[l.dst]
        w = max(l.value * scale, 1.0)
        sy = s._out_cursor + w / 2
        dy = d._in_cursor + w / 2
        s._out_cursor += w
        d._in_cursor += w
        x0 = xpos(s.column) + node_w
        x1 = xpos(d.column) - 12          # leave room for the arrowhead
        cx = (x0 + x1) / 2
        title = l.title or f"{s.label} -> {d.label}: {fmt(l.value)}"
        parts.append(
            f'<path class="ribbon" d="M{x0:.1f},{sy:.1f} C{cx:.1f},{sy:.1f} {cx:.1f},{dy:.1f} {x1:.1f},{dy:.1f}" '
            f'stroke="var({s.color})" stroke-width="{w:.1f}" marker-end="url(#arrow)">'
            f'<title>{_esc(title)}</title></path>'
        )
    for n in nodes:
        x = xpos(n.column)
        parts.append(f'<rect x="{x:.1f}" y="{n.y0:.1f}" width="{node_w}" height="{max(n.y1 - n.y0, 1):.1f}" '
                     f'rx="3" fill="var({n.color})"><title>{_esc(n.label)}: {fmt(n.value)}</title></rect>')
        ty = (n.y0 + n.y1) / 2 + 4
        if n.column == 0:
            parts.append(f'<text x="{x - 8:.1f}" y="{ty:.1f}" text-anchor="end">{_esc(n.label)} '
                         f'<tspan class="muted">{fmt(n.value)}</tspan></text>')
        else:
            parts.append(f'<text x="{x + node_w + 8:.1f}" y="{ty:.1f}">{_esc(n.label)} '
                         f'<tspan class="muted">{fmt(n.value)}</tspan></text>')
    parts.append("</svg>")
    return '<div class="chart">' + "".join(parts) + "</div>"


# --------------------------------------------------------------------------- #
# Box-and-arrow flow diagram (the default; the Sankey above is kept for dense data)
# --------------------------------------------------------------------------- #

def fold(sk: Sankey, max_per_col: int) -> Sankey:
    """Keep the top nodes per column, merge the rest into one 'other' node per column."""
    inflow: dict[str, float] = {}
    outflow: dict[str, float] = {}
    for l in sk.links:
        outflow[l.src] = outflow.get(l.src, 0.0) + l.value
        inflow[l.dst] = inflow.get(l.dst, 0.0) + l.value
    value = {n.id: max(inflow.get(n.id, 0.0), outflow.get(n.id, 0.0)) for n in sk.nodes.values()}
    remap: dict[str, str] = {}
    out = Sankey()
    cols: dict[int, list[SNode]] = {}
    for n in sk.nodes.values():
        if value.get(n.id, 0) > 0:
            cols.setdefault(n.column, []).append(n)
    for col, nodes in cols.items():
        nodes.sort(key=lambda n: -value[n.id])
        keep = nodes if len(nodes) <= max_per_col else nodes[: max_per_col - 1]
        for n in keep:
            out.node(n.id, n.label, n.column, n.color)
            remap[n.id] = n.id
        rest = [n for n in nodes if n not in keep]
        if rest:
            oid = f"other:{col}"
            out.node(oid, f"{len(rest)} others", col, "--ink-3")
            for n in rest:
                remap[n.id] = oid
    for l in sk.links:
        out.link(remap[l.src], remap[l.dst], l.value)
    return out


def render_flow(sk: Sankey, columns: list[str] | None = None, width: int = 1040, box_w: int = 196,
                box_h: int = 46, gap: int = 16, max_per_col: int = 7, fmt=_usd,
                aria: str = "Flow diagram") -> str:
    """Boxes per column, arrows between them.  Arrow width grows with the square
    root of value so one dominant flow does not flatten everything else."""
    if not sk.links:
        return '<p class="sub">Nothing to draw.</p>'
    sk = fold(sk, max_per_col)
    inflow: dict[str, float] = {}
    outflow: dict[str, float] = {}
    for l in sk.links:
        outflow[l.src] = outflow.get(l.src, 0.0) + l.value
        inflow[l.dst] = inflow.get(l.dst, 0.0) + l.value
    nodes = [n for n in sk.nodes.values()]
    for n in nodes:
        n.value = max(inflow.get(n.id, 0.0), outflow.get(n.id, 0.0))
    ncols = max(n.column for n in nodes) + 1
    cols: list[list[SNode]] = [[] for _ in range(ncols)]
    for n in nodes:
        cols[n.column].append(n)
    for c in cols:
        c.sort(key=lambda n: -n.value)

    top = 34 if columns else 12
    height = top + max(len(c) for c in cols) * (box_h + gap) - gap + 12
    padl, padr = 8, 8
    def xpos(col: int) -> float:
        if ncols == 1:
            return padl
        return padl + (width - padl - padr - box_w) * col / (ncols - 1)
    col_max = [max((n.value for n in c), default=1.0) for c in cols]
    for c in cols:
        total_h = len(c) * (box_h + gap) - gap
        y = top + (height - top - 12 - total_h) / 2
        for n in c:
            n.y0, n.y1 = y, y + box_h
            y += box_h + gap

    vmax = max(l.value for l in sk.links)
    # spread arrow endpoints along each box edge, ordered by the other end's position
    outs: dict[str, list[SLink]] = {}
    ins: dict[str, list[SLink]] = {}
    for l in sk.links:
        outs.setdefault(l.src, []).append(l)
        ins.setdefault(l.dst, []).append(l)
    y_out: dict[int, float] = {}
    y_in: dict[int, float] = {}
    for nid, ls in outs.items():
        n = sk.nodes[nid]
        ls.sort(key=lambda l: sk.nodes[l.dst].y0)
        for k, l in enumerate(ls):
            y_out[id(l)] = n.y0 + box_h * (k + 1) / (len(ls) + 1)
    for nid, ls in ins.items():
        n = sk.nodes[nid]
        ls.sort(key=lambda l: sk.nodes[l.src].y0)
        for k, l in enumerate(ls):
            y_in[id(l)] = n.y0 + box_h * (k + 1) / (len(ls) + 1)

    parts = [f'<svg viewBox="0 0 {width} {height:.0f}" width="{width}" height="{height:.0f}" role="img" '
             f'aria-label="{_esc(aria)}" class="flow">',
             '<defs><marker id="flow-arrow" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" '
             'refX="8" refY="4.5" orient="auto"><path d="M0,0 L9,4.5 L0,9 z" fill="var(--ink-2)"/></marker></defs>']
    if columns:
        for i, title in enumerate(columns[:ncols]):
            parts.append(f'<text class="colhead" x="{xpos(i) + box_w / 2:.1f}" y="18" text-anchor="middle">{_esc(title)}</text>')
    for l in sorted(sk.links, key=lambda l: l.value):
        s, d = sk.nodes[l.src], sk.nodes[l.dst]
        w = 1.5 + 11 * (l.value / vmax) ** 0.5
        x0 = xpos(s.column) + box_w
        x1 = xpos(d.column) - 10
        sy, dy = y_out[id(l)], y_in[id(l)]
        cx = (x0 + x1) / 2
        title = l.title or f"{s.label} -> {d.label}: {fmt(l.value)}"
        parts.append(
            f'<path class="arrow" d="M{x0:.1f},{sy:.1f} C{cx:.1f},{sy:.1f} {cx:.1f},{dy:.1f} {x1:.1f},{dy:.1f}" '
            f'stroke="var({s.color})" stroke-width="{w:.1f}" marker-end="url(#flow-arrow)">'
            f'<title>{_esc(title)}</title></path>'
        )
    for c, cmax in zip(cols, col_max):
        for n in c:
            x = xpos(n.column)
            share = n.value / cmax if cmax else 0
            label = n.label if len(n.label) <= 24 else n.label[:22] + "..."
            parts.append(
                f'<g class="box"><rect x="{x:.1f}" y="{n.y0:.1f}" width="{box_w}" height="{box_h}" rx="7"/>'
                f'<rect class="accent" x="{x:.1f}" y="{n.y0:.1f}" width="5" height="{box_h}" rx="2" fill="var({n.color})"/>'
                f'<text class="lbl" x="{x + 14:.1f}" y="{n.y0 + 19:.1f}">{_esc(label)}</text>'
                f'<text class="val" x="{x + 14:.1f}" y="{n.y0 + 36:.1f}">{fmt(n.value)}</text>'
                f'<rect class="share" x="{x + box_w - 70:.1f}" y="{n.y0 + 28:.1f}" width="{max(2, 58 * share):.1f}" '
                f'height="6" rx="3" fill="var({n.color})"/>'
                f'<title>{_esc(n.label)}: {fmt(n.value)}</title></g>'
            )
    parts.append("</svg>")
    return '<div class="chart">' + "".join(parts) + "</div>"


def top_flows(sk: Sankey, limit: int = 12) -> list[dict[str, Any]]:
    total = sum(l.value for l in sk.links if sk.nodes[l.src].column == 0) or 1.0
    rows = sorted(sk.links, key=lambda l: -l.value)[:limit]
    return [{"from": sk.nodes[l.src].label, "to": sk.nodes[l.dst].label, "value": l.value,
             "share": l.value / total} for l in rows]


# --------------------------------------------------------------------------- #
# Ready-made diagrams
# --------------------------------------------------------------------------- #

ACTIVITY = {
    "Read": "Reading", "Grep": "Reading", "Glob": "Reading", "WebFetch": "Reading", "WebSearch": "Reading",
    "ReadMcpResourceTool": "Reading", "NotebookRead": "Reading",
    "Bash": "Running commands", "Monitor": "Running commands",
    "Write": "Editing", "Edit": "Editing", "NotebookEdit": "Editing", "MultiEdit": "Editing",
    "Agent": "Delegating", "Workflow": "Delegating", "SendMessage": "Delegating",
    "Skill": "Loading skills", "ToolSearch": "Loading skills",
}


def activity_of(tool: str | None) -> str:
    if tool is None:
        return "Writing answers"
    return ACTIVITY.get(tool, "Other tools")


def _short(label: str, n: int = 34) -> str:
    label = label.replace("\n", " ")
    return label if len(label) <= n else label[: n - 3] + "..."


def session_sankey(attr: Attribution, max_turns: int = 40, max_tools: int = 40) -> str:
    return render_flow(session_flow(attr, max_turns, max_tools), columns=["Turn", "Tool", "Subagent"],
                       aria="Cost flow from turns to tools to subagents")


def session_flow(attr: Attribution, max_turns: int = 8, max_tools: int = 9) -> Sankey:
    """Turns -> tools (and reasoning) -> subagents, weighted by cost."""
    sk = Sankey()
    turns = [c for c in attr.tree.children if c.kind == "turn"]
    ranked = sorted(turns, key=lambda t: -t.cost_usd)
    keep = {t.id for t in ranked[:max_turns]}
    tool_totals: dict[str, float] = {}
    for t in turns:
        for c in t.children:
            key = c.meta.get("tool") or "answer text" if c.kind == "tool" else "answer text"
            tool_totals[key] = tool_totals.get(key, 0.0) + c.own_cost_usd
    top_tools = {k for k, _ in sorted(tool_totals.items(), key=lambda kv: -kv[1])[:max_tools]}

    for t in turns:
        tid = t.id if t.id in keep else "turn:other"
        label = _short(t.label) if t.id in keep else f"{len(turns) - len(keep)} other turns"
        sk.node(tid, label, 0)
        for c in t.children:
            tool = c.meta.get("tool") if c.kind == "tool" else None
            key = tool if tool in top_tools else ("answer text" if tool is None else "other tools")
            sk.node(f"tool:{key}", key, 1)
            sk.link(tid, f"tool:{key}", c.own_cost_usd)
            for agent in c.children:
                aid = f"agent:{agent.meta.get('agent_id')}"
                desc = agent.meta.get("description") or agent.meta.get("agent_type") or "subagent"
                if len(agent.children) == 0:
                    continue
                sk.node(aid, _short(str(desc)), 2)
                sk.link(f"tool:{key}", aid, agent.cost_usd)
    return sk


def fleet_sankey(by_model_tool: dict[tuple[str, str | None], float], max_tools: int = 40) -> str:
    return render_flow(fleet_flow(by_model_tool, max_tools), columns=["Model", "Activity", "Tool"],
                       aria="Cost flow from models to activities to tools")


def fleet_flow(by_model_tool: dict[tuple[str, str | None], float], max_tools: int = 9) -> Sankey:
    """Models -> activity -> tool, across many sessions, weighted by cost."""
    sk = Sankey()
    tool_totals: dict[str, float] = {}
    for (_, tool), cost in by_model_tool.items():
        tool_totals[tool or "answer text"] = tool_totals.get(tool or "answer text", 0.0) + cost
    top = {k for k, _ in sorted(tool_totals.items(), key=lambda kv: -kv[1])[:max_tools]}
    for (model, tool), cost in sorted(by_model_tool.items(), key=lambda kv: -kv[1]):
        act = activity_of(tool)
        sk.node(f"m:{model}", model, 0)
        sk.node(f"a:{act}", act, 1)
        sk.link(f"m:{model}", f"a:{act}", cost)
        name = tool or "answer text"
        if name not in top:
            name = "other tools"
        sk.node(f"t:{name}", name, 2)
        sk.link(f"a:{act}", f"t:{name}", cost)
    return sk


# --------------------------------------------------------------------------- #
# Stacked bars
# --------------------------------------------------------------------------- #

def stacked_bars(days: list[str], series: dict[str, list[float]], width: int = 1040, height: int = 240,
                 fmt=_usd, aria: str = "Stacked bars") -> str:
    """One bar per day, stacked by series (insertion order), with a legend."""
    if not days:
        return '<p class="sub">No data.</p>'
    padl, padr, padt, padb = 56, 8, 12, 40
    n = len(days)
    totals = [sum(series[s][i] for s in series) for i in range(n)]
    ymax = max(totals) * 1.08 or 1.0
    plot_w = width - padl - padr
    slot = plot_w / n
    bw = max(2.0, slot * 0.72)
    def y(v: float) -> float:
        return padt + (height - padt - padb) * (1 - v / ymax)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="{_esc(aria)}">']
    for k in range(5):
        v = ymax * k / 4
        parts.append(f'<line class="axis" x1="{padl}" x2="{width - padr}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                     f'<text x="{padl - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{fmt(v)}</text>')
    colors = {s: SERIES[i % len(SERIES)] for i, s in enumerate(series)}
    for i, day in enumerate(days):
        x = padl + i * slot + (slot - bw) / 2
        base = 0.0
        for s in series:
            v = series[s][i]
            if v <= 0:
                continue
            y1, y0 = y(base), y(base + v)
            parts.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{max(y1 - y0 - 1, 0.5):.1f}" '
                         f'fill="var({colors[s]})" rx="2"><title>{_esc(day)} {_esc(s)}: {fmt(v)}</title></rect>')
            base += v
        if totals[i] > 0 and (n <= 20 or totals[i] == max(totals)):
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{y(totals[i]) - 4:.1f}" text-anchor="middle">{fmt(totals[i])}</text>')
        step = max(1, n // 10)
        if i % step == 0 or i == n - 1:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{height - padb + 16}" text-anchor="middle">{_esc(day[5:])}</text>')
    parts.append("</svg>")
    legend = "".join(f'<span><i style="background:var({colors[s]})"></i>{_esc(s)}</span>' for s in series)
    return f'<div class="legend">{legend}</div><div class="chart">{"".join(parts)}</div>'


def composition_bar(parts_: dict[str, float], fmt=lambda v: f"{v:,.0f}") -> str:
    """One horizontal stacked bar with a legend, for token mix."""
    total = sum(parts_.values()) or 1.0
    segs = []
    legend = []
    for i, (k, v) in enumerate(parts_.items()):
        if v <= 0:
            continue
        c = SERIES[i % len(SERIES)]
        segs.append(f'<i style="width:{100 * v / total:.2f}%;background:var({c})" title="{_esc(k)}: {fmt(v)} ({v / total:.0%})"></i>')
        legend.append(f'<span><i style="background:var({c})"></i>{_esc(k)} {v / total:.0%}</span>')
    return f'<div class="stack tall">{"".join(segs)}</div><div class="legend">{"".join(legend)}</div>'
