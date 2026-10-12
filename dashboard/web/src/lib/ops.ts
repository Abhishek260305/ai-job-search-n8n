// The health and spend panels' rules, apart from the components so they can be checked directly.
import type { DailySpend, ScheduleRow, Spend } from '@/api/types'
import { DASH } from '@/lib/format'

const STAGES: Record<string, string> = {
  collect: 'Collect jobs', score: 'Score jobs', tailor: 'Tailor applications', digest: 'Send the digest',
}

export function stageLabel(name: string): string {
  return STAGES[name] ?? name.replace(/_/g, ' ')
}

/** "every 6 h", "every 30 min", "daily at 08:00", or "off". */
export function cadence(s: Pick<ScheduleRow, 'enabled' | 'every_minutes' | 'at_time'>): string {
  if (!s.enabled) return 'off'
  if (s.every_minutes) {
    const m = s.every_minutes
    return m >= 60 && m % 60 === 0 ? `every ${m / 60} h` : `every ${m} min`
  }
  if (s.at_time) return `daily at ${s.at_time.slice(0, 5)}`
  return DASH
}

const money = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 3 })

/** "$10.00", "$0.53", and "$0.016" for the fractions of a cent a single scoring run costs. */
export function formatMoney(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? money.format(value) : DASH
}

export type MeterTone = 'ok' | 'warn' | 'bad'

/** Spend against a cap. `blocked` (the server's verdict, at or over a cap) always wins; `warnPct` is the amber line. */
export function meter(spent: number, cap: number, warnPct: number | null, blocked = false): { pct: number; width: number; tone: MeterTone } {
  // the tone uses the exact share: 99.6% rounds to "100%" on screen but the cap is not reached yet
  const exact = cap > 0 ? (spent / cap) * 100 : spent > 0 ? 100 : 0
  const pct = Math.round(exact)
  const tone: MeterTone = blocked || exact >= 100 ? 'bad' : warnPct !== null && exact >= warnPct ? 'warn' : 'ok'
  return { pct, width: Math.max(0, Math.min(100, pct)), tone }
}

// Okabe-Ito colours, which stay apart for colour-blind eyes (the same family as the status donut).
const PALETTE = ['#0072B2', '#E69F00', '#009E73', '#CC79A7', '#56B4E9', '#D55E00', '#999999']
const PURPOSES: Record<string, string> = { score: 'Scoring', tailor: 'Tailoring', verify: 'Fact check', search: 'Search' }

export type Series = { key: string; label: string; color: string }

export function seriesKey(d: Pick<DailySpend, 'provider' | 'purpose'>): string {
  return d.provider === 'anthropic' ? d.purpose : `${d.provider}:${d.purpose}`
}

function seriesLabel(d: Pick<DailySpend, 'provider' | 'purpose'>): string {
  const purpose = PURPOSES[d.purpose] ?? d.purpose.replace(/_/g, ' ')
  if (d.provider === 'anthropic') return purpose
  return d.provider === 'apify' ? 'Apify' : `${d.provider} ${purpose}`
}

export type DayPoint = { day: string; label: string; total: number } & Record<string, number | string>

const dayLabel = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })

/** The calendar date of an instant in a time zone, as YYYY-MM-DD. */
export function dateIn(instant: string | Date, timeZone: string): string {
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(instant))
  } catch {
    return new Date(instant).toISOString().slice(0, 10)
  }
}

function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + n)
  return d.toISOString().slice(0, 10)
}

/**
 * One point per day for the last `days` days ending today (zero-spend days included, so the bars keep their place),
 * a series per provider and purpose, and the total. Series are Claude's purposes alphabetically, then the others.
 */
export function chartData(spend: Pick<Spend, 'daily' | 'generated_at' | 'timezone'>, days = 30): { points: DayPoint[]; series: Series[]; total: number } {
  const today = dateIn(spend.generated_at, spend.timezone)
  const seen = new Map<string, string>()
  for (const d of spend.daily) if (!seen.has(seriesKey(d))) seen.set(seriesKey(d), seriesLabel(d))
  const keys = [...seen.keys()].sort((a, b) => Number(a.includes(':')) - Number(b.includes(':')) || a.localeCompare(b))
  const series = keys.map((key, i) => ({ key, label: seen.get(key)!, color: PALETTE[i % PALETTE.length] }))

  const byDay = new Map<string, Record<string, number>>()
  for (const d of spend.daily) {
    const row = byDay.get(d.day) ?? {}
    row[seriesKey(d)] = (row[seriesKey(d)] ?? 0) + d.cost_usd
    byDay.set(d.day, row)
  }
  let total = 0
  const points: DayPoint[] = []
  for (let i = days - 1; i >= 0; i--) {
    const day = addDays(today, -i)
    const row = byDay.get(day) ?? {}
    const point: DayPoint = { day, label: dayLabel.format(new Date(`${day}T00:00:00Z`)), total: 0 }
    for (const k of keys) {
      const v = Math.round((row[k] ?? 0) * 1e6) / 1e6
      point[k] = v
      point.total += v
    }
    point.total = Math.round(point.total * 1e6) / 1e6
    total += point.total
    points.push(point)
  }
  return { points, series, total: Math.round(total * 1e6) / 1e6 }
}

/** What a screen reader hears instead of the bars. */
export function chartDescription(points: DayPoint[], series: Series[], total: number): string {
  if (total === 0) return `No spending in the last ${points.length} days.`
  const top = points.reduce((a, b) => (b.total > a.total ? b : a))
  return `Spending per day over the last ${points.length} days: ${formatMoney(total)} in total, most on ${top.label} (${formatMoney(top.total)}). `
    + `Split by ${series.map((s) => s.label).join(', ')}.`
}
