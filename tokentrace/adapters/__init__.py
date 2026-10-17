"""Adapters turn tool-specific logs into the shared Event schema.

Each adapter exposes ``parse_session(path) -> list[Event]`` and a module-level
``ATTRIBUTION_DEPTH`` describing how much structure the source log carries:

* ``"steps"``   - per API call, per tool call, and nested subagents
* ``"calls"``   - per API call only
* ``"totals"``  - daily or per-session totals only
"""
