# tokentrace

Per-step token and cost attribution for AI agent runs.

Tools like Claude Code tell you what a session cost. tokentrace tells you **where it went**: which turn, which subagent, which tool call, and which waste pattern (re-reading the same file, a 20k-character shell dump, a cache rebuilt after an idle gap, a compaction) drove the number. It then cross-checks its own totals against the numbers the source tool recorded, so you know when to trust the breakdown.

It reads Claude Code's local transcripts today. The event schema and the SQLite store are tool-neutral so that Cursor, Codex, or raw API logs can be added as adapters and compared side by side.

```
$ tokentrace report 9cc94229 --rows 5

tokentrace report  session 9cc94229-abdc-4433-a5b1-53c5781956d8
title: munisignal project lookup
span: 2026-07-21T19:05:51.251Z .. 2026-09-28T02:58:52.189Z (1639.9h wall, 6.4h active)   pricing table 2026-10-06

TOTAL  $190.00   97,748,142 tokens   (input 10,575, cache read 88,107,639, cache write 9,249,390, output 380,538)

BY AGENT  (top 5 of 133; inclusive cost rolls subagents into their parent)
  agent                                           calls  tools   own cost  incl cost   time
  session                                           355    279    $167.41    $190.00 1639.9h
  agent general-purpose: Distill research jou...     22     21      $2.17      $2.17     9m
  agent workflow-subagent: a596fddc4fa48c562         17     17      $0.76      $0.76     4m
  ...
  ... 128 more agents, 418 calls, $18.53 own cost

BY TURN  (top 5 of 108 by cost, subagents included)
  turn                                                        calls  tools       cost  share
  turn 3: Are you able to use the research mode on this?        469      3     $20.78    11%
  turn 112: The EXCEPTION runs next and rollback is calle...      1      0      $6.80     4%
  turn 111: I just want to commit the changes up until db...      1      0      $6.77     4%

BY TOOL  (issue cost = share of the API calls that emitted the tool call; incl = plus spawned subagents)
  tool              calls errors  result chars  ~ctx tokens  issue cost  incl cost
  Bash                280     21       346,415       86,603      $68.67     $68.67
  Workflow              2      0         2,440          610       $0.61     $21.03
  ToolSearch          116      0             0            0      $10.19     $10.19

FINDINGS (41)
  [warn ] oversized_result         Read: /Users/.../journal.jsonl returned 51,122 chars (~12,780 tokens) into context of agent aa6106e2226bba587
  [warn ] unrecorded_source_usage  source counts 43,712,415 more cache_read tokens than the transcript records (compaction summaries, away summaries, classifier calls on the same model); these are billed but cannot be attributed to a step
  [info ] cache_rewarm             386,085 tokens re-written to cache after a 1331-minute gap (turn 42), ~$4.73 more than a cache hit
  [info ] compaction               context compacted (auto): 429,393 -> 6,009 tokens at turn 31
  [info ] reconciliation           snapshot window covers 113 of 820 API calls (2026-09-05T04:15:52Z to 2026-09-28T02:58:52Z): parsed $104.76 vs source $167.33; cache_write, output match within 2%. ...
```

A turn with one API call that costs $6.80 is not a typo: after a long idle gap the 540k-token context is re-written to the cache at the cache-write rate. The `cache_rewarm` findings quantify exactly that.

## Install

Project-local only, no global installs:

```
cd tokentrace
uv venv --python 3.12
uv pip install -e ".[dev]"
.venv/bin/tokentrace --help
```

No runtime dependencies. Python 3.11 or newer.

## Commands

| Command | What it does |
|---|---|
| `tokentrace sessions` | List Claude Code sessions on this machine, newest first |
| `tokentrace report <target>` | Text report. `--format json` for the full tree, `--format html --out r.html` for a page with a context-growth chart and a collapsible attribution tree |
| `tokentrace check <target>` | Findings only. Exit code 1 if any finding is error-level, so it can gate a script |
| `tokentrace ingest <target>... --db usage.db` | Store parsed events in SQLite |
| `tokentrace totals --db usage.db` | Per-day, per-model tokens and cost from the database |
| `tokentrace dashboard [--open]` | Sync every session on the machine into the SQLite store, then write a static dashboard to `reports/dashboard/`: spend per day by model, a flow diagram of cost from models through activities to tools, token mix, a sessions table linking to a full report page per session, tools and recurring findings across sessions. No server, no JavaScript dependencies |
| `tokentrace savings [<target>]` | Concrete ways to lower the cost of a session, each with the dollars it would have saved and the evidence. `--all` adds them up across every session in the database. Also shown in `report` and on the dashboard's Savings tab |
| `tokentrace watch [<target>]` | Follow a live session. Prints one line per turn as it runs, with the turn's cost, the current context size and cache hit rate, and the running session total, plus any new waste finding. Defaults to the most recent session |

`<target>` is a transcript path, a session directory, a session id prefix, or `latest`.

## How attribution works

