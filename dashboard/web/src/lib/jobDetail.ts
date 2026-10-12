// What the details panel shows from the scorer's and the safety check's JSON. Those come from a model reading a posting:
// every field is checked for its type here, and anything unexpected is left out instead of breaking the panel.
import { DASH } from '@/lib/format'
import type { Tone } from '@/lib/status'

export const DESCRIPTION_LINES = 40
// a posting with no line breaks at all is collapsed too
const DESCRIPTION_CHARS = 4000

/** The start of a long description (about 40 lines), or null when it is short enough to show whole. */
export function descriptionPreview(text: string): string | null {
  const lines = text.split('\n')
  let preview = lines.length > DESCRIPTION_LINES ? lines.slice(0, DESCRIPTION_LINES).join('\n') : text
  if (preview.length > DESCRIPTION_CHARS) preview = preview.slice(0, DESCRIPTION_CHARS)
  return preview.length < text.length ? `${preview.trimEnd()}…` : null
}

export type Gate = { name: string; verdict: string; label: string; note: string; tone: Tone; failed: boolean }

const GATE_VERDICTS: Record<string, { label: string; tone: Tone }> = {
  PASS: { label: 'Pass', tone: 'success' },
  FAIL: { label: 'Fail', tone: 'danger' },
  FLAG: { label: 'Check', tone: 'warning' },
  UNKNOWN: { label: 'Unknown', tone: 'muted' },
}

/** The scorer's gates (eligibility, language, location) with their verdicts; a failed one is marked. */
export function gateList(gates: Record<string, unknown> | null | undefined): Gate[] {
  if (!gates || typeof gates !== 'object') return []
  return Object.entries(gates).flatMap(([name, gate]) => {
    if (!gate || typeof gate !== 'object') return []
    const { verdict, note } = gate as Record<string, unknown>
    if (typeof verdict !== 'string') return []
    const key = verdict.toUpperCase()
    const meta = GATE_VERDICTS[key] ?? { label: verdict, tone: 'muted' as Tone }
    return [{
      name: name.replace(/_/g, ' '), verdict: key, label: meta.label, tone: meta.tone,
      note: typeof note === 'string' ? note.trim() : '', failed: key === 'FAIL',
    }]
  })
}

export type Flag = { code: string; label: string; severity: number | null; detail: string }

/** "HIDDEN_CONTENT" as "Hidden content". */
function flagLabel(code: string): string {
  const words = code.replace(/_/g, ' ').trim().toLowerCase()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** The safety check's flags, the most severe first. */
export function flagList(flags: unknown[] | null | undefined): Flag[] {
  if (!Array.isArray(flags)) return []
  return flags
    .flatMap((flag) => {
      if (!flag || typeof flag !== 'object') return []
      const { code, severity, detail } = flag as Record<string, unknown>
      if (typeof code !== 'string' || !code.trim()) return []
      return [{
        code, label: flagLabel(code), severity: typeof severity === 'number' && Number.isFinite(severity) ? severity : null,
        detail: typeof detail === 'string' ? detail.trim() : '',
      }]
    })
    .sort((a, b) => (b.severity ?? 0) - (a.severity ?? 0))
}

/** Strengths and gaps: the strings only, without blanks. */
export function textList(items: unknown[] | null | undefined): string[] {
  if (!Array.isArray(items)) return []
  return items.filter((s): s is string => typeof s === 'string').map((s) => s.trim()).filter(Boolean)
}

const years = (n: number) => `${n} ${n === 1 ? 'year' : 'years'}`

/** The experience a posting asks for: "3–5 years", "3+ years", "Up to 5 years"; null when it says nothing. */
export function yearsAsked(min: number | null | undefined, max: number | null | undefined): string | null {
  const lo = typeof min === 'number' ? min : null
  const hi = typeof max === 'number' ? max : null
  if (lo !== null && hi !== null) return lo === hi ? years(lo) : `${lo}–${years(hi)}`
  if (lo !== null) return `${lo}+ years`
  if (hi !== null) return `Up to ${years(hi)}`
  return null
}

/** What tailoring cost: "$0.034"; "<$0.001" for a fraction of a tenth of a cent. */
export function formatCost(usd: number | null | undefined): string {
  if (typeof usd !== 'number' || !Number.isFinite(usd)) return DASH
  if (usd === 0) return '$0'
  return usd < 0.001 ? '<$0.001' : `$${usd.toFixed(3)}`
}

/** "Medium risk" for the safety check's level. */
export function riskLabel(risk: string): string {
  return `${risk.charAt(0).toUpperCase()}${risk.slice(1)} risk`
}

/** The jobs before and after `id` on the current page, and its place there (1-based); nulls when it is not on it. */
export function neighbours(ids: readonly number[], id: number | null): { prev: number | null; next: number | null; position: number | null } {
  const i = id === null ? -1 : ids.indexOf(id)
  if (i < 0) return { prev: null, next: null, position: null }
  return { prev: ids[i - 1] ?? null, next: ids[i + 1] ?? null, position: i + 1 }
}
