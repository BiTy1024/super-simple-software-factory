/**
 * Usage rollups across sessions: cost per day, model, skill, runtime and run,
 * and tool calls per agent and per skill.
 *
 * Everything is derived from what the tracers already write — `agent_end`
 * payloads for cost, `tool_call` payloads for tools — so a db written by any
 * SSSF version reports without a migration. Fields only a claude_code agent
 * writes (by_model, by_skill, skill, subagent) are used when present; a pi
 * agent falls back to its configured model and the "(none)" skill.
 */
import type { Database } from "bun:sqlite";
import type {
  AgentEndPayload,
  AgentStartPayload,
  CostRow,
  ToolCount,
  UsageReport,
  UsageRun,
} from "../shared/types.ts";

export const NO_SKILL = "(none)";

/** ISO date `days` ago, compared lexicographically against the ISO columns. */
function since(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString();
}

function parse<T>(raw: string | null): T | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

class Rollup {
  private readonly rows = new Map<string, CostRow>();

  add(key: string, cost: number, tokens: number, calls = 1): void {
    const row = this.rows.get(key) ?? { key, cost: 0, tokens: 0, calls: 0 };
    row.cost += cost;
    row.tokens += tokens;
    row.calls += calls;
    this.rows.set(key, row);
  }

  byCost(): CostRow[] {
    return [...this.rows.values()].toSorted((a, b) => b.cost - a.cost || a.key.localeCompare(b.key));
  }

  byKey(): CostRow[] {
    return [...this.rows.values()].toSorted((a, b) => a.key.localeCompare(b.key));
  }
}

function keyOf(adw: string, agent: string): string {
  return `${adw}\u0000${agent}`;
}

function byCalls(a: ToolCount, b: ToolCount): number {
  return b.calls - a.calls || a.tool.localeCompare(b.tool);
}

