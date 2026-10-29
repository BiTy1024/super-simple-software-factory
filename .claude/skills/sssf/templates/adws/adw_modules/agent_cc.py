"""Claude Code interface — the second coding agent, next to pi.

Runs `claude -p --output-format stream-json --verbose` and tails its JSON lines
the way agent_pi tails `pi --mode json`: every event is written to
raw_output.jsonl and forwarded to a callback WHILE the agent works. Running and
continuing an agent are two flags here instead of one: `--session-id <uuid>`
creates the conversation, `--resume <uuid>` continues it, so a JSON fix or a
gate correction lands in the same context window, exactly like pi.

The prompt travels on stdin, not argv: `--tools` and `--allowedTools` take a
variable number of values, and a positional prompt after them would be read as
one more tool name.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from .agent_pi import ARG_VALUE_CHARS, RESULT_SNIPPET_CHARS, _clip, _label
from .data_types import ClaudeRequest, PiResult, UsageBreakdown
from .utils import now_iso, operator_env

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")
NO_SKILL = "(none)"

# USD per million tokens, Anthropic first-party list prices. cache_write_5m is
# 1.25 x input and cache_write_1h is 2 x input. These price the per-bucket and
# per-skill split only — the run's total is Claude Code's own total_cost_usd.
# Point SSSF_CLAUDE_PRICING at a JSON file of the same shape to override.
CLAUDE_PRICES: dict[str, dict[str, float]] = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_write_5m": 12.5, "cache_write_1h": 20.0, "cache_read": 0.25},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write_5m": 5.0, "cache_write_1h": 8.0, "cache_read": 0.2},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write_5m": 2.5, "cache_write_1h": 4.0, "cache_read": 0.2},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_write_5m": 0.125, "cache_write_1h": 0.2, "cache_read": 0.01},
}

# Roster tool names are pi's; Claude Code names its built-ins differently.
TOOL_NAMES = {"read": "Read", "bash": "Bash", "edit": "Edit", "write": "Write",
              "grep": "Grep", "find": "Glob", "ls": "Glob"}

# --effort takes low..max; pi's thinking scale has two levels below low.
EFFORT = {"off": "low", "minimal": "low", "low": "low", "medium": "medium",
          "high": "high", "xhigh": "xhigh", "max": "max"}

SPAWN_TOOLS = ("Agent", "Task")


def prices() -> dict[str, dict[str, float]]:
    override = os.environ.get("SSSF_CLAUDE_PRICING")
    if override and Path(override).is_file():
        return json.loads(Path(override).read_text()).get("models", {})
    return CLAUDE_PRICES


def price_for(model: str) -> Optional[dict[str, float]]:
    """Exact id first, then the longest table key the id starts with — so a
    dated snapshot id prices like its family."""
    table = prices()
    if model in table:
        return table[model]
    matches = [key for key in table if model.startswith(key)]
    return table[max(matches, key=len)] if matches else None


def map_tools(tools: Optional[list[str]]) -> tuple[Optional[list[str]], list[str]]:
    """Roster allowlist -> (Claude Code tool names, names that had no mapping).

    A capitalized name is taken as a Claude Code tool already (Skill, Agent,
    WebFetch). A lowercase name without a mapping is a pi extension tool,
    which Claude Code does not have — it is dropped and reported, not guessed.
    """
    if tools is None:
        return None, []
    mapped, dropped = [], []
    for name in tools:
        target = TOOL_NAMES.get(name) or (name if name[:1].isupper() else None)
        if target is None:
            dropped.append(name)
        elif target not in mapped:
            mapped.append(target)
    return mapped, dropped


def model_arg(model: str) -> str:
    return model.removeprefix("anthropic/")


def available() -> bool:
    return shutil.which(CLAUDE_PATH) is not None or Path(CLAUDE_PATH).is_file()


def build_command(request: ClaudeRequest) -> list[str]:
    opts = request.options
    prompt_flag = ("--append-system-prompt" if opts.system_prompt_mode == "append"
                   else "--system-prompt")
    cmd = [
        CLAUDE_PATH, "-p", "--output-format", "stream-json", "--verbose",
        "--model", model_arg(request.model),
        "--effort", EFFORT.get(request.thinking, "medium"),
        "--resume" if request.resume else "--session-id", request.session_id,
        # `=` form: a rendered prompt may begin with "-" and must not be parsed as a flag.
        f"{prompt_flag}={request.system_prompt}",
        "--permission-mode", opts.permission_mode,
    ]
    tools, _ = map_tools(request.tools)
    if tools is not None:
        cmd += ["--tools", ",".join(tools)]
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
    for plugin_dir in request.plugin_dirs:
        cmd += ["--plugin-dir", plugin_dir]
    if opts.bare:
        cmd.append("--bare")
    if opts.setting_sources:
        cmd += ["--setting-sources", opts.setting_sources]
    if opts.max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(opts.max_budget_usd)]
    return cmd + list(opts.extra_args)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _n(usage: dict, key: str) -> int:
    return int(usage.get(key) or 0)


class StreamAccumulator:
    """Folds Claude Code's stream into tool-call records, usage and rollups.

    Three facts about the stream drive the shape of this class:

    - One API message arrives as several `assistant` lines, one per content
      block, each repeating the message's `usage` with `output_tokens` growing.
      Usage is therefore keyed by `message.id` and counted once.
    - A subagent's lines carry `parent_tool_use_id` = the id of the Agent/Task
      call that spawned it. That id is the subagent's identity here.
    - A skill is active from its `Skill` tool call until the send ends, per
      context (main thread or one subagent). A subagent starts with the skill
      that was active when it was spawned.

    `observe()` returns the tool-call records that finished on this event, so
    the same class drives the live trace and the end-of-send totals.
    """

    def __init__(self) -> None:
        self.messages: dict[str, dict] = {}
        self.open_calls: dict[str, dict] = {}
        self.skill: dict[Optional[str], str] = {}
        self.subagents: dict[str, dict] = {}
        self.tool_calls_by_skill: dict[str, int] = {}
        self.text = ""
        self.result: Optional[dict] = None
        self.context_tokens = 0
        self.main_model = ""

    # ── stream ──────────────────────────────────────────────────────────────
    def observe(self, event: dict) -> list[dict]:
        etype = event.get("type")
        if etype == "assistant":
            self._assistant(event)
            return []
        if etype == "user":
            return self._user(event)
        if etype == "result":
            self.result = event
            if isinstance(event.get("result"), str) and event["result"].strip():
                self.text = event["result"]
        return []

    def _assistant(self, event: dict) -> None:
        ctx = event.get("parent_tool_use_id") or None
        message = event.get("message") or {}
        msg_id = message.get("id") or f"anon-{len(self.messages)}"
        usage = message.get("usage") or {}
        known = self.messages.get(msg_id)
        if known is None:
            known = self.messages[msg_id] = {
                "model": message.get("model", ""), "ctx": ctx,
                "skill": self.skill.get(ctx, NO_SKILL), "usage": {}}
        if usage:
            output = max(_n(usage, "output_tokens"), _n(known["usage"], "output_tokens"))
            known["usage"] = {**usage, "output_tokens": output}
        if ctx is None and usage:
            self.context_tokens = (_n(usage, "input_tokens") + _n(usage, "cache_read_input_tokens")
                                   + _n(usage, "cache_creation_input_tokens"))
            self.main_model = message.get("model") or self.main_model
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self._announce(block, ctx)
            elif block.get("type") == "text" and ctx is None and block.get("text", "").strip():
                self.text = block["text"]

    def _announce(self, block: dict, ctx: Optional[str]) -> None:
        call_id = str(block.get("id") or "")
        if not call_id or call_id in self.open_calls:
            return
        name = str(block.get("name") or "tool")
        args = block.get("input") or {}
        active = self.skill.get(ctx, NO_SKILL)
        self.open_calls[call_id] = {"tool": name, "args": args, "ctx": ctx, "skill": active,
                                    "started_at": now_iso(), "clock": time.monotonic()}
        if name == "Skill" and args.get("skill"):
            self.skill[ctx] = str(args["skill"])      # counts from the next call on
        if name in SPAWN_TOOLS:
            self.subagents[call_id] = {
                "id": call_id, "type": args.get("subagent_type") or "general-purpose",
                "description": str(args.get("description") or "")[:200],
                "model": args.get("model") or "", "tokens": 0, "cost": 0.0, "tool_calls": 0}
            self.skill[call_id] = active

    def _user(self, event: dict) -> list[dict]:
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            return []
        records = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            opened = self.open_calls.pop(str(block.get("tool_use_id") or ""), None)
            if opened is None:
                continue
            records.append(self._record(str(block["tool_use_id"]), opened, block))
        return records

    def _record(self, call_id: str, opened: dict, block: dict) -> dict:
        tool, args = opened["tool"], opened["args"]
        record = {
            "tool": tool.lower(),
            "tool_call_id": call_id,
            "args": {key: _clip(value, ARG_VALUE_CHARS) if isinstance(value, str) else value
                     for key, value in args.items()},
            "ok": not block.get("is_error", False),
            "label": _label(tool.lower(), args),
            "skill": opened["skill"],
            "runtime": "claude_code",
            "started_at": opened["started_at"],
            "ended_at": now_iso(),
            "duration_ms": int((time.monotonic() - opened["clock"]) * 1000),
        }
        result_text = _text_of(block.get("content"))
        if result_text:
            record["result_snippet"] = _clip(result_text, RESULT_SNIPPET_CHARS)
        sub = self.subagents.get(opened["ctx"] or "")
        if sub:
            record["subagent"] = {"id": sub["id"], "type": sub["type"]}
            sub["tool_calls"] += 1
        self.tool_calls_by_skill[opened["skill"]] = self.tool_calls_by_skill.get(opened["skill"], 0) + 1
        return record

    # ── totals ──────────────────────────────────────────────────────────────
    def totals(self) -> tuple[UsageBreakdown, dict]:
        """Usage over every message once, plus the agent_end rollups."""
        usage = UsageBreakdown()
        by_model: dict[str, dict] = {}
        by_skill: dict[str, dict] = {}
        for msg in self.messages.values():
            u = msg["usage"]
            if not u:
                continue
            model = msg["model"] or "unknown"
            inp, out = _n(u, "input_tokens"), _n(u, "output_tokens")
            read, write = _n(u, "cache_read_input_tokens"), _n(u, "cache_creation_input_tokens")
            write_1h = _n(u.get("cache_creation") or {}, "ephemeral_1h_input_tokens")
            tokens = inp + out + read + write
            price = price_for(model) or {}
            costs = {
                "input": inp * price.get("input", 0) / 1e6,
                "output": out * price.get("output", 0) / 1e6,
                "cache_read": read * price.get("cache_read", 0) / 1e6,
                "cache_write": ((write - write_1h) * price.get("cache_write_5m", 0)
                                + write_1h * price.get("cache_write_1h", 0)) / 1e6,
            }
            cost = sum(costs.values())
            usage.input_tokens += inp
            usage.output_tokens += out
            usage.cache_read_tokens += read
            usage.cache_write_tokens += write
            usage.total_tokens += tokens
            usage.input_cost += costs["input"]
            usage.output_cost += costs["output"]
            usage.cache_read_cost += costs["cache_read"]
            usage.cache_write_cost += costs["cache_write"]
            usage.priced_cost += cost
            _add(by_model, model, tokens=tokens, cost=cost)
            _add(by_skill, msg["skill"], tokens=tokens, cost=cost)
            sub = self.subagents.get(msg["ctx"] or "")
            if sub:
                sub["tokens"] += tokens
                sub["cost"] += cost
                sub["model"] = model
        for skill, calls in self.tool_calls_by_skill.items():
            _add(by_skill, skill, tool_calls=calls)

        usage.total_cost = usage.priced_cost
        if self.result is not None and self.result.get("total_cost_usd") is not None:
            usage.total_cost = float(self.result["total_cost_usd"])
        for model, mu in ((self.result or {}).get("modelUsage") or {}).items():
            if isinstance(mu, dict) and mu.get("costUSD") is not None:
                by_model.setdefault(model, {"tokens": 0, "cost": 0.0})["cost"] = float(mu["costUSD"])
        rollups = {"runtime": "claude_code", "by_model": by_model, "by_skill": by_skill,
                   "subagents": list(self.subagents.values())}
        return usage, rollups

    def context_window(self) -> int:
        model_usage = (self.result or {}).get("modelUsage") or {}
        entry = model_usage.get(self.main_model) or next(iter(model_usage.values()), {})
        return int((entry or {}).get("contextWindow") or 0)


def _add(table: dict, key: str, **amounts) -> None:
    row = table.setdefault(key, {"tokens": 0, "cost": 0.0})
    for name, value in amounts.items():
        row[name] = row.get(name, 0) + value


def merge_rollups(into: dict, other: dict) -> dict:
    """Sum one send's rollups into the phase's — a retried phase paid twice."""
    if not other:
        return into
    into.setdefault("runtime", other.get("runtime"))
    for table in ("by_model", "by_skill"):
        target = into.setdefault(table, {})
        for key, row in (other.get(table) or {}).items():
            _add(target, key, **{k: v for k, v in row.items() if isinstance(v, (int, float))})
    into.setdefault("subagents", []).extend(other.get("subagents") or [])
    return into


class ToolCallTracker:
    """agent_pi.ToolCallTracker's counterpart: raw event in, finished records out."""

    def __init__(self) -> None:
        self._stream = StreamAccumulator()

    def observe(self, event: dict) -> list[dict]:
        return self._stream.observe(event)


