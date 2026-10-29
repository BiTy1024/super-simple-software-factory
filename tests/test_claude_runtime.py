"""Claude Code runtime: stream parsing, command line, config, and an end-to-end
ADW run against a replayed stream (tests/fixtures/fake_claude.py) — zero cost.

    uv run --with pydantic --with pyyaml --with python-dotenv --with rich \\
        python -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "skills" / "sssf"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_CLAUDE = FIXTURES / "fake_claude.py"
STREAM = FIXTURES / "claude_stream.jsonl"

sys.path.insert(0, str(SKILL / "templates" / "adws"))

from adw_modules import agent_cc  # noqa: E402
from adw_modules.data_types import ClaudeCodeOptions, ClaudeRequest  # noqa: E402


def replay(path: Path = STREAM) -> tuple[agent_cc.StreamAccumulator, list[dict]]:
    stream, records = agent_cc.StreamAccumulator(), []
    for line in path.read_text().splitlines():
        records += stream.observe(json.loads(line.replace("SESSION", "s-1")))
    return stream, records


class StreamAccumulatorTest(unittest.TestCase):
    def setUp(self):
        self.stream, self.records = replay()
        self.usage, self.rollups = self.stream.totals()

    def test_one_api_message_counts_once(self):
        # msg_1 and msg_2 arrive as two lines each with repeated usage; the
        # last line's output_tokens is the final one.
        self.assertEqual(self.usage.input_tokens, 10 + 5 + 100 + 3)
        self.assertEqual(self.usage.output_tokens, 12 + 30 + 10 + 40)
        self.assertEqual(self.usage.cache_read_tokens, 1000 + 1100)
        self.assertEqual(self.usage.cache_write_tokens, 1000 + 50)
        self.assertEqual(self.usage.total_tokens, 3360)

    def test_naive_per_line_sum_would_overcount(self):
        naive = 0
        for line in STREAM.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") == "assistant":
                u = event["message"]["usage"]
                naive += sum(u.get(k, 0) for k in ("input_tokens", "output_tokens",
                                                    "cache_read_input_tokens",
                                                    "cache_creation_input_tokens"))
        self.assertGreater(naive, self.usage.total_tokens)

    def test_total_cost_is_claude_codes_own(self):
        self.assertAlmostEqual(self.usage.total_cost, 0.0123)
        # priced from the table: sonnet + haiku buckets, 1h cache write at 2x input
        expected = (
            (10 * 2 + 12 * 10 + 1000 * 2.5) / 1e6          # msg_1, 5m write
            + (5 * 2 + 30 * 10 + 1000 * 0.2 + 50 * 4.0) / 1e6  # msg_2, 1h write
            + (100 * 0.1 + 10 * 0.5) / 1e6                  # msg_3, haiku subagent
            + (3 * 2 + 40 * 10 + 1100 * 0.2) / 1e6          # msg_4
        )
        self.assertAlmostEqual(self.usage.priced_cost, expected)
        self.assertAlmostEqual(self.rollups["by_model"]["claude-sonnet-5-5"]["cost"], 0.0120)
        self.assertAlmostEqual(self.rollups["by_model"]["claude-haiku-5-5"]["cost"], 0.0003)

    def test_tool_calls_carry_skill_and_subagent(self):
        by_tool = {r["tool"]: r for r in self.records}
        self.assertEqual(list(by_tool), ["skill", "bash", "grep", "agent"])
        self.assertEqual(by_tool["skill"]["skill"], "(none)")   # the Skill call itself
        self.assertEqual(by_tool["bash"]["skill"], "demo-skill")
        self.assertEqual(by_tool["bash"]["label"], "bash: ls -la")
        self.assertEqual(by_tool["bash"]["result_snippet"], "README.md")
        self.assertEqual(by_tool["grep"]["skill"], "demo-skill")  # inherited at spawn
        self.assertEqual(by_tool["grep"]["subagent"], {"id": "toolu_a1", "type": "Explore"})
        self.assertFalse(by_tool["grep"]["ok"])
        self.assertNotIn("subagent", by_tool["bash"])
        self.assertTrue(all(r["runtime"] == "claude_code" for r in self.records))

    def test_rollups(self):
        skills = self.rollups["by_skill"]
        self.assertEqual(skills["(none)"]["tokens"], 1022)
        self.assertEqual(skills["demo-skill"]["tokens"], 1085 + 110 + 1143)
        self.assertEqual(skills["(none)"]["tool_calls"], 1)
        self.assertEqual(skills["demo-skill"]["tool_calls"], 3)
        [sub] = self.rollups["subagents"]
        self.assertEqual((sub["type"], sub["tokens"], sub["tool_calls"], sub["model"]),
                         ("Explore", 110, 1, "claude-haiku-5-5"))

    def test_final_text_and_context(self):
        self.assertEqual(json.loads(self.stream.text)["summary"], "listed files")
        self.assertEqual(self.stream.context_tokens, 3 + 1100)
        self.assertEqual(self.stream.context_window(), 1_000_000)

    def test_merge_rollups_sums_sends(self):
        merged = agent_cc.merge_rollups({}, self.rollups)
        agent_cc.merge_rollups(merged, self.rollups)
        self.assertEqual(merged["by_skill"]["demo-skill"]["tool_calls"], 6)
        self.assertEqual(len(merged["subagents"]), 2)


class StreamOutputShortfallTest(unittest.TestCase):
    """Usage numbers recorded from a real `claude -p` Haiku run: the stream's
    lines report 24 output tokens, the result's modelUsage 694."""

    def test_output_comes_from_model_usage_and_prices_to_the_bill(self):
        stream, _ = replay(FIXTURES / "claude_stream_haiku.jsonl")
        usage, rollups = stream.totals()
        self.assertEqual((usage.input_tokens, usage.output_tokens,
                          usage.cache_read_tokens, usage.cache_write_tokens),
                         (8, 694, 4999, 3185))
        self.assertEqual(usage.total_tokens, 8 + 694 + 4999 + 3185)
        self.assertAlmostEqual(usage.priced_cost, 0.000795915)
        self.assertAlmostEqual(usage.total_cost, 0.000795915)
        self.assertEqual(rollups["by_skill"]["(none)"]["tokens"], usage.total_tokens)