export function usageReport(db: Database, days: number): UsageReport {
  const from = since(days);

  const sessions = db
    .query<
      {
        adw_id: string;
        adw_name: string | null;
        request: string | null;
        status: string | null;
        started_at: string | null;
        total_cost: number | null;
        total_tokens: number | null;
      },
      [string]
    >(
      `SELECT adw_id, adw_name, request, status, started_at, total_cost, total_tokens
         FROM sessions WHERE started_at >= ? ORDER BY started_at DESC, rowid DESC`,
    )
    .all(from);
  const inWindow = new Set(sessions.map((s) => s.adw_id));

  // Which runtime and model each (run, agent) used. agent_sessions is the
  // finished record; agent_start covers an agent that is still running.
  const agentInfo = new Map<string, { runtime: string | null; model: string | null }>();
  for (const row of db
    .query<{ adw_id: string; name: string; payload_json: string | null }, [string]>(
      `SELECT adw_id, name, payload_json FROM events
        WHERE type = 'agent_start' AND started_at >= ? ORDER BY rowid`,
    )
    .all(from)) {
    const p = parse<AgentStartPayload>(row.payload_json);
    agentInfo.set(keyOf(row.adw_id, row.name), {
      runtime: p?.coding_agent ?? null,
      model: p?.model ?? null,
    });
  }
  for (const row of db
    .query<{ adw_id: string; agent: string; coding_agent: string | null; model: string | null }, []>(
      "SELECT adw_id, agent, coding_agent, model FROM agent_sessions",
    )
    .all()) {
    if (!inWindow.has(row.adw_id)) continue;
    agentInfo.set(keyOf(row.adw_id, row.agent), { runtime: row.coding_agent, model: row.model });
  }

  const byDay = new Rollup();
  const byModel = new Rollup();
  const bySkill = new Rollup();
  const byRuntime = new Rollup();
  const runtimesByRun = new Map<string, Set<string>>();

  for (const row of db
    .query<
      { adw_id: string; name: string; payload_json: string | null; tokens: number | null; started_at: string },
      [string]
    >(
      `SELECT adw_id, name, payload_json, tokens, started_at FROM events
        WHERE type = 'agent_end' AND started_at >= ? ORDER BY rowid`,
    )
    .all(from)) {
    const p = parse<AgentEndPayload>(row.payload_json) ?? {};
    const cost = p.cost ?? p.usage?.total_cost ?? 0;
    const tokens = row.tokens ?? p.usage?.total_tokens ?? 0;
    const info = agentInfo.get(keyOf(row.adw_id, row.name));
    const runtime = p.runtime ?? info?.runtime ?? "pi";

    byDay.add(row.started_at.slice(0, 10), cost, tokens);
    byRuntime.add(runtime, cost, tokens);
    const runRuntimes = runtimesByRun.get(row.adw_id) ?? new Set<string>();
    runRuntimes.add(runtime);
    runtimesByRun.set(row.adw_id, runRuntimes);

    if (p.by_model && Object.keys(p.by_model).length) {
      for (const [model, cell] of Object.entries(p.by_model)) byModel.add(model, cell.cost, cell.tokens);
    } else {
      byModel.add(info?.model ?? "unknown", cost, tokens);
    }
    if (p.by_skill && Object.keys(p.by_skill).length) {
      for (const [skill, cell] of Object.entries(p.by_skill)) bySkill.add(skill, cell.cost, cell.tokens);
    } else {
      bySkill.add(NO_SKILL, cost, tokens);
    }
  }

  const runs: UsageRun[] = sessions.map((s) => ({
    adw_id: s.adw_id,
    adw_name: s.adw_name,
    request: s.request,
    status: s.status,
    started_at: s.started_at,
    cost: s.total_cost ?? 0,
    tokens: s.total_tokens ?? 0,
    runtimes: [...(runtimesByRun.get(s.adw_id) ?? [])].toSorted(),
  }));

  const toolRows = db
    .query<
      {
        adw_id: string;
        agent: string | null;
        tool: string | null;
        skill: string | null;
        subagent: string | null;
        calls: number;
        errors: number;
      },
      [string]
    >(
      `SELECT adw_id,
              json_extract(payload_json, '$.agent')         AS agent,
              json_extract(payload_json, '$.tool')          AS tool,
              json_extract(payload_json, '$.skill')         AS skill,
              json_extract(payload_json, '$.subagent.type') AS subagent,
              COUNT(*) AS calls,
              SUM(CASE WHEN json_extract(payload_json, '$.ok') = 0 THEN 1 ELSE 0 END) AS errors
         FROM events
        WHERE type = 'tool_call' AND started_at >= ?
        GROUP BY adw_id, agent, tool, skill, subagent`,
    )
    .all(from);

  const perAgent = new Map<string, ToolCount>();
  const perSkill = new Map<string, ToolCount>();
  let toolCalls = 0;
  for (const r of toolRows) {
    const tool = r.tool ?? "tool";
    const owner = r.agent ?? "?";
    const agent = r.subagent ? `${owner} › ${r.subagent}` : owner;
    const runtime = agentInfo.get(keyOf(r.adw_id, owner))?.runtime ?? null;
    const skill = r.skill ?? NO_SKILL;
    toolCalls += r.calls;

    const a = perAgent.get(`${agent}\u0000${runtime}\u0000${tool}`) ?? { agent, runtime, tool, calls: 0, errors: 0 };
    a.calls += r.calls;
    a.errors += r.errors;
    perAgent.set(`${agent}\u0000${runtime}\u0000${tool}`, a);

    const s = perSkill.get(`${skill}\u0000${tool}`) ?? { skill, tool, calls: 0, errors: 0 };
    s.calls += r.calls;
    s.errors += r.errors;
    perSkill.set(`${skill}\u0000${tool}`, s);
  }

  return {
    days,
    totals: {
      cost: runs.reduce((sum, r) => sum + r.cost, 0),
      tokens: runs.reduce((sum, r) => sum + r.tokens, 0),
      runs: runs.length,
      tool_calls: toolCalls,
    },
    by_day: byDay.byKey(),
    by_model: byModel.byCost(),
    by_skill: bySkill.byCost(),
    by_runtime: byRuntime.byCost(),
    runs,
    tools_by_agent: [...perAgent.values()].toSorted(byCalls),
    tools_by_skill: [...perSkill.values()].toSorted(byCalls),
  };
}
