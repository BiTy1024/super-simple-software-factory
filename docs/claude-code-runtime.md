# Claude Code as a second runtime

SSSF v1 runs every agent on pi. The config schema already accepts
`coding_agent: claude_code`, and `adw_modules/agent_cc.py` was a stub. This
document fills it in: Claude Code becomes a runtime next to pi. One roster can
mix both. Runs land in the same `sssf.db`, and the visualizer shows both.

## Decision: headless `claude -p` per agent call

Three ways were considered.

| Option | How it runs | Fits SSSF? |
|---|---|---|
| **A. Headless CLI** `claude -p --output-format stream-json --verbose` | one subprocess per send, like `pi -p --mode json` | yes |
| B. Claude Agent SDK (`claude-agent-sdk`, Python) | in-process `query()` loop | partly |
| C. Claude Code as orchestrator, phases as subagents | one Claude Code session runs the workflow; each agent is a subagent | no |

**A is the choice.** Two reasons:

1. **The control plane stays in Python.** That is SSSF's core rule: "code
   owns sequencing, retries, and acceptance". Option C turns that around. The
   orchestrating model owns the loop, and Python only records what happened
   after the fact. That is a useful mode for interactive work, but it is a
   different product. Option A keeps `agents.execute()` exactly as it is:
   render prompts, send, parse the envelope, gate, correct in the same
   session, enforce write permissions.
2. **It is the same shape as pi.** `agent_pi.run()` spawns `pi -p --mode json`,
   tails JSONL, and forwards each event while the agent works. `agent_cc.run()`
   does the same with `claude -p --output-format stream-json`. Sessions
   continue with `--session-id <uuid>` on the first send and `--resume <uuid>`
   after it. That keeps one agent's context window across JSON-fix retries,
   gate corrections, and joined runs, which is pi's `--session-id` contract.
   The pid is a real child process, so `processes` rows and `just kill` keep
   working.

**Trade-off.** Option B would give typed messages and no subprocess. It would
also add a Python dependency that the stamped ADWs (PEP 723 inline deps)
would have to carry. And it would be a second integration style that pi does
not have. The CLI's stream-json is a looser contract than SDK types, so the
parser in `agent_cc.py` is defensive: unknown event types are written to
`raw_output.jsonl` and otherwise ignored. A process per send costs some
seconds of startup. That is negligible next to model latency, and it is what
pi pays too.

What A gives up compared with C: the orchestrator cannot improvise phases.
That is intended.

## Runtime and model per agent

The roster already has `coding_agent`. It now means something:

```yaml
defaults:
  coding_agent: pi
  claude_code:                     # only read by claude_code agents
    permission_mode: dontAsk       # headless: nobody answers prompts; --tools/--allowedTools decide
    system_prompt_mode: append     # append | replace (Claude Code's own system prompt)
    bare: false                    # true: --bare, no user hooks/plugins/CLAUDE.md, API key auth only
    setting_sources: null          # e.g. "project,local" to ignore user settings
    max_budget_usd: null           # --max-budget-usd per send
    extra_args: []

agents:
  - name: planner
    coding_agent: claude_code
    model: opus                    # alias or full id: claude-opus-5-5
    thinking: high                 # -> --effort high
  - name: builder
    coding_agent: claude_code
    model: sonnet
  - name: reviewer
    coding_agent: claude_code
    model: opus
  - name: documenter
    coding_agent: claude_code
    model: haiku
  - name: scout
    coding_agent: pi               # mixing runtimes in one chain is fine
    model: google/gemini-3.6-flash
```

`adws/adw_sssf_config/claude_code.example.yaml` ships this roster by role:
planner and reviewer on Opus, builder on Sonnet, documenter and scout on
Haiku.

Mapping from the roster to the CLI:

| Roster | Claude Code |
|---|---|
| `model` | `--model` (an `anthropic/` prefix is stripped) |
| `thinking` | `--effort` (`off`/`minimal` -> `low`, the rest passes through) |
| `tools` | `--tools` and `--allowedTools`. pi names map: `read`->`Read`, `bash`->`Bash`, `edit`->`Edit`, `write`->`Write`, `grep`->`Grep`, `find`/`ls`->`Glob`. A capitalized name passes through (`Skill`, `Agent`, `WebFetch`). Unmapped lowercase names (pi extension tools) are dropped and listed in the `agent_start` payload. |
| `harness_engineering` | `--plugin-dir` per entry (pi: `-e` extensions) |
| `writes` | unchanged: enforced in Python after every call (`permissions.py`) |
| system prompt | `--append-system-prompt` (default) or `--system-prompt` |

`append` is the default on purpose. The value of Claude Code as a runtime is
its harness: tool guidance, skills, subagents. Replacing its system prompt
throws that away. `replace` is there for parity experiments with pi.

`validate()` skips pi's model catalog for `claude_code` agents and checks
instead that the `claude` binary resolves (`CLAUDE_PATH`, like `PI_PATH`).

## Tracing into the same sssf.db

No new tables. Everything is in the existing columns and payloads, so an
older visualizer still renders a Claude Code run. The additions are payload
fields, read when present.

**Agent identity.** `agent_start.payload.coding_agent` and
`agent_sessions.coding_agent` already exist. They carry `claude_code`.
`session_id` is the Claude Code session UUID, so
`claude --resume <id>` opens the agent's real conversation.

**Tool calls.** One `tool_call` event per call, emitted when its
`tool_result` arrives, with the call's real span in
`started_at`/`ended_at`. That is the same contract as pi's
`ToolCallTracker`. The payload adds:

