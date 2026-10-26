from __future__ import annotations

from tokentrace import attribution, cli, dashboard, store, viz
from tokentrace.adapters import claude_code

from conftest import SESSION


def test_sankey_renders_one_ribbon_per_link():
    sk = viz.Sankey()
    sk.node("a", "A", 0)
    sk.node("b", "B", 0)
    sk.node("x", "X", 1)
    sk.node("y", "Y", 1)
    sk.node("z", "Z", 2)
    sk.link("a", "x", 3.0)
    sk.link("a", "y", 1.0)
    sk.link("b", "y", 2.0)
    sk.link("y", "z", 1.5)
    sk.link("a", "x", 1.0)          # merges into the existing a->x ribbon
    svg = viz.render_sankey(sk)
    assert svg.count('class="ribbon"') == 4
    assert svg.count("<rect") == 5
    assert "marker-end" in svg and "A -&gt; X: $4.00" in svg


def test_session_flow_has_turns_tools_and_agent(session_path):
    attr = attribution.build(claude_code.parse_session(session_path))
    svg = viz.session_sankey(attr)
    assert 'class="flow"' in svg and ">Turn<" in svg and ">Subagent<" in svg
    assert "fix the failing test" in svg
    assert ">Read<" in svg and ">Agent<" in svg
    assert "look around" in svg          # the Explore subagent appears in the third column


def test_fold_limits_boxes_per_column():
    sk = viz.Sankey()
    sk.node("m", "model", 0)
    for i in range(10):
        sk.node(f"t{i}", f"tool{i}", 1)
        sk.link("m", f"t{i}", float(10 - i))
    folded = viz.fold(sk, 4)
    col1 = [n for n in folded.nodes.values() if n.column == 1]
    assert len(col1) == 4 and any(n.label == "7 others" for n in col1)
    assert abs(sum(l.value for l in folded.links) - sum(range(1, 11))) < 1e-9
    svg = viz.render_flow(sk, columns=["Model", "Tool"], max_per_col=4)
    assert svg.count('class="box"') == 5 and svg.count('class="arrow"') == 4
    rows = viz.top_flows(folded, 2)
    assert rows[0]["to"] == "7 others" or rows[0]["to"] == "tool0"


def test_stacked_bars_and_composition():
    svg = viz.stacked_bars(["2026-10-01", "2026-10-02"], {"m1": [1.0, 2.0], "m2": [0.5, 0.0]})
    assert svg.count("<rect") == 3 and "m1" in svg
    bar = viz.composition_bar({"a": 75, "b": 25})
    assert "75%" in bar and "25%" in bar


def test_dashboard_build_and_sync(session_path, projects_root, tmp_path):
    conn = store.open_db(tmp_path / "d.db")
    done, skipped = dashboard.sync(conn, projects_root)
    assert (done, skipped) == (1, 0)
    assert dashboard.sync(conn, projects_root) == (0, 1)        # unchanged file is skipped
    index = dashboard.build(conn, tmp_path / "dash")
    page = index.read_text()
    assert "fixture session" in page and 'class="flow"' in page and "Spend per day" in page
    assert 'class="tabs"' in page and 'id="flow"' in page and "Largest flows" in page
    assert (tmp_path / "dash" / "sessions" / f"{SESSION}.html").exists()
    sess_page = (tmp_path / "dash" / "sessions" / f"{SESSION}.html").read_text()
    assert "Where the cost went" in sess_page and 'class="flow"' in sess_page


def test_cli_dashboard(projects_root, tmp_path, capsys):
    rc = cli.main(["--root", str(projects_root), "dashboard", "--db", str(tmp_path / "c.db"),
                   "--out", str(tmp_path / "out"), "--no-session-pages"])
    assert rc == 0
    assert (tmp_path / "out" / "index.html").exists()
    assert "synced 1" in capsys.readouterr().err