def run(request: ClaudeRequest, on_event: Optional[Callable[[dict], None]] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None) -> PiResult:
    """Run one non-interactive Claude Code send. Same contract as agent_pi.run."""
    cmd = build_command(request)
    raw_path = Path(request.raw_output_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, bufsize=1, cwd=request.cwd, env=operator_env())
    if on_spawn:
        on_spawn(process.pid)
    assert process.stdin is not None and process.stdout is not None
    # Written and closed before reading: an open stdin is a child waiting
    # forever for input that never comes (see agent_pi.run).
    process.stdin.write(request.prompt)
    process.stdin.close()

    stream = StreamAccumulator()
    with raw_path.open("a") as raw:
        for line in process.stdout:
            raw.write(line)
            raw.flush()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            stream.observe(event)
            if on_event:
                on_event(event)

    stderr = process.stderr.read() if process.stderr else ""
    returncode = process.wait()
    if on_exit:
        on_exit(process.pid)

    usage, rollups = stream.totals()
    result = PiResult(text=stream.text, returncode=returncode, session_id=request.session_id,
                      tokens=usage.total_tokens, cost=usage.total_cost, usage=usage,
                      context_tokens=stream.context_tokens,
                      context_window=stream.context_window(), rollups=rollups)
    if stream.result and stream.result.get("is_error") and not result.text:
        raise RuntimeError(f"claude reported {stream.result.get('subtype')!r}: "
                           f"{str(stream.result.get('errors') or '')[:800]}")
    if returncode != 0 and not result.text:
        raise RuntimeError(f"claude exited {returncode}: {stderr.strip()[-800:]}")
    return result
