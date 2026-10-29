<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import type { CostRow, ToolCount, UsageReport } from '../lib/types'
import { fetchUsage } from '../lib/api'
import { fmtCost, fmtDate, fmtTokens } from '../lib/format'
import { hrefFor } from '../lib/router'
import { modelName } from '../lib/models'
import RuntimeBadge from './RuntimeBadge.vue'
import StatChip from './StatChip.vue'

const WINDOWS = [7, 30, 90, 365]
const days = ref(30)
const report = shallowRef<UsageReport | null>(null)
const apiError = ref<string | null>(null)

// Rollups move slowly next to a live trace — a 5s poll keeps a running
// run's cost current without re-aggregating the whole db twice a second.
let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function load() {
  if (inflight) return
  inflight = true
  try {
    report.value = await fetchUsage(days.value)
    apiError.value = null
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void load()
  timer = setInterval(() => void load(), 5000)
})
onUnmounted(() => clearInterval(timer))
watch(days, () => void load())

/** Bar width relative to the largest row of the same rollup. */
function bars(rows: CostRow[]): { row: CostRow; pct: number }[] {
  const max = Math.max(...rows.map((r) => r.cost), 0)
  return rows.map((row) => ({ row, pct: max > 0 ? Math.max((row.cost / max) * 100, 1) : 0 }))
}

const rollups = computed(() => {
  const r = report.value
  if (!r) return []
  return [
    { title: 'cost by day', rows: bars(r.by_day), label: (k: string) => k, badge: false },
    { title: 'cost by model', rows: bars(r.by_model), label: (k: string) => modelName(k), badge: false },
    { title: 'cost by skill', rows: bars(r.by_skill), label: (k: string) => k, badge: false },
    { title: 'cost by runtime', rows: bars(r.by_runtime), label: (k: string) => k, badge: true },
  ]
})

interface ToolGroup {
  owner: string
  runtime: string | null
  calls: number
  tools: ToolCount[]
}

/** Group tool counts by owner; `keyOf` may separate owners that share a name
 *  (one agent name on two runtimes), `ownerOf` is what the row shows. */
function group(
  rows: ToolCount[],
  ownerOf: (t: ToolCount) => string,
  keyOf: (t: ToolCount) => string = ownerOf,
): ToolGroup[] {
  const groups = new Map<string, ToolGroup>()
  for (const t of rows) {
    const key = keyOf(t)
    const g = groups.get(key) ?? { owner: ownerOf(t), runtime: t.runtime ?? null, calls: 0, tools: [] }
    g.calls += t.calls
    g.tools.push(t)
    groups.set(key, g)
  }
  return [...groups.values()].toSorted((a, b) => b.calls - a.calls)
}

const byAgent = computed(() =>
  group(
    report.value?.tools_by_agent ?? [],
    (t) => t.agent ?? '?',
    (t) => `${t.agent ?? '?'}\u0000${t.runtime ?? ''}`,
  ),
)
const bySkill = computed(() => group(report.value?.tools_by_skill ?? [], (t) => t.skill ?? '(none)'))

// Skill × tool: rows are skills, columns are tools, both ordered by volume.
const matrix = computed(() => {
  const rows = report.value?.tools_by_skill ?? []
  const toolTotals = new Map<string, number>()
  for (const r of rows) toolTotals.set(r.tool, (toolTotals.get(r.tool) ?? 0) + r.calls)
  const tools = [...toolTotals.entries()].toSorted((a, b) => b[1] - a[1]).map(([t]) => t)
  const cells = new Map(rows.map((r) => [`${r.skill}\u0000${r.tool}`, r]))
  const max = Math.max(...rows.map((r) => r.calls), 1)
  return {
    tools,
    skills: bySkill.value.map((g) => g.owner),
    cell: (skill: string, tool: string) => cells.get(`${skill}\u0000${tool}`),
    heat: (calls: number) => `rgba(90, 210, 221, ${0.08 + 0.5 * (calls / max)})`,
  }
})
</script>