1. The adapter turns a transcript into flat **events**: `prompt`, `api_call`, `tool_use`, `tool_result`, `compaction`, `cost_snapshot`. Each carries the session, the agent (main thread or subagent id), the turn, a parent link, and the four token counters.
2. `attribution.build` rebuilds the tree: session, then turns, then the tool calls each turn made, with subagents nested under the `Agent` call that spawned them.
3. An API call's tokens and cost are split equally among the tool calls it emitted. Calls that emitted no tool land in a `reasoning` leaf for that turn. Everything rolls up, so inclusive numbers at every level sum to the session total. A check enforces this.
4. Cost uses a **versioned list-price table** (`pricing.py`). The version is stored with every ingest so historic sessions can be recomputed when prices change. Unknown models are counted at $0 and flagged. Cache writes are priced by TTL: 1.25x input for 5-minute writes and 2x input for 1-hour writes. Claude Code writes almost all of its cache with the 1-hour TTL, so pricing every write at the 5-minute rate understates a session by roughly a sixth.

### Checks

| Code | Meaning |
|---|---|
| `repeated_call` | Same `Read`, `Grep`, `Glob`, or `Bash` command ran more than once; reports the repeat tokens |
| `oversized_result` | A tool result above the size threshold (default 15,000 chars) |
| `cache_rewarm` | The previous context had to be written to the cache again because the idle gap outlived the cache (1 hour for 1-hour writes, 5 minutes otherwise). Reports the extra cost versus a hit |
| `cache_break` | The same kind of rewrite while the cache was still warm, because the prefix changed: a model switch, a compaction, or an edited system prompt or tool list (the last is inferred) |
| `context_jump` | Context grew by more than 30k tokens in one step |
| `compaction` | The context was summarized; pre and post sizes |
| `tool_use_without_result`, `result_without_tool_use`, `api_call_without_usage`, `unlinked_subagent`, `unknown_model_price` | Parser gaps or data gaps that lower confidence in the numbers |
| `tree_mismatch`, `child_exceeds_parent` | Internal invariants. These should never fire; if they do, the report is wrong |
| `unrecorded_source_usage` | The source counts more tokens than the transcript on some field while being at least as large on every field. Claude Code makes calls on the same model that never appear as messages (compaction summaries, away summaries, classifier and advisor calls). Billed, but not attributable to a step. The parser counting more than the source stays a `token_mismatch` error, since that is what a parser bug looks like |
| `token_mismatch`, `cost_mismatch`, `unattributable_model`, `model_missing_in_source`, `model_attribution_drift`, `reconciliation` | Comparison with Claude Code's own `cost-state` totals. Models that appear only in the source totals (title generation on Haiku, for example) are listed as unattributable rather than treated as errors |

### Savings tips

`tokentrace savings` turns the findings into changes you can make. Each tip replays the recorded calls with one thing changed and prices them again. That assumes the model would have made the same calls otherwise, so read every number as an upper bound. Tips can overlap (compacting earlier also shrinks what an idle resume rewrites), so don't add them all up.

| Tip | Kind | What it estimates |
|---|---|---|
| Clear or compact before a long break | free | Each expired-cache resume, as if it had started again from the session's base context |
| Keep the model and the prompt prefix fixed | free | The rewrites recorded as `cache_break`, apart from compactions |
| Trim large tool output | free | Each result over 4,000 tokens cut to 1,000. The tokens saved are paid once as a cache write and again as a read on every later call until the context shrinks |
| Compact when the context passes 200k | tradeoff | The main thread replayed with a /compact each time it crosses `--cap`. The cost of each compaction (one full read, a summary, a fresh write) is subtracted |
| Use a cheaper model for routine turns | tradeoff | The same calls priced one tier down (Fable to Opus 5.5, Opus to Sonnet 5.5) |

Across 79 of the author's sessions ($1,487), the free wins came to about $574. Almost all of that came from returning to large sessions after more than an hour away.

## Claude Code transcript facts the parser relies on

Verified against Claude Code 2.1.216 through 2.1.301 on macOS. Pinned in the fixture under `tests/`.

- Transcripts live in `~/.claude/projects/<project-slug>/<session-id>.jsonl`. Subagents are in `<session-id>/subagents/agent-<id>.jsonl` with a sibling `.meta.json` holding the `toolUseId` of the `Agent` call that spawned them.
- One API response is written as several `assistant` lines that share `message.id` and repeat the same `usage` block. Usage is counted once per message id.
- `user` lines whose content is a list of `tool_result` blocks are tool results. `toolUseResult` holds structured data such as `stdout` and `filePath`.
- `system` lines with `subtype: "compact_boundary"` mark compaction; `compactMetadata` has `preTokens` and `postTokens`. The follow-up `user` line has `isCompactSummary: true` and does not start a new turn.
- `cost-state` lines are Claude Code's own running totals per model, used for reconciliation. They cover only the current process lifetime (`startTime`) and are written periodically, so parsed events are restricted to that window before comparing. They key usage by the model active at snapshot time, so a session that changed model is compared in aggregate first.
- `usage.cache_creation` splits each cache write into `ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens`. Claude Code uses the 1-hour TTL for nearly all writes.
- `<synthetic>` is the model name for locally generated messages and is priced at zero.

If a Claude Code release changes any of this, the fixture-based tests fail before the report silently goes wrong.

## Roadmap

- More adapters (Cursor, Codex CLI, raw API logs) producing the same events, each declaring its attribution depth: steps, calls, or totals only.
- Reconciliation against the Anthropic usage API, not only the local `cost-state`.
- Budget alerts: warn in `watch` when a session or a day passes a set amount, and when a resume is about to rewrite a large expired cache.
- Side-by-side comparison of the same task run in two tools.

## Development

```
.venv/bin/python -m pytest -q
```

MIT license.
