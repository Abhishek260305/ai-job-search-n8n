// The numbers behind the donut and the funnel, kept out of the chart components so they can be checked directly.
import type { Summary } from '@/api/types'
import { DASH, formatCount } from '@/lib/format'
import { DONUT_SLICES } from '@/lib/status'

export type DonutSlice = { key: string; label: string; color: string; count: number }

/** Every submitted status in pipeline order (zeros too, for the legend), plus the shortlist when asked for. */
export function donutSlices(byStatus: Summary['by_status'], includeNotApplied: boolean): DonutSlice[] {
  return DONUT_SLICES
    .filter((s) => includeNotApplied || s.key !== 'not_applied')
    .map((s) => ({ ...s, count: byStatus[s.key] ?? 0 }))
}

export function totalOf(slices: DonutSlice[]): number {
  return slices.reduce((sum, s) => sum + s.count, 0)
}

/** What a screen reader hears instead of the ring. */
export function donutDescription(slices: DonutSlice[]): string {
  const parts = slices.map((s) => `${s.label} ${formatCount(s.count)}`).join(', ')
  return `Applications by status: ${parts}. ${formatCount(totalOf(slices))} in total.`
}

const STEP_LABELS: Record<string, string> = {
  found: 'Found',
  passed_filters: 'Passed filters',
  scored: 'Scored',
  shortlisted: 'Shortlisted',
  applied: 'Applied',
  interview: 'Interview',
  offer: 'Offer',
}

/** `count` as a share of `previous`: "49%", "<1%" for a small non-zero share, "—" when there was nothing before. */
export function share(count: number, previous: number): string {
  if (!(previous > 0)) return DASH
  const pct = (count / previous) * 100
  if (count > 0 && pct < 1) return '<1%'
  return `${Math.round(pct)}%`
}

export type FunnelRow = { step: string; label: string; count: number; share: string | null; text: string }

/** One row per step, in the order the API sends them; the first step has no share. */
export function funnelRows(funnel: Summary['funnel']): FunnelRow[] {
  return funnel.map((f, i) => {
    const pct = i === 0 ? null : share(f.count, funnel[i - 1].count)
    const label = STEP_LABELS[f.step] ?? f.step.replace(/_/g, ' ')
    return { step: f.step, label, count: f.count, share: pct, text: pct === null ? formatCount(f.count) : `${formatCount(f.count)} · ${pct}` }
  })
}

export function funnelDescription(rows: FunnelRow[]): string {
  const parts = rows.map((r) => (r.share === null ? `${r.label} ${formatCount(r.count)}` : `${r.label} ${formatCount(r.count)} (${r.share})`))
  return `Where jobs drop out, each step as a share of the one before: ${parts.join(', ')}.`
}

export function thresholdNote(threshold: number | null): string {
  return threshold === null
    ? 'Shortlisted uses your current score threshold.'
    : `Shortlisted uses your current threshold (${threshold}).`
}