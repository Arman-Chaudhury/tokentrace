"""Tool-neutral event schema.

Every adapter emits a flat list of ``Event`` records.  Nothing in this module
knows about Claude Code, Cursor, or any other source, so the attribution,
checks, storage, and reports work unchanged when a new adapter is added.

Event kinds
-----------
prompt        a human (or compaction-generated) user turn
api_call      one model request/response, carrying the usage counters
tool_use      a tool invocation emitted by an api_call (parent_id = api_call id)
tool_result   the result of a tool_use (parent_id = tool_use id)
compaction    the context was summarized; payload has pre/post token counts
cost_snapshot the source tool's own running totals, used for reconciliation
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

KINDS = ("prompt", "api_call", "tool_use", "tool_result", "compaction", "cost_snapshot")


@dataclass
class Event:
    id: str
    session_id: str
    source: str                  # adapter name, e.g. "claude_code"
    kind: str
    ts: str                      # ISO-8601 UTC, "" if unknown
    agent_id: str | None = None  # None = main thread; otherwise the subagent id
    turn: int = 0                # index of the user prompt this event falls under (per agent)
    parent_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    tool_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def context_tokens(self) -> int:
        """Prompt size the model saw on this call (everything but output)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.context_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        kwargs = {k: d.get(k) for k in cls.__dataclass_fields__}
        kwargs["payload"] = d.get("payload") or {}
        for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "turn"):
            kwargs[k] = int(kwargs.get(k) or 0)
        return cls(**kwargs)


@dataclass
class Usage:
    """Token counters, summed across events."""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    def add(self, e: Event) -> None:
        self.input_tokens += e.input_tokens
        self.cache_read_tokens += e.cache_read_tokens
        self.cache_write_tokens += e.cache_write_tokens
        self.output_tokens += e.output_tokens

    def merge(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.output_tokens += other.output_tokens

    @property
    def context(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def total(self) -> int:
        return self.context + self.output_tokens

    def to_dict(self) -> dict[str, int]:
        return asdict(self) | {"total": self.total}
