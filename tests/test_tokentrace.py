from __future__ import annotations

import json
from datetime import datetime, timezone

from tokentrace import attribution, checks, cli, pricing, report, store
from tokentrace.adapters import claude_code
from tokentrace.schema import Event, Usage

from conftest import AGENT, MODEL, SESSION, WF_AGENT, fill_cost_state


# ---- adapter ---------------------------------------------------------------

def test_parse_dedupes_split_messages(session_path):
    events = claude_code.parse_session(session_path)
    calls = [e for e in events if e.kind == "api_call" and e.agent_id is None]
    ids = [e.id for e in calls]
    assert ids.count("call:msg_1") == 1
    msg1 = next(e for e in calls if e.id == "call:msg_1")
    assert (msg1.input_tokens, msg1.cache_read_tokens, msg1.cache_write_tokens, msg1.output_tokens) == (10, 1000, 500, 50)


def test_parse_links_subagent_to_agent_tool_use(session_path):
    events = claude_code.parse_session(session_path)
    agent_tu = next(e for e in events if e.kind == "tool_use" and e.id == "tu_agent")
    assert agent_tu.payload["spawned_agent_ids"] == [AGENT]
    wf_tu = next(e for e in events if e.kind == "tool_use" and e.id == "tu_wf")
    assert wf_tu.payload["spawned_agent_ids"] == [WF_AGENT]
    wf_prompt = next(e for e in events if e.kind == "prompt" and e.agent_id == WF_AGENT)
    assert wf_prompt.payload["agent_meta"]["run_id"] == "wf_test-1"
    assert wf_prompt.parent_id == "tu_wf"
    sub_prompt = next(e for e in events if e.kind == "prompt" and e.agent_id == AGENT)
    assert sub_prompt.payload["agent_meta"]["agent_type"] == "Explore"
    assert sub_prompt.parent_id == "tu_agent"


def test_parse_turns_compaction_and_results(session_path):
    events = claude_code.parse_session(session_path)
    prompts = [e for e in events if e.kind == "prompt" and e.agent_id is None and not e.payload.get("meta_only")]
    assert [p.turn for p in prompts] == [1, 2, 2]          # compact summary does not start a new turn
    assert prompts[2].payload["compact_summary"] is True
    comp = [e for e in events if e.kind == "compaction"]
    assert len(comp) == 1 and comp[0].payload["pre_tokens"] == 26000
    bash_res = next(e for e in events if e.kind == "tool_result" and e.parent_id == "tu_bash")
    assert bash_res.payload["chars"] == 20000              # stdout length wins over the empty content
    read_res = next(e for e in events if e.kind == "tool_result" and e.parent_id == "tu_read1")
    assert read_res.payload["file_path"] == "/p/a.py"


def test_list_and_resolve_sessions(projects_root, session_path):
    rows = claude_code.list_sessions(projects_root)
    assert len(rows) == 1 and rows[0]["title"] == "fixture session" and rows[0]["subagents"] == 2
    assert claude_code.resolve_session("latest", projects_root) == session_path
    assert claude_code.resolve_session(SESSION[:8], projects_root) == session_path
    assert claude_code.resolve_session(str(session_path), projects_root) == session_path


# ---- pricing ---------------------------------------------------------------

def test_pricing_lookup_normalizes_suffixes():
    assert pricing.lookup("claude-opus-5-5[1m]") is pricing.PRICES["claude-opus-5-5"]
    assert pricing.lookup("claude-haiku-4-5-20251001") is pricing.PRICES["claude-haiku-4-5"]
    assert pricing.lookup("<synthetic>").input == 0
    assert pricing.lookup("gpt-9") is None
    assert pricing.cost_usd("claude-opus-5-5", 1_000_000, 0, 0, 0) == 4.0
    assert pricing.cost_usd("claude-opus-5-5", 0, 0, 0, 1_000_000) == 20.0


# ---- attribution -----------------------------------------------------------