<template>
  <div class="usage">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div class="u-head">
      <span class="u-window">
        <button
          v-for="d in WINDOWS"
          :key="d"
          type="button"
          :class="{ on: d === days }"
          @click="days = d"
        >
          {{ d }}d
        </button>
      </span>
      <template v-if="report">
        <StatChip kind="cost" :value="report.totals.cost" />
        <StatChip kind="tokens" :value="report.totals.tokens" />
        <span class="dim">{{ report.totals.runs }} runs · {{ report.totals.tool_calls }} tool calls</span>
      </template>
    </div>

    <div v-if="report && !report.totals.runs" class="empty-state">no runs in the last {{ days }} days</div>

    <template v-else-if="report">
      <div class="u-grid">
        <section v-for="r in rollups" :key="r.title" class="u-panel">
          <h3>{{ r.title }}</h3>
          <div v-for="{ row, pct } in r.rows" :key="row.key" class="bar-row" :title="row.key">
            <span class="bar-key">
              <RuntimeBadge v-if="r.badge" :runtime="row.key" />
              <template v-else>{{ r.label(row.key) }}</template>
            </span>
            <span class="bar-track"><span class="bar-fill" :style="{ width: `${pct}%` }" /></span>
            <span class="bar-val">{{ fmtCost(row.cost) }}</span>
            <span class="bar-sub faint">{{ fmtTokens(row.tokens) }}</span>
          </div>
          <div v-if="!r.rows.length" class="faint">nothing yet</div>
        </section>
      </div>

      <section class="u-panel wide">
        <h3>skill × tool</h3>
        <div v-if="!matrix.tools.length" class="faint">no tool calls yet</div>
        <div v-else class="matrix-wrap">
          <table class="matrix">
            <thead>
              <tr>
                <th />
                <th v-for="t in matrix.tools" :key="t">{{ t }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in matrix.skills" :key="s">
                <th>{{ s }}</th>
                <td
                  v-for="t in matrix.tools"
                  :key="t"
                  :style="matrix.cell(s, t) ? { background: matrix.heat(matrix.cell(s, t)!.calls) } : {}"
                  :title="matrix.cell(s, t) ? `${s} · ${t}: ${matrix.cell(s, t)!.calls} calls, ${matrix.cell(s, t)!.errors} failed` : ''"
                >
                  <template v-if="matrix.cell(s, t)">
                    {{ matrix.cell(s, t)!.calls }}
                    <span v-if="matrix.cell(s, t)!.errors" class="t-red err">{{ matrix.cell(s, t)!.errors }}✗</span>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <div class="u-grid two">
        <section class="u-panel">
          <h3>tools per agent</h3>
          <div v-for="g in byAgent" :key="`${g.owner}-${g.runtime}`" class="tool-group">
            <span class="tg-owner">{{ g.owner }} <RuntimeBadge :runtime="g.runtime" /></span>
            <span class="tg-tools">
              <span v-for="t in g.tools" :key="t.tool" class="tool-chip" :class="{ bad: t.errors }"
                >{{ t.tool }} {{ t.calls }}<template v-if="t.errors"> · {{ t.errors }}✗</template></span
              >
            </span>
          </div>
          <div v-if="!byAgent.length" class="faint">no tool calls yet</div>
        </section>
        <section class="u-panel">
          <h3>tools per skill</h3>
          <div v-for="g in bySkill" :key="g.owner" class="tool-group">
            <span class="tg-owner">{{ g.owner }}</span>
            <span class="tg-tools">
              <span v-for="t in g.tools" :key="t.tool" class="tool-chip" :class="{ bad: t.errors }"
                >{{ t.tool }} {{ t.calls }}<template v-if="t.errors"> · {{ t.errors }}✗</template></span
              >
            </span>
          </div>
          <div v-if="!bySkill.length" class="faint">no tool calls yet</div>
        </section>
      </div>

      <section class="u-panel wide">
        <h3>cost per run</h3>
        <table class="runs">
          <thead>
            <tr>
              <th>run</th>
              <th>workflow</th>
              <th>runtime</th>
              <th>started</th>
              <th class="num">cost</th>
              <th class="num">tokens</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in report.runs" :key="r.adw_id">
              <td><a :href="hrefFor(r.adw_id)" class="mono">{{ r.adw_id }}</a></td>
              <td class="ellip" :title="r.request ?? ''">{{ r.adw_name ?? '—' }}</td>
              <td><RuntimeBadge v-for="rt in r.runtimes" :key="rt" :runtime="rt" /></td>
              <td class="dim">{{ fmtDate(r.started_at) }}</td>
              <td class="num">{{ fmtCost(r.cost) }}</td>
              <td class="num dim">{{ fmtTokens(r.tokens) }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
    <div v-else-if="!apiError" class="empty-state">loading usage…</div>
  </div>
</template>

<style scoped>
.usage {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 18px 24px 28px;
}

.u-head {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.u-window {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 999px;
  overflow: hidden;
}

.u-window button {
  padding: 4px 14px;
  border: 0;
  background: transparent;
  color: var(--dim);
  font-family: var(--mono);
  font-size: 15px;
  cursor: pointer;
}

.u-window button.on {
  background: var(--panel-2);
  color: var(--text);
}

.u-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 18px;
}

.u-grid.two {
  grid-template-columns: repeat(auto-fill, minmax(520px, 1fr));
}

.u-panel {
  padding: 16px 18px;
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
  min-width: 0;
}

.u-panel h3 {
  margin: 0 0 12px;
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--dim);
}

.bar-row {
  display: grid;
  grid-template-columns: minmax(90px, 32%) 1fr auto auto;
  align-items: center;
  gap: 10px;
  padding: 3px 0;
}

.bar-key {
  font-family: var(--mono);
  font-size: 15px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bar-track {
  height: 8px;
  border-radius: 999px;
  background: var(--panel-3);
  overflow: hidden;
}

.bar-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, rgba(200, 155, 255, 0.6), var(--purple));
}

