// The settings forms' rules, apart from the components so they can be checked directly. The scraper validates every
// value for real (ranges, "collect at most every 15 minutes"); these helpers only turn what is typed into what is
// sent, send what changed, and give the hints and the one rule the page adds itself: caps can only be lowered.
import type { BudgetCaps, BudgetChange, ScheduleChange, ScheduleSetting, TailoringChange, TailoringSettings } from '@/api/types'

export type Unit = 'minutes' | 'hours' | 'days'
const PER_UNIT: Record<Unit, number> = { minutes: 1, hours: 60, days: 1440 }

/** 360 -> { value: '6', unit: 'hours' }; the biggest unit that divides evenly. */
export function splitMinutes(minutes: number | null): { value: string; unit: Unit } {
  if (!minutes || minutes < 1) return { value: '', unit: 'hours' }
  if (minutes % 1440 === 0) return { value: String(minutes / 1440), unit: 'days' }
  if (minutes % 60 === 0) return { value: String(minutes / 60), unit: 'hours' }
  return { value: String(minutes), unit: 'minutes' }
}

/** '6' + hours -> 360; null for anything that is not a whole, positive number of minutes. Integer arithmetic on
 * hundredths: 2.05 hours is exactly 123 minutes, which floating point would call 122.99999999999999. */
export function toMinutes(value: string, unit: Unit): number | null {
  const m = /^(\d{1,5})(?:\.(\d{1,2}))?$/.exec(value.trim())
  if (!m) return null
  const hundredths = Number(m[1]) * 100 + Number((m[2] ?? '').padEnd(2, '0'))
  const scaled = hundredths * PER_UNIT[unit]
  return scaled % 100 === 0 && scaled > 0 ? scaled / 100 : null
}

/** A whole number typed in a box, or null when empty or not whole. */
export function wholeNumber(text: string): number | null {
  return /^\d{1,6}$/.test(text.trim()) ? Number(text.trim()) : null
}

export type ScheduleForm = {
  enabled: boolean
  every: { value: string; unit: Unit }
  daily: boolean            // the digest only: a time of day instead of every N
  atTime: string
  batch: string
  boardBatch: string
}

export function scheduleForm(s: ScheduleSetting): ScheduleForm {
  return {
    enabled: s.enabled,
    every: splitMinutes(s.every_minutes),
    daily: s.at_time !== null,
    atTime: s.at_time ? s.at_time.slice(0, 5) : '08:00',
    batch: String(s.batch),
    boardBatch: s.board_batch === null ? '' : String(s.board_batch),
  }
}

export type Built<T> = { changes: T; error: string | null }

/** What to send for a schedule: only what differs from the saved one. An empty `changes` means nothing to save. */
export function scheduleChanges(saved: ScheduleSetting, form: ScheduleForm): Built<ScheduleChange> {
  const changes: ScheduleChange = {}
  if (form.enabled !== saved.enabled) changes.enabled = form.enabled

  const wasDaily = saved.at_time !== null
  if (saved.name === 'digest' && form.daily) {
    if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(form.atTime.trim())) return { changes: {}, error: 'Enter the time as HH:MM, for example 08:00.' }
    if (!wasDaily || form.atTime.trim() !== saved.at_time?.slice(0, 5)) changes.at_time = form.atTime.trim()
  } else {
    const minutes = toMinutes(form.every.value, form.every.unit)
    if (minutes === null) return { changes: {}, error: 'How often needs a whole number of minutes, hours or days.' }
    if (minutes !== saved.every_minutes) changes.every_minutes = minutes
  }

  const batch = wholeNumber(form.batch)
  if (batch === null) return { changes: {}, error: 'The number per run must be a whole number.' }
  if (batch !== saved.batch) changes.batch = batch

  // a scraper that does not report board_batch yet (not rebuilt) leaves the field out rather than blocking the card
  if (saved.name === 'collect' && saved.board_batch !== null) {
    const boards = wholeNumber(form.boardBatch)
    if (boards === null) return { changes: {}, error: 'Boards and feeds per run must be a whole number.' }
    if (boards !== saved.board_batch) changes.board_batch = boards
  }
  return { changes, error: null }
}

export type TailoringForm = {
  mode: 'auto' | 'manual'
  minScore: string
  maxScore: string
  dailyLimit: string
  coverLetter: boolean
  maxPages: string
  paper: 'a4' | 'letter'
}

