// The one place that says how a status is called and coloured (docs/dashboard.md, "One status per job").
import type { EditableStatus, PipelineStatus, Summary } from '@/api/types'

export type Tone = 'neutral' | 'muted' | 'info' | 'progress' | 'success' | 'warning' | 'danger'

type Meta = { label: string; tone: Tone; order: number }

// `order` follows the pipeline: found, filtered, scored, shortlisted, prepared, submitted, outcome, closed.
export const STATUS_META: Record<PipelineStatus, Meta> = {
  new: { label: 'Waiting to be scored', tone: 'muted', order: 1 },
  filtered_out: { label: 'Filtered out', tone: 'muted', order: 2 },
  scam_blocked: { label: 'Scam-blocked', tone: 'danger', order: 3 },
  scored: { label: 'Below threshold', tone: 'muted', order: 4 },
  shortlisted: { label: 'Not applied', tone: 'neutral', order: 5 },
  tailored: { label: 'Drafted, waiting for review', tone: 'info', order: 6 },
  drafted: { label: 'Drafted, waiting for review', tone: 'info', order: 7 },
  approved: { label: 'Ready to apply', tone: 'info', order: 8 },
  applied: { label: 'Applied', tone: 'progress', order: 9 },
  in_progress: { label: 'In progress', tone: 'progress', order: 10 },
  interview: { label: 'Interview', tone: 'warning', order: 11 },
  offer: { label: 'Offer', tone: 'success', order: 12 },
  rejected: { label: 'Rejected', tone: 'danger', order: 13 },
  withdrawn: { label: 'Withdrawn', tone: 'muted', order: 14 },
  skipped: { label: 'Skipped', tone: 'muted', order: 15 },
  expired: { label: 'Expired', tone: 'muted', order: 16 },
}

/** What set_application_status() accepts, in pipeline order (the dropdown's order). `drafted` only comes from tailoring. */
export const EDITABLE_STATUSES: readonly EditableStatus[] = [
  'approved', 'applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn', 'skipped',
]

const SUBMITTED = new Set<string>(['applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn'])

export function statusLabel(status: string): string {
  return STATUS_META[status as PipelineStatus]?.label ?? status.replace(/_/g, ' ')
}

export function statusTone(status: string): Tone {
  return STATUS_META[status as PipelineStatus]?.tone ?? 'neutral'
}

/** Sent to the employer: every stage from `applied` on. */
export function isSubmitted(status: string): boolean {
  return SUBMITTED.has(status)
}

/** Moving a submitted application back to "not yet" asks first (applied_at is kept, as the SQL function does). */
export function needsConfirm(from: string, to: string): boolean {
  return isSubmitted(from) && (to === 'approved' || to === 'skipped')
}

/** Tailwind classes for a status badge. Text and background keep at least 4.5:1 contrast in both themes. */
export const TONE_CLASSES: Record<Tone, string> = {
  neutral: 'bg-secondary text-secondary-foreground',
  muted: 'bg-muted text-muted-foreground',
  info: 'bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200',
  progress: 'bg-indigo-100 text-indigo-900 dark:bg-indigo-950 dark:text-indigo-200',
  success: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200',
  warning: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200',
  danger: 'bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200',
}

type DonutKey = keyof Summary['by_status']

/** The donut's slices in drawing order: Okabe-Ito colours, which stay apart for colour-blind eyes. */
export const DONUT_SLICES: readonly { key: DonutKey; label: string; color: string }[] = [
  { key: 'applied', label: STATUS_META.applied.label, color: '#0072B2' },
  { key: 'in_progress', label: STATUS_META.in_progress.label, color: '#56B4E9' },
  { key: 'interview', label: STATUS_META.interview.label, color: '#E69F00' },
  { key: 'offer', label: STATUS_META.offer.label, color: '#009E73' },
  { key: 'rejected', label: STATUS_META.rejected.label, color: '#D55E00' },
  { key: 'withdrawn', label: STATUS_META.withdrawn.label, color: '#999999' },
  { key: 'not_applied', label: STATUS_META.shortlisted.label, color: '#CC79A7' },
]
