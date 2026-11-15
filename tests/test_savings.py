"""1-hour cache pricing, TTL-aware cache checks, and the savings tips."""

from __future__ import annotations

import json
from pathlib import Path

from tokentrace import attribution, checks, cli, pricing, savings, store
from tokentrace.adapters import claude_code
from tokentrace.schema import Event

from conftest import MODEL, SESSION, _assistant, _user_text


def _call(i: int, minute: float, read: int, write: int, w1h: int | None = None, out: int = 100,
          model: str = MODEL, agent: str | None = None) -> Event:
    total_min = int(minute)
    ts = f"2026-10-15T{12 + total_min // 60:02d}:{total_min % 60:02d}:00.000Z"
    payload = {} if w1h is None else {"cache_write_1h_tokens": w1h}
    return Event(id=f"c{i}", session_id="s", source="test", kind="api_call", ts=ts, agent_id=agent, model=model,
                 input_tokens=5, cache_read_tokens=read, cache_write_tokens=write, output_tokens=out,
                 payload=payload)


# ---- pricing ---------------------------------------------------------------

def test_one_hour_writes_cost_twice_input():
    p = pricing.lookup(MODEL)
    assert p.cache_write_1h == p.input * 2 and p.cache_write == p.input * 1.25
    five = pricing.cost_usd(MODEL, 0, 0, 1_000_000, 0)
    hour = pricing.cost_usd(MODEL, 0, 0, 1_000_000, 0, cache_write_1h=1_000_000)
    assert five == p.cache_write and hour == p.cache_write_1h
    # the 1h share can never exceed the total write
    assert pricing.cost_usd(MODEL, 0, 0, 10, 0, cache_write_1h=999) == pricing.cost_usd(MODEL, 0, 0, 10, 0, 10)
    e = _call(0, 0, 0, 1_000_000, w1h=1_000_000, out=0)
    assert abs(pricing.event_cost(e) - (5 * p.input / 1e6 + p.cache_write_1h)) < 1e-9


def test_adapter_reads_one_hour_split(tmp_path: Path):
    proj = tmp_path / "-p"
    proj.mkdir()
    usage = {"input_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 300,
             "output_tokens": 2, "cache_creation": {"ephemeral_1h_input_tokens": 200, "ephemeral_5m_input_tokens": 100}}
    lines = [_user_text("2026-10-15T12:00:00.000Z", "hi", "u1"),
             _assistant("2026-10-15T12:00:01.000Z", "m1", [{"type": "text", "text": "ok"}], usage, "a1")]
    path = proj / f"{SESSION}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    call = next(e for e in claude_code.parse_session(path) if e.kind == "api_call")
    assert call.cache_write_tokens == 300 and call.payload["cache_write_1h_tokens"] == 200


def test_daily_totals_price_the_split(tmp_path: Path):
    conn = store.open_db(tmp_path / "t.db")
    store.ingest(conn, [_call(0, 0, 0, 1_000_000, w1h=1_000_000, out=0)])
    row = store.daily_totals(conn)[0]
    assert row["cache_write_1h_tokens"] == 1_000_000
    assert abs(row["cost_usd"] - pricing.event_cost(_call(0, 0, 0, 1_000_000, w1h=1_000_000, out=0))) < 1e-9


# ---- TTL-aware cache checks --------------------------------------------------

def _codes(events):
    return [f for f in checks.cache_rewarms(events, checks.Thresholds())]


def test_one_hour_cache_survives_a_thirty_minute_gap():
    events = [_call(0, 0, 0, 100_000, w1h=100_000), _call(1, 30, 100_000, 500, w1h=500)]
    assert _codes(events) == []


def test_expiry_after_the_ttl_is_a_rewarm():
    events = [_call(0, 0, 0, 100_000, w1h=100_000), _call(1, 90, 0, 101_000, w1h=101_000)]
    [f] = _codes(events)
    assert f.code == "cache_rewarm" and f.data["ttl_s"] == 3600 and f.data["rewritten_tokens"] == 100_005
    p = pricing.lookup(MODEL)
    assert abs(f.data["extra_cost_usd"] - 100_005 * (p.cache_write_1h - p.cache_read) / 1e6) < 1e-9


def test_five_minute_writes_expire_after_five_minutes():
    events = [_call(0, 0, 0, 50_000, w1h=0), _call(1, 10, 0, 50_500, w1h=0)]
    [f] = _codes(events)
    assert f.code == "cache_rewarm" and f.data["ttl_s"] == 300


def test_warm_rewrite_after_model_switch_is_a_break():
    events = [_call(0, 0, 0, 80_000, w1h=80_000),
              _call(1, 2, 0, 81_000, w1h=81_000, model="claude-sonnet-5-5")]
    [f] = _codes(events)
    assert f.code == "cache_break" and f.data["cause"] == "model_switch"