def test_attribution_tree_adds_up(session_path):
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    assert attr.title == "fixture session"
    assert attr.tree.usage.total == attr.totals.total
    assert abs(attr.tree.cost_usd - attr.total_cost_usd) < 1e-9
    assert "<synthetic>" in attr.models and attr.models["<synthetic>"]["cost_usd"] == 0

    turns = [c for c in attr.tree.children if c.kind == "turn"]
    assert [t.meta["turn"] for t in turns] == [1, 2]
    assert not [c for c in attr.tree.children if c.kind == "agent"]   # nothing unlinked
    turn1 = turns[0]
    tools = [c for c in turn1.children if c.kind == "tool"]
    assert [t.meta["tool"] for t in tools] == ["Read", "Read", "Agent"]
    agent_tool = tools[2]
    assert agent_tool.children and agent_tool.children[0].kind == "agent"
    # the Agent tool node's inclusive cost = its share of msg_2 + the whole subagent
    sub = agent_tool.children[0]
    assert sub.calls == 2
    assert abs(agent_tool.cost_usd - (agent_tool.own_cost_usd + sub.cost_usd)) < 1e-9
    # turn call count: 3 own + 2 inside the subagent
    assert turn1.calls == 5
    reasoning = [c for c in turn1.children if c.kind == "reasoning"]
    assert len(reasoning) == 1 and reasoning[0].own_usage.output_tokens == 30
    wf_tool = next(c for c in turns[1].children if c.kind == "tool" and c.meta["tool"] == "Workflow")
    assert wf_tool.children[0].meta["agent_id"] == WF_AGENT

    agents = {a["agent_id"]: a for a in attr.agents}
    assert set(agents) == {"main", AGENT, WF_AGENT}
    assert attr.agents[0]["agent_id"] == "main"
    assert agents["main"]["cost_usd"] > 0
    assert abs(agents["main"]["inclusive_cost_usd"] - attr.total_cost_usd) < 1e-9
    assert abs(agents["main"]["cost_usd"] + agents[AGENT]["cost_usd"] + agents[WF_AGENT]["cost_usd"]
               - attr.total_cost_usd) < 1e-9
    tools_by_name = {t["tool"]: t for t in attr.tools}
    assert tools_by_name["Agent"]["inclusive_cost_usd"] > tools_by_name["Agent"]["issue_cost_usd"]
    assert tools_by_name["Read"]["calls"] == 2
    assert attr.context_series[0]["context"] == 1510
    assert attr.active_s is not None and attr.active_s < attr.duration_s


# ---- checks ----------------------------------------------------------------

def test_checks_detect_waste_patterns(session_path):
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    findings = checks.run_all(events, attr)
    codes = {f.code for f in findings}
    assert "repeated_call" in codes
    rep = next(f for f in findings if f.code == "repeated_call")
    assert rep.data["key"] == "/p/a.py" and rep.data["count"] == 2
    assert "oversized_result" in codes
    assert "cache_rewarm" in codes
    rw = next(f for f in findings if f.code == "cache_rewarm")
    assert rw.data["cache_write_tokens"] == 25000 and rw.data["gap_s"] >= 300
    assert "compaction" in codes
    assert not [f for f in findings if f.severity == "error"]


def test_reconcile_matches_when_source_agrees(session_path):
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    u = attr.models[MODEL]["usage"]
    fill_cost_state(session_path, {
        "claude-opus-5-5[1m]": {"inputTokens": u["input_tokens"], "outputTokens": u["output_tokens"],
                                "cacheReadInputTokens": u["cache_read_tokens"],
                                "cacheCreationInputTokens": u["cache_write_tokens"],
                                "costUSD": attr.models[MODEL]["cost_usd"]},
        "claude-haiku-5-5": {"inputTokens": 500, "outputTokens": 20, "cacheReadInputTokens": 0,
                             "cacheCreationInputTokens": 0, "costUSD": 0.0001},
    }, attr.total_cost_usd + 0.0001)
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    findings = checks.reconcile(events, attr, checks.Thresholds())
    codes = [f.code for f in findings]
    assert "token_mismatch" not in codes and "cost_mismatch" not in codes
    assert "unattributable_model" in codes
    rec = next(f for f in findings if f.code == "reconciliation")
    assert abs(rec.data["parsed_cost_usd"] - attr.total_cost_usd) < 1e-6
    assert all(v["ok"] for v in rec.data["fields"].values())


