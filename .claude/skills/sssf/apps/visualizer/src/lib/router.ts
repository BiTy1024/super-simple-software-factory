import { ref } from 'vue'

// Hash routes: #/ → sessions · #/<adw_id> → waterfall · #/<adw_id>/<phase_id> → phase panel open
// · #/usage → cost and tool rollups across runs (adw_ids are hex, so no collision)
export interface Route {
  adwId: string | null
  phaseId: string | null
  usage: boolean
}

export const USAGE_HREF = '#/usage'

function parse(): Route {
  const parts = window.location.hash
    .replace(/^#\/?/, '')
    .split('/')
    .filter(Boolean)
    .map(decodeURIComponent)
  if (parts[0] === 'usage') return { adwId: null, phaseId: null, usage: true }
  return { adwId: parts[0] ?? null, phaseId: parts[1] ?? null, usage: false }
}

const route = ref<Route>(parse())

window.addEventListener('hashchange', () => {
  route.value = parse()
})

export function useRoute() {
  return route
}

// Display name for the phase crumb — set by the trace view once phases load,
// since the phase_id in the URL is not the display name.
export const phaseCrumb = ref<string | null>(null)

export function hrefFor(adwId?: string | null, phaseId?: string | null): string {
  let h = '#/'
  if (adwId) h += encodeURIComponent(adwId)
  if (adwId && phaseId) h += `/${encodeURIComponent(phaseId)}`
  return h
}

export function navigate(adwId?: string | null, phaseId?: string | null): void {
  window.location.hash = hrefFor(adwId, phaseId)
}