def test_new_tool_output_is_not_a_rewrite():
    # previous prefix read back in full; the large write is new content, not a rewarm
    events = [_call(0, 0, 0, 40_000, w1h=40_000), _call(1, 90, 40_005, 60_000, w1h=60_000)]
    assert _codes(events) == []


def test_compaction_is_labelled_as_such():
    comp = Event(id="k", session_id="s", source="test", kind="compaction", ts="2026-10-15T12:01:00.000Z")
    events = [_call(0, 0, 0, 80_000, w1h=80_000), comp, _call(1, 2, 0, 30_000, w1h=30_000)]
    [f] = _codes(events)
    assert f.code == "cache_break" and f.data["cause"] == "compaction"
    assert savings.cache_breaks([f]) is None       # expected cost, not something to fix


# ---- savings tips ------------------------------------------------------------

def test_fixture_session_gets_ranked_tips(session_path):
    events = claude_code.parse_session(session_path)
    attr = attribution.build(events)
    tips = savings.advise(events, attr, checks.run_all(events, attr))
    codes = [t.code for t in tips]
    assert "idle_resume" in codes and "cheaper_model" in codes
    kinds = [t.kind for t in tips]
    assert kinds == sorted(kinds, key=lambda k: k != "free")         # free wins first
    model_tip = next(t for t in tips if t.code == "cheaper_model")
    assert model_tip.data["models"] == {MODEL: "claude-sonnet-5-5"}
    assert 0 < model_tip.saved_usd < model_tip.data["actual_usd"]
    assert all(t.fix and t.evidence and t.saved_usd >= 0.01 for t in tips)


def test_long_context_replay_saves_after_subtracting_compactions():
    # a main thread that climbs to 600k tokens, one call per minute
    events, ctx = [], 20_000
    for i in range(80):
        events.append(_call(i, i, ctx, 7_000, w1h=7_000))
        ctx += 7_000
    attr = attribution.build(events)
    tip = savings.long_context(events, attr, cap=200_000, post=30_000)
    assert tip is not None and tip.kind == "tradeoff"
    assert tip.data["simulated_compactions"] >= 2 and tip.saved_usd > 0
    assert savings.long_context(events, attr, cap=10_000_000, post=30_000) is None


def test_oversized_output_counts_calls_that_carry_it():
    use = Event(id="tu", session_id="s", source="test", kind="tool_use", ts="2026-10-15T12:00:00.000Z",
                tool_name="Bash", parent_id="c0", payload={"command": "cat big.log"})
    res = Event(id="res:tu", session_id="s", source="test", kind="tool_result", ts="2026-10-15T12:00:30.000Z",
                parent_id="tu", payload={"chars": 80_000})
    calls = [_call(0, 0, 0, 10_000, w1h=10_000)] + [_call(i, i, 30_000, 100, w1h=100) for i in range(1, 11)]
    tip = savings.oversized_output([calls[0], use, res] + calls[1:], calls)
    row = tip.data["results"][0]
    assert row["tool"] == "Bash" and row["carried_calls"] == 10 and row["tokens"] == 20_000
    p = pricing.lookup(MODEL)
    assert abs(tip.saved_usd - 19_000 * (p.cache_write_1h + 9 * p.cache_read) / 1e6) < 1e-9


def test_summarize_adds_up_by_code():
    t = savings.Tip("idle_resume", "free", "x", 2.0, "fix", "why")
    rows = savings.summarize([("a", [t]), ("b", [savings.Tip("idle_resume", "free", "x", 3.0, "fix", "why")])])
    assert rows[0]["saved_usd"] == 5.0 and rows[0]["sessions"] == 2 and rows[0]["top_session"] == "b"


def test_cli_savings(projects_root, tmp_path, capsys):
    assert cli.main(["--root", str(projects_root), "savings", "latest"]) == 0
    assert "HOW TO SPEND LESS" in capsys.readouterr().out
    assert cli.main(["--root", str(projects_root), "savings", "latest", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session_id"] == SESSION and data["tips"]
    assert cli.main(["--root", str(projects_root), "savings", "--all", "--db", str(tmp_path / "s.db")]) == 0
    assert "Free wins found" in capsys.readouterr().out
    out = tmp_path / "r.html"
    assert cli.main(["--root", str(projects_root), "report", "latest", "--format", "html", "--out", str(out)]) == 0
    assert "How to spend less" in out.read_text()


def test_sync_reingests_rows_from_an_older_price_table(projects_root, tmp_path):
    from tokentrace import dashboard
    conn = store.open_db(tmp_path / "v.db")
    assert dashboard.sync(conn, projects_root) == (1, 0)
    assert dashboard.sync(conn, projects_root) == (0, 1)
    conn.execute("UPDATE sessions SET pricing_version = 'old'")
    assert dashboard.sync(conn, projects_root) == (1, 0)
