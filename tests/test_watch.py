from __future__ import annotations

import io
import json

from tokentrace import cli, watch
from tokentrace.adapters import claude_code

from conftest import SESSION


def test_snapshot_and_diff_report_new_turn(session_path):
    events = claude_code.parse_session(session_path)
    full = watch.take_snapshot(events)
    assert full.session_id == SESSION and set(full.turns) == {1, 2}
    assert full.calls > 0 and full.total_cost_usd > 0
    assert full.last_context > 0

    # Pretend we had only seen turn 1: everything at or after the turn-2 prompt is new.
    cutoff = "2026-10-15T12:30:00.000Z"
    earlier = [e for e in events if (e.ts or "") < cutoff]
    partial = watch.take_snapshot(earlier)
    assert set(partial.turns) == {1}

    lines = watch.diff_lines(partial, full)
    assert lines, "a new turn must produce output"
    assert lines[0].startswith("12:3") and "turn 2" in lines[0] and "session $" in lines[0]
    codes = [l.split("! ", 1)[1].split(":")[0] for l in lines if "! " in l]
    assert "oversized_result" in codes and "cache_rewarm" in codes and "compaction" in codes
    # nothing new -> nothing printed
    assert watch.diff_lines(full, full) == []


def test_header_mentions_title_and_cost(session_path):
    snap = watch.take_snapshot(claude_code.parse_session(session_path))
    h = watch.header(snap)
    assert "fixture session" in h and "turns" in h and "$" in h


def test_cli_watch_once(projects_root, capsys):
    assert cli.main(["--root", str(projects_root), "watch", "--once"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("watching") and "fixture session" in out


def test_watch_loop_picks_up_appended_lines(session_path, monkeypatch, projects_root):
    """Drive the polling loop: append a new API call to the transcript, then interrupt."""
    buf = io.StringIO()
    ticks = {"n": 0}

    def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 1:
            with open(session_path, "a") as fh:
                fh.write(json.dumps({
                    "type": "assistant", "uuid": "late", "timestamp": "2026-10-15T13:00:00.000Z",
                    "sessionId": SESSION,
                    "message": {"id": "msg_late", "model": "claude-opus-5-5", "role": "assistant",
                                "content": [{"type": "text", "text": "late"}],
                                "usage": {"input_tokens": 10, "cache_read_input_tokens": 50000,
                                          "cache_creation_input_tokens": 0, "output_tokens": 500}},
                }) + "\n")
            # make sure the mtime moves even on coarse filesystems
            import os
            st = os.stat(session_path)
            os.utime(session_path, (st.st_atime, st.st_mtime + 5))
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(watch.time, "sleep", fake_sleep)
    rc = watch.watch(str(session_path), root=projects_root, interval=0, once=False, out=buf)
    text = buf.getvalue()
    assert rc == 0
    assert "13:00:00  turn 2  +1 call" in text
    assert "stopped:" in text