class CommandTest(unittest.TestCase):
    def request(self, **overrides) -> ClaudeRequest:
        base = dict(prompt="do it", system_prompt="# You are the builder", model="anthropic/sonnet",
                    thinking="off", session_id="123e4567-e89b-12d3-a456-426614174000",
                    raw_output_path="/tmp/x.jsonl", tools=["read", "bash", "find", "ls",
                                                            "subagent_create", "WebFetch"])
        base.update(overrides)
        return ClaudeRequest(**base)

    def test_create_then_resume(self):
        cmd = agent_cc.build_command(self.request())
        self.assertIn("--session-id", cmd)
        self.assertNotIn("--resume", cmd)
        cmd = agent_cc.build_command(self.request(resume=True))
        self.assertIn("--resume", cmd)

    def test_mapping(self):
        cmd = agent_cc.build_command(self.request())
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Bash,Glob,WebFetch")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read,Bash,Glob,WebFetch")
        self.assertIn("--append-system-prompt=# You are the builder", cmd)
        self.assertNotIn("do it", cmd)          # the prompt goes on stdin
        self.assertEqual(agent_cc.map_tools(["bash", "subagent_create"])[1], ["subagent_create"])

    def test_options(self):
        opts = ClaudeCodeOptions(system_prompt_mode="replace", bare=True, max_budget_usd=0.5,
                                 setting_sources="project", extra_args=["--fallback-model", "haiku"])
        cmd = agent_cc.build_command(self.request(options=opts, tools=None))
        self.assertIn("--system-prompt=# You are the builder", cmd)
        self.assertIn("--bare", cmd)
        self.assertNotIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "0.5")
        self.assertEqual(cmd[-2:], ["--fallback-model", "haiku"])

    def test_empty_allowlist_disables_tools(self):
        cmd = agent_cc.build_command(self.request(tools=[]))
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", cmd)