export function tailoringForm(t: TailoringSettings): TailoringForm {
  return {
    mode: t.mode,
    minScore: t.min_score === null ? '' : String(t.min_score),
    maxScore: t.max_score === null ? '' : String(t.max_score),
    dailyLimit: t.daily_limit === null ? '' : String(t.daily_limit),
    coverLetter: t.cover_letter,
    maxPages: String(t.max_pages),
    paper: t.paper === 'letter' ? 'letter' : 'a4',
  }
}

/** An emptied optional number is sent as a "clear" (back to "none"), a typed one as a value. */
export function tailoringChanges(saved: TailoringSettings, form: TailoringForm): Built<TailoringChange> {
  const changes: TailoringChange = {}
  const clear: NonNullable<TailoringChange['clear']> = []
  if (form.mode !== saved.mode) changes.mode = form.mode
  if (form.coverLetter !== saved.cover_letter) changes.cover_letter = form.coverLetter
  if (form.paper !== saved.paper) changes.paper = form.paper
  if (Number(form.maxPages) !== saved.max_pages) changes.max_pages = Number(form.maxPages)
  for (const [key, text, was, label] of [
    ['min_score', form.minScore, saved.min_score, 'The lowest score'],
    ['max_score', form.maxScore, saved.max_score, 'The score above which your base resume is used'],
    ['daily_limit', form.dailyLimit, saved.daily_limit, 'The daily limit'],
  ] as const) {
    if (text.trim() === '') {
      if (was !== null) clear.push(key)
    } else {
      const n = wholeNumber(text)
      if (n === null) return { changes: {}, error: `${label} must be a whole number, or empty for none.` }
      if (n !== was) changes[key] = n
    }
  }
  if (clear.length) changes.clear = clear
  return { changes, error: null }
}

export const CAP_LABELS: Record<keyof Omit<BudgetCaps, 'warn_pct'>, string> = {
  monthly_cap_usd: 'Claude, per month',
  daily_cap_usd: 'Claude, per day',
  apify_monthly_cap_usd: 'Apify, per month',
  apify_daily_cap_usd: 'Apify, per day',
}
export type CapKey = keyof typeof CAP_LABELS
export const CAP_KEYS = Object.keys(CAP_LABELS) as CapKey[]

/** The caps to lower: only those typed lower than now. Anything higher is refused here, as the server does too.
 * A field that was not edited is not checked: a stored value that prints oddly must not lock the others. */
export function budgetChanges(saved: BudgetCaps, typed: Record<CapKey, string>): Built<BudgetChange> {
  const changes: BudgetChange = {}
  const original = capsForm(saved)
  for (const key of CAP_KEYS) {
    const text = typed[key].trim()
    if (text === original[key]) continue
    if (text === '') return { changes: {}, error: `${CAP_LABELS[key]}: enter an amount.` }
    if (!/^\d{1,6}(\.\d{1,3})?$/.test(text)) return { changes: {}, error: `${CAP_LABELS[key]}: enter an amount in dollars, for example 7.50.` }
    const value = Number(text)
    if (value > saved[key] + 1e-9) {
      return { changes: {}, error: `${CAP_LABELS[key]} can only be lowered here. To raise it, run scripts/jobsearch budget set.` }
    }
    if (value < saved[key] - 1e-9) changes[key] = value
  }
  return { changes, error: null }
}

/** A cap as it is typed: up to three decimals, no exponent ("1e-7" would not be a number the form accepts). */
function capText(value: number): string {
  return String(Number(value.toFixed(3)))
}

export function capsForm(caps: BudgetCaps): Record<CapKey, string> {
  return Object.fromEntries(CAP_KEYS.map((k) => [k, capText(caps[k])])) as Record<CapKey, string>
}

/** The scraper's range for "every", as a hint: "15 minutes to 7 days". */
export function everyHint(min: number, max: number): string {
  const text = (m: number) => (m % 1440 === 0 ? `${m / 1440} day${m === 1440 ? '' : 's'}` : m % 60 === 0 ? `${m / 60} hour${m === 60 ? '' : 's'}` : `${m} minutes`)
  return `${text(min)} to ${text(max)}`
}

export const isEmpty = (changes: object): boolean => Object.keys(changes).length === 0
