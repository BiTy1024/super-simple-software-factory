import { Database } from "bun:sqlite";
import { beforeEach, describe, expect, it } from "bun:test";
import { usageReport } from "./usage.ts";

// The columns usage.ts reads, as tracer.py creates them.
const SCHEMA = `
CREATE TABLE sessions (adw_id TEXT PRIMARY KEY, adw_name TEXT, request TEXT, status TEXT,
  engineer TEXT, started_at TEXT, ended_at TEXT, total_tokens INTEGER DEFAULT 0,
  total_cost REAL DEFAULT 0, archived INTEGER DEFAULT 0);
CREATE TABLE events (event_id TEXT PRIMARY KEY, adw_id TEXT, phase_id TEXT, parent_id TEXT,
  type TEXT, name TEXT, payload_json TEXT, tokens INTEGER, started_at TEXT, ended_at TEXT);
CREATE TABLE agent_sessions (adw_id TEXT, agent TEXT, coding_agent TEXT, model TEXT, color TEXT,
  session_id TEXT, context_tokens INTEGER, context_window INTEGER, created_at TEXT,
  last_used_at TEXT, PRIMARY KEY (adw_id, agent));
`;

const NOW = new Date().toISOString();
const TODAY = NOW.slice(0, 10);
let seq = 0;

function event(db: Database, adw: string, type: string, name: string, payload: object, tokens?: number) {
  seq += 1;
  db.query(
    "INSERT INTO events (event_id, adw_id, type, name, payload_json, tokens, started_at) VALUES (?,?,?,?,?,?,?)",
  ).run(`evt_${seq}`, adw, type, name, JSON.stringify(payload), tokens ?? null, NOW);
}

/** One pi run (builder) and one claude_code run (scout with a skill and a subagent). */
function seed(db: Database) {
  db.query("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,0)").run(
    "aaaa0001", "adw_build", "add a thing", "success", "t", NOW, NOW, 1000, 0.5);
  db.query("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,0)").run(
    "bbbb0002", "adw_prompt", "list files", "success", "t", NOW, NOW, 3360, 0.0123);
  db.query("INSERT INTO agent_sessions (adw_id, agent, coding_agent, model) VALUES (?,?,?,?)").run(
    "aaaa0001", "builder", "pi", "google/gemini-3.6-flash");
  db.query("INSERT INTO agent_sessions (adw_id, agent, coding_agent, model) VALUES (?,?,?,?)").run(
    "bbbb0002", "scout", "claude_code", "haiku");

  event(db, "aaaa0001", "agent_start", "builder", { model: "google/gemini-3.6-flash", coding_agent: "pi" });
  event(db, "aaaa0001", "tool_call", "bash: ls", { tool: "bash", ok: true, agent: "builder" });
  event(db, "aaaa0001", "tool_call", "bash: x", { tool: "bash", ok: false, agent: "builder" });
  event(db, "aaaa0001", "agent_end", "builder", { cost: 0.5 }, 1000);

  event(db, "bbbb0002", "agent_start", "scout", { model: "haiku", coding_agent: "claude_code" });
  event(db, "bbbb0002", "tool_call", "bash: ls", { tool: "bash", ok: true, agent: "scout", skill: "demo-skill" });
  event(db, "bbbb0002", "tool_call", "grep: x", {
    tool: "grep", ok: false, agent: "scout", skill: "demo-skill", subagent: { id: "toolu_a1", type: "Explore" },
  });
  event(db, "bbbb0002", "agent_end", "scout", {
    cost: 0.0123,
    runtime: "claude_code",
    by_model: { "claude-sonnet-5-5": { tokens: 3250, cost: 0.012 }, "claude-haiku-5-5": { tokens: 110, cost: 0.0003 } },
    by_skill: { "(none)": { tokens: 1022, cost: 0.003 }, "demo-skill": { tokens: 2338, cost: 0.0093, tool_calls: 2 } },
  }, 3360);
}

describe("usageReport", () => {
  let db: Database;

  beforeEach(() => {
    db = new Database(":memory:");
    db.exec(SCHEMA);
    seed(db);
  });

  it("rolls cost up per runtime, falling back to the agent's row for pi", () => {
    // Act
    const report = usageReport(db, 30);

    // Assert
    expect(report.by_runtime).toEqual([
      { key: "pi", cost: 0.5, tokens: 1000, calls: 1 },
      { key: "claude_code", cost: 0.0123, tokens: 3360, calls: 1 },
    ]);
    expect(report.totals).toEqual({ cost: 0.5123, tokens: 4360, runs: 2, tool_calls: 4 });
  });

  it("uses claude_code's per-model rollup and the configured model for pi", () => {
    // Act
    const keys = usageReport(db, 30).by_model.map((r) => r.key);

    // Assert
    expect(keys).toEqual(["google/gemini-3.6-flash", "claude-sonnet-5-5", "claude-haiku-5-5"]);
  });

  it("puts pi spend under (none) and claude_code spend under its skills", () => {
    // Act
    const skills = Object.fromEntries(usageReport(db, 30).by_skill.map((r) => [r.key, r.cost]));

    // Assert
    expect(skills).toHaveProperty("(none)", 0.503);
    expect(skills).toHaveProperty("demo-skill", 0.0093);
  });

  it("buckets cost by day", () => {
    // Act
    const days = usageReport(db, 30).by_day;

    // Assert
    expect(days).toHaveLength(1);
    expect(days[0]).toHaveProperty("key", TODAY);
  });

  it("counts tools per agent with subagents and runtimes kept apart", () => {
    // Act
    const rows = usageReport(db, 30).tools_by_agent;

    // Assert
    expect(rows).toContainEqual({ agent: "builder", runtime: "pi", tool: "bash", calls: 2, errors: 1 });
    expect(rows).toContainEqual({ agent: "scout › Explore", runtime: "claude_code", tool: "grep", calls: 1, errors: 1 });
    expect(rows).toContainEqual({ agent: "scout", runtime: "claude_code", tool: "bash", calls: 1, errors: 0 });
  });

  it("builds the skill × tool cells", () => {
    // Act
    const cells = usageReport(db, 30).tools_by_skill;

    // Assert
    expect(cells).toContainEqual({ skill: "(none)", tool: "bash", calls: 2, errors: 1 });
    expect(cells).toContainEqual({ skill: "demo-skill", tool: "bash", calls: 1, errors: 0 });
    expect(cells).toContainEqual({ skill: "demo-skill", tool: "grep", calls: 1, errors: 1 });
  });

  it("lists each run with the runtimes it used", () => {
    // Act
    const runs = usageReport(db, 30).runs;

    // Assert
    expect(runs.map((r) => [r.adw_id, r.runtimes])).toEqual([
      ["bbbb0002", ["claude_code"]],
      ["aaaa0001", ["pi"]],
    ]);
  });
});
