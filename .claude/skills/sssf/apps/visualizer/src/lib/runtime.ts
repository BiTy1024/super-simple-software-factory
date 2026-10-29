/**
 * Coding-agent runtime → display label. The db stores the config value
 * (`pi`, `claude_code`); the UI says `claude-code`, the CLI's own name.
 */
export function runtimeLabel(runtime: string | null | undefined): string {
  if (!runtime) return ''
  return runtime === 'claude_code' ? 'claude-code' : runtime
}

/** Distinct runtimes of a session's agents, in a stable order. */
export function runtimesOf(agents: { coding_agent: string | null }[]): string[] {
  return [...new Set(agents.map((a) => a.coding_agent).filter((r): r is string => !!r))].toSorted()
}