def test_reconcile_flags_token_mismatch(session_path):
    fill_cost_state(session_path, {
        "claude-opus-5-5": {"inputTokens": 999_999, "outputTokens": 1, "cacheReadInputTokens": 1,
                            "cacheCreationInputTokens": 1, "costUSD": 5.0},
    }, 5.0)
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    findings = checks.reconcile(events, attr, checks.Thresholds())
    mism = [f for f in findings if f.code == "token_mismatch"]
    assert mism and any(f.severity == "error" for f in mism)


def test_reconcile_restricts_to_snapshot_window(session_path):
    """Only calls at or after the snapshot's startTime are compared; usage keyed by one model name is fine."""
    events = claude_code.parse_session(session_path)
    # window starts at turn 2 (12:30); the main-thread calls after it are msg_4 and msg_5
    late = [e for e in events if e.kind == "api_call" and e.ts >= "2026-10-15T12:30:00.000Z"
            and not e.payload.get("synthetic")]
    u = Usage()
    for e in late:
        u.add(e)
    fill_cost_state(session_path, {
        "claude-opus-5-5[1m]": {"inputTokens": u.input_tokens, "outputTokens": u.output_tokens,
                                "cacheReadInputTokens": u.cache_read_tokens,
                                "cacheCreationInputTokens": u.cache_write_tokens, "costUSD": 0.0},
    }, 0.0, start_ms=int(datetime(2026, 10, 15, 12, 30, tzinfo=timezone.utc).timestamp() * 1000))
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    findings = checks.reconcile(events, attr, checks.Thresholds())
    assert not [f for f in findings if f.code == "token_mismatch"]
    rec = next(f for f in findings if f.code == "reconciliation")
    assert rec.data["calls_in_window"] == len(late) and rec.data["calls_total"] > len(late)


# ---- store -----------------------------------------------------------------

def test_store_roundtrip_and_totals(session_path, tmp_path):
    events = claude_code.parse_session(session_path)
    conn = store.open_db(tmp_path / "t.db")
    n = store.ingest(conn, events, path=str(session_path), title="fixture")
    assert n == len(events)
    back = store.load_events(conn, SESSION)
    assert len(back) == len(events)
    assert {e.id for e in back} == {e.id for e in events}
    assert attribution.build(back).total_cost_usd == attribution.build(events).total_cost_usd
    # re-ingest replaces rather than duplicates
    assert store.ingest(conn, events) == n
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == n
    totals = store.daily_totals(conn)
    assert any(r["model"] == MODEL and r["day"] == "2026-10-15" for r in totals)


def test_event_roundtrip_dict():
    e = Event(id="x", session_id="s", source="t", kind="api_call", ts="", model="m", output_tokens=3,
              payload={"a": 1})
    assert Event.from_dict(e.to_dict()) == e


# ---- reports and cli -------------------------------------------------------

def test_renderers_produce_output(session_path):
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    findings = checks.run_all(events, attr)
    txt = report.render_text(attr, findings)
    assert "BY AGENT" in txt and "repeated_call" in txt
    js = json.loads(report.render_json(attr, findings))
    assert js["attribution"]["tree"]["kind"] == "session"
    page = report.render_html(attr, findings)
    assert "<svg" in page and "Attribution tree" in page and "prefers-color-scheme" in page


def test_cli_report_json_and_check(session_path, projects_root, tmp_path, capsys):
    out = tmp_path / "r.json"
    rc = cli.main(["--root", str(projects_root), "report", "latest", "--format", "json", "--out", str(out),
                   "--db", str(tmp_path / "c.db")])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["attribution"]["session_id"] == SESSION
    assert cli.main(["--root", str(projects_root), "check", SESSION[:6]]) == 0
    captured = capsys.readouterr()
    assert "repeated_call" in captured.out
    assert cli.main(["--root", str(projects_root), "sessions"]) == 0
    assert "fixture session" in capsys.readouterr().out
    assert cli.main(["--root", str(projects_root), "totals", "--db", str(tmp_path / "c.db")]) == 0
    assert cli.main(["--root", str(projects_root), "report", "nope"]) == 2