.bar-val {
  font-family: var(--mono);
  font-size: 15px;
  text-align: right;
}

.bar-sub {
  font-family: var(--mono);
  font-size: 14px;
  min-width: 52px;
  text-align: right;
}

.matrix-wrap {
  overflow-x: auto;
}

.matrix {
  border-collapse: separate;
  border-spacing: 3px;
  font-family: var(--mono);
  font-size: 14px;
}

.matrix th {
  font-weight: 400;
  color: var(--dim);
  padding: 4px 8px;
  white-space: nowrap;
}

.matrix thead th {
  text-align: center;
}

.matrix tbody th {
  text-align: left;
}

.matrix td {
  min-width: 52px;
  padding: 4px 8px;
  border-radius: 6px;
  text-align: center;
  background: var(--panel-3);
}

.err {
  font-size: 12px;
  margin-left: 4px;
}

.tool-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 0;
  border-top: 1px solid var(--border-soft);
}

.tool-group:first-of-type {
  border-top: 0;
}

.tg-owner {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: var(--mono);
  font-size: 15px;
}

.tg-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.tool-chip {
  padding: 1px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-family: var(--mono);
  font-size: 14px;
  color: var(--dim);
}

.tool-chip.bad {
  border-color: rgba(255, 111, 103, 0.45);
}

.runs {
  width: 100%;
  border-collapse: collapse;
  font-size: 15px;
}

.runs th {
  text-align: left;
  font-weight: 400;
  color: var(--faint);
  padding: 4px 8px;
}

.runs td {
  padding: 5px 8px;
  border-top: 1px solid var(--border-soft);
}

.runs td :deep(.runtime) {
  margin-right: 4px;
}

.num {
  text-align: right;
  font-family: var(--mono);
}

.mono {
  font-family: var(--mono);
}

.ellip {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 600px) {
  .usage {
    padding: 14px 16px 24px;
  }

  .u-grid,
  .u-grid.two {
    grid-template-columns: 1fr;
  }
}
</style>
