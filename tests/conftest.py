"""Builds a small synthetic Claude Code session on disk that mirrors the real layout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SESSION = "11111111-2222-3333-4444-555555555555"
AGENT = "abc123def456"
WF_AGENT = "wf0000aaaa1111"
MODEL = "claude-opus-5-5"


def _usage(inp=0, read=0, write=0, out=0):
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write, "output_tokens": out}


def _assistant(ts, mid, content, usage, uuid, parent=None, agent=None, model=MODEL):
    d = {"type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
         "isSidechain": agent is not None, "sessionId": SESSION, "version": "2.1.301",
         "message": {"id": mid, "model": model, "role": "assistant", "content": content,
                     "stop_reason": "tool_use", "usage": usage}}
    if agent:
        d["agentId"] = agent
    return d


def _user_text(ts, text, uuid, agent=None, compact=False):
    d = {"type": "user", "uuid": uuid, "parentUuid": None, "timestamp": ts, "isSidechain": agent is not None,
         "sessionId": SESSION, "message": {"role": "user", "content": text}}
    if compact:
        d["isCompactSummary"] = True
    if agent:
        d["agentId"] = agent
    return d


def _tool_result(ts, tool_use_id, text, uuid, agent=None, tur=None, is_error=False):
    d = {"type": "user", "uuid": uuid, "timestamp": ts, "isSidechain": agent is not None, "sessionId": SESSION,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                                                  "content": text, "is_error": is_error}]}}
    if tur is not None:
        d["toolUseResult"] = tur
    if agent:
        d["agentId"] = agent
    return d


def build_session(root: Path) -> Path:
    proj = root / "-Users-test-proj"
    proj.mkdir(parents=True)
    main = proj / f"{SESSION}.jsonl"
    T = "2026-10-15T12:{:02d}:00.000Z".format

    lines = [
        {"type": "ai-title", "aiTitle": "fixture session", "sessionId": SESSION},
        _user_text(T(0), "fix the failing test", "u1"),
        # call 1: thinking + text + tool_use split across 3 lines sharing a message id
        _assistant(T(1), "msg_1", [{"type": "thinking", "thinking": ""}], _usage(10, 1000, 500, 50), "a1"),
        _assistant(T(1), "msg_1", [{"type": "text", "text": "Reading."}], _usage(10, 1000, 500, 50), "a2"),
        _assistant(T(1), "msg_1", [{"type": "tool_use", "id": "tu_read1", "name": "Read",
                                    "input": {"file_path": "/p/a.py"}}], _usage(10, 1000, 500, 50), "a3"),
        _tool_result(T(1), "tu_read1", "x" * 2000, "u2", tur={"type": "text", "file": {"filePath": "/p/a.py"}}),
        # call 2: two parallel tool uses, one of them spawns an agent
        _assistant(T(2), "msg_2", [
            {"type": "tool_use", "id": "tu_read2", "name": "Read", "input": {"file_path": "/p/a.py"}},
            {"type": "tool_use", "id": "tu_agent", "name": "Agent",
             "input": {"description": "look around", "subagent_type": "Explore", "prompt": "find things"}},
        ], _usage(20, 1500, 0, 100), "a4"),
        _tool_result(T(2), "tu_read2", "x" * 2000, "u3", tur={"type": "text", "file": {"filePath": "/p/a.py"}}),
        _tool_result(T(5), "tu_agent", "found it", "u4", tur={"status": "completed"}),
        # call 3: text only (reasoning leaf) with a big oversized bash result afterwards? no: text only
        _assistant(T(6), "msg_3", [{"type": "text", "text": "done"}], _usage(5, 1600, 0, 30), "a5"),
        # turn 2 after a long idle gap: cache re-warm
        _user_text(T(30), "now add a feature", "u5"),
        _assistant(T(31), "msg_4", [{"type": "tool_use", "id": "tu_bash", "name": "Bash",
                                     "input": {"command": "pytest -q"}}], _usage(5, 0, 25000, 80), "a6"),
        _tool_result(T(31), "tu_bash", "", "u6", tur={"stdout": "y" * 20000, "stderr": "", "interrupted": False}),
        {"type": "system", "subtype": "compact_boundary", "uuid": "s1", "timestamp": T(32),
         "compactMetadata": {"trigger": "auto", "preTokens": 26000, "postTokens": 3000,
                             "cumulativeDroppedTokens": 23000}},
        _user_text(T(32), "This session is being continued...", "u7", compact=True),
        _assistant(T(33), "msg_5", [{"type": "tool_use", "id": "tu_wf", "name": "Workflow",
                                     "input": {"name": "deep-research"}}], _usage(3000, 0, 0, 20), "a7"),
        _tool_result(T(33), "tu_wf", "launched", "u8",
                     tur={"status": "async_launched", "runId": "wf_test-1", "workflowName": "deep-research"}),
        {"type": "ai-title", "aiTitle": "fixture session", "sessionId": SESSION},   # repeated title lines are normal
        # synthetic local message, must not be priced
        _assistant(T(33), "syn_1", [{"type": "text", "text": "[local]"}], _usage(0, 0, 0, 0), "a8",
                   model="<synthetic>"),
        {"type": "cost-state", "sessionId": SESSION, "timestamp": T(40), "totalCostUSD": 0.0, "totalDuration": 2_000_000,
         "totalAPIDuration": 100_000, "totalToolDuration": 5_000,
         "modelUsage": {}},
    ]
    main.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    sub = proj / SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / f"agent-{AGENT}.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": "look around", "toolUseId": "tu_agent", "spawnDepth": 1}))
    agent_lines = [
        _user_text(T(3), "find things", "s_u1", agent=AGENT),
        _assistant(T(3), "msg_s1", [{"type": "tool_use", "id": "tu_grep", "name": "Grep",
                                     "input": {"pattern": "foo", "path": "/p"}}],
                   _usage(50, 0, 8000, 40), "s_a1", agent=AGENT),
        _tool_result(T(4), "tu_grep", "match", "s_u2", agent=AGENT),
        _assistant(T(4), "msg_s2", [{"type": "text", "text": "found it"}], _usage(10, 8000, 100, 60), "s_a2",
                   agent=AGENT),
    ]
    (sub / f"agent-{AGENT}.jsonl").write_text("\n".join(json.dumps(x) for x in agent_lines) + "\n")

    # A workflow-spawned agent: lives under workflows/<run id>/ and its meta has no toolUseId.
    wf = sub / "workflows" / "wf_test-1"
    wf.mkdir(parents=True)
    (wf / f"agent-{WF_AGENT}.meta.json").write_text(json.dumps({"agentType": "workflow-subagent", "spawnDepth": 1}))
    wf_lines = [
        _user_text(T(34), "search angle 1", "w_u1", agent=WF_AGENT),
        _assistant(T(34), "msg_w1", [{"type": "text", "text": "result"}], _usage(400, 0, 0, 90), "w_a1",
                   agent=WF_AGENT),
    ]
    (wf / f"agent-{WF_AGENT}.jsonl").write_text("\n".join(json.dumps(x) for x in wf_lines) + "\n")
    return main


def fill_cost_state(main: Path, model_usage: dict, total_cost: float, start_ms: int | None = None) -> None:
    """Rewrite the cost-state line so reconciliation tests can control it."""
    out = []
    for raw in main.read_text().splitlines():
        d = json.loads(raw)
        if d.get("type") == "cost-state":
            d["modelUsage"] = model_usage
            d["totalCostUSD"] = total_cost
            if start_ms is not None:
                d["startTime"] = start_ms
        out.append(json.dumps(d))
    main.write_text("\n".join(out) + "\n")


@pytest.fixture
def session_path(tmp_path: Path) -> Path:
    return build_session(tmp_path)


@pytest.fixture
def projects_root(session_path: Path) -> Path:
    return session_path.parent.parent