class EndToEndTest(unittest.TestCase):
    """adw_prompt on a claude_code agent, replayed — the zero-cost smoke run."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="sssf-cc-"))
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True,  # noqa: E731
                                        capture_output=True)
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "init")
        subprocess.run([sys.executable, str(SKILL / "scripts" / "install.py")],
                       cwd=self.repo, check=True, capture_output=True)
        config = self.repo / "adws" / "adw_sssf_config" / "sssf.config.yaml"
        text = config.read_text().replace("  - name: scout\n",
                                          "  - name: scout\n    coding_agent: claude_code\n"
                                          "    model: haiku\n", 1)
        config.write_text(text)
        # Outside the repo: the scout is read-only, and the factory would
        # (rightly) fail the phase for a file appearing in the tree.
        self.scratch = Path(tempfile.mkdtemp(prefix="sssf-cc-argv-"))
        self.argv_log = self.scratch / "argv.jsonl"

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.scratch, ignore_errors=True)

    def run_adw(self) -> subprocess.CompletedProcess:
        env = {**os.environ, "CLAUDE_PATH": str(FAKE_CLAUDE),
               "FAKE_CLAUDE_ARGV": str(self.argv_log), "ENGINEER_NAME": "tester"}
        return subprocess.run([sys.executable, "adws/adw_prompt.py", "--agent", "scout",
                               "--adw-id", "e2e00001", "list the files"],
                              cwd=self.repo, env=env, capture_output=True, text=True, timeout=120)

    def test_run_lands_in_sssf_db(self):
        proc = self.run_adw()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        db = sqlite3.connect(self.repo / "adws" / "adw_data" / "sssf.db")
        status, cost, tokens = db.execute(
            "SELECT status, total_cost, total_tokens FROM sessions WHERE adw_id='e2e00001'").fetchone()
        self.assertEqual(status, "success")
        self.assertAlmostEqual(cost, 0.0123)
        self.assertEqual(tokens, 3360)
        coding_agent, session_id, window = db.execute(
            "SELECT coding_agent, session_id, context_window FROM agent_sessions"
            " WHERE agent='scout'").fetchone()
        self.assertEqual(coding_agent, "claude_code")
        self.assertEqual(len(session_id), 36)
        self.assertEqual(window, 1_000_000)
        tools = [json.loads(p)["tool"] for (p,) in db.execute(
            "SELECT payload_json FROM events WHERE type='tool_call' ORDER BY rowid")]
        self.assertEqual(tools, ["skill", "bash", "grep", "agent"])
        end = json.loads(db.execute(
            "SELECT payload_json FROM events WHERE type='agent_end'").fetchone()[0])
        self.assertEqual(end["runtime"], "claude_code")
        self.assertIn("demo-skill", end["by_skill"])
        self.assertEqual(end["subagents"][0]["type"], "Explore")
        call = json.loads(self.argv_log.read_text().splitlines()[0])
        self.assertIn("list the files", call["stdin"])
        self.assertEqual(call["argv"][call["argv"].index("--tools") + 1], "Read,Grep,Glob,Bash,Write")
        start = json.loads(db.execute(
            "SELECT payload_json FROM events WHERE type='agent_start'").fetchone()[0])
        self.assertEqual(start["dropped_extensions"],
                         ["adws/adw_data/harness_engineering/subagents.ts"])
        self.assertNotIn("--plugin-dir", call["argv"])

    def test_rejoined_session_resumes(self):
        self.assertEqual(self.run_adw().returncode, 0)
        self.assertEqual(self.run_adw().returncode, 0)
        first, second = (json.loads(l)["argv"] for l in self.argv_log.read_text().splitlines())
        self.assertIn("--session-id", first)
        self.assertIn("--resume", second)
        self.assertEqual(first[first.index("--session-id") + 1],
                         second[second.index("--resume") + 1])

    def test_missing_cli_fails_validation(self):
        env = {**os.environ, "CLAUDE_PATH": str(self.repo / "no-such-claude")}
        proc = subprocess.run([sys.executable, "adws/adw_prompt.py", "--agent", "scout", "x"],
                              cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("claude_code needs the `claude` CLI", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