- `skill`: the skill active for this call. It is the last `Skill` tool call
  in the same context (main thread or subagent) since this send began.
  Within one assistant message, the `Skill` call itself still counts under
  the previous skill. A subagent inherits the skill active when it was spawned.
- `subagent`: `{id, type}` when the call came from a subagent
  (`parent_tool_use_id` set), else absent.
- `runtime: "claude_code"`.

Tool names are lowercased (`Bash` -> `bash`) so a skill × tool view lines
up across runtimes and with traces written by other Claude Code tracers.

**Tokens and cost.** Claude Code's stream writes one API message as several
lines, one per content block. The `usage` repeats on every line, and
`output_tokens` grows while the message streams. Summing per line
over-counts. The adapter keys usage by `message.id`, keeps the latest line's
usage, and counts each message once:

- buckets: input, output, cache read, cache write. Cache write is split into
  5-minute and 1-hour writes from `usage.cache_creation` for pricing.
- `UsageBreakdown` gets the token buckets, and component costs from a price
  table (`CLAUDE_PRICES` in `agent_cc.py`, USD per million tokens,
  overridable with `SSSF_CLAUDE_PRICING=<json file>`).
- **The total is Claude Code's own number.** `result.total_cost_usd` is what
  Claude Code itself bills for the send, subagents included. When it is
  present, it is the `cost` that rolls into `sessions.total_cost`. The
  table-priced sum is kept as `usage.priced_cost`, so drift between the two
  shows up instead of hiding. Without a `result` event (the process was
  killed), the priced sum is used.
- Per model: `result.modelUsage[model].costUSD` when present, else priced.
- Context: input + cache read + cache write of the last main-thread message.
  The window comes from `result.modelUsage[model].contextWindow`.

**Subagents.** A subagent's messages carry `parent_tool_use_id` = the id of
the `Agent`/`Task` call that spawned it. The adapter attributes them to that
subagent: tool calls carry `subagent`, and tokens and cost go to
`agent_end.payload.subagents[]` with type, description, model, tokens, cost,
and tool-call count. A subagent stays inside its parent agent's phase and lane.
It does not become an SSSF agent: SSSF agents are roster entries with their
own prompts and gates, and a subagent is not one.

**Rollups on `agent_end`.** These sit next to the existing `cost`/`usage`:

```json
{"runtime": "claude_code",
 "by_model": {"claude-sonnet-5-5": {"tokens": 1234, "cost": 0.01}},
 "by_skill": {"(none)": {"tokens": 1234, "cost": 0.01, "tool_calls": 3}},
 "subagents": [{"id": "toolu_…", "type": "Explore", "model": "…", "tokens": 0, "cost": 0, "tool_calls": 0}]}
```

They are summed across every send of the phase, like `usage`. pi runs do not
write them. The visualizer falls back to the `agent_start` model and the
`(none)` skill.

## Importing free Claude Code sessions (not built here)

A session started by hand in Claude Code has no ADW around it. Its transcript
(`<config>/projects/<slug>/<id>.jsonl` plus `subagents/`) still maps onto the
schema:

| Claude Code | sssf.db |
|---|---|
| session file | `sessions` row, `adw_id` = session id |
| main thread / subagent | `agent_sessions` row each |
| stretch of turns under one skill | `phases` row, `kind=agent`, `name` = skill |
| `tool_use` | `tool_call` event, same payload as above |
| usage | `agent_end` with the same rollups |

The multi-line usage rule and the skill attribution rules are the same as in
the adapter, so both should share one parser module. An importer would need
an incremental byte offset per file and deterministic row ids, so a rerun is
a no-op. It should store metadata only, no prompt or tool-output text. This
document does not build it. The adapter's parser
(`agent_cc.StreamAccumulator`) is written so it can be fed transcript lines
later: it keys on `message.id`, `parent_tool_use_id`, and content blocks,
which both formats share.

## Visualizer

1. **Runtime badge.** The lane label in the trace view and the agent rows on
   a session card show `pi` or `claude-code`. The source is
   `agent_sessions.coding_agent`, falling back to the `agent_start` payload
   while the agent runs. The session card header shows every runtime the
   run used.
2. **Usage view** (`#/usage`, linked from the top bar). It is served by
   `GET /api/usage?days=N`, which aggregates in SQL over `events`:
   - cost rollups per run, model, skill, runtime, and day;
   - tool usage per agent (and subagent) and per skill;
   - a skill × tool matrix: call counts with an error count per cell.
3. **Phase detail** already shows `coding_agent` in its agent-config section.
   It is unchanged.

## Tests and smoke run

- `tests/` (Python, stdlib `unittest`): the stream parser against a fixture
  stream. The fixture has multi-line usage, a skill, a subagent, and a
  failing tool. Also tests for command construction, config validation, and
  a full `adw_prompt` run against `tests/fixtures/fake_claude.py` via
  `CLAUDE_PATH`. That last test is the zero-cost smoke run: the real ADW path
  end to end into a temp `sssf.db`.
- `apps/visualizer/server/usage.test.ts` (`bun test`): the usage rollup over
  a seeded db.

```sh
uv run --with pydantic --with pyyaml --with python-dotenv --with rich \
  python -m unittest discover -s tests -t .
cd .claude/skills/sssf/apps/visualizer && bun test && bun run build
```

## Out of scope / known gaps

- Long-context surcharges and Haiku's higher rate above 100K prompt tokens
  are not modelled in `priced_cost`. The authoritative total comes from
  Claude Code, so only the per-skill split is affected.
- Server-tool fees (web search) appear in the total, not in any bucket.
- Free-session import (above).
- `claude_code` agents ignore pi extension tools. A roster that relies on
  `subagent_*` pi tools keeps that agent on pi, or uses Claude Code's own
  `Agent` tool instead.
