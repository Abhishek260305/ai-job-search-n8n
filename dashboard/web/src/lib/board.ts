// How the Kanban board is laid out, and which moves it allows (docs/dashboard.md, "Version 3a").
import type { BoardColumnKey, EditableStatus, JobEvent, PipelineStatus, Stats } from '@/api/types'
import { isSubmitted, statusLabel } from '@/lib/status'

type ColumnMeta = {
  label: string
  /** The status a card dropped on this column gets. `null`: nothing can be dropped here (it only holds jobs not yet applied). */
  drop: EditableStatus | null
  hint: string
}

export const COLUMN_META: Record<BoardColumnKey, ColumnMeta> = {
  not_applied: { label: 'Not applied', drop: null, hint: 'Shortlisted jobs and drafts, best score first' },
  applied: { label: 'Applied', drop: 'applied', hint: 'Waiting for an answer' },
  in_progress: { label: 'In progress', drop: 'in_progress', hint: 'A recruiter or test is under way' },
  interview: { label: 'Interview', drop: 'interview', hint: 'Interviews scheduled or done' },
  offer: { label: 'Offer', drop: 'offer', hint: 'You have an offer' },
  rejected: { label: 'Rejected', drop: 'rejected', hint: 'They said no' },
}

/** The column a status belongs to; `null` for statuses the board does not show (withdrawn, skipped, and everything before shortlisting). */
export function columnOf(status: PipelineStatus): BoardColumnKey | null {
  if (status === 'shortlisted' || status === 'tailored' || status === 'drafted' || status === 'approved') return 'not_applied'
  if (status === 'applied' || status === 'in_progress' || status === 'interview' || status === 'offer' || status === 'rejected') return status
  return null
}

/** What a drop of a card on a column does: the status to set, or `null` when the drop is not allowed or changes nothing. */
export function dropTarget(from: PipelineStatus, to: BoardColumnKey): EditableStatus | null {
  const status = COLUMN_META[to].drop
  if (status === null || status === from) return null
  return status
}

/** Whole days since `since`, never negative (the server's clock may be a little ahead of the browser's). */
export function daysSince(since: string, now: Date): number {
  return Math.max(0, Math.floor((now.getTime() - new Date(since).getTime()) / 86_400_000))
}

/** "today", "1 day", "5 days": how long a card has been in its column. */
export function inColumnFor(since: string, now: Date): string {
  const d = daysSince(since, now)
  return d === 0 ? 'today' : d === 1 ? '1 day' : `${d} days`
}

/** A column whose cards were all submitted: the card shows how long ago, not a score. */
export function showsSubmitted(column: BoardColumnKey): boolean {
  return isSubmitted(column)
}

/** One line of the timeline, in words: who, what and when. */
export function describeEvent(e: Pick<JobEvent, 'from_status' | 'to_status' | 'source'>): string {
  const by = e.source === 'dashboard' ? ' on the dashboard' : ''
  if (e.from_status === e.to_status) return `Note added${by}`
  if (e.from_status === null) return `${statusLabel(e.to_status)}${by}`
  return `${statusLabel(e.from_status)} to ${statusLabel(e.to_status)}${by}`
}


/** Whole percent, or a dash while nothing has been applied to: never a division by zero. */
export function responseRate(stats: Pick<Stats, 'applied' | 'responded'>): string {
  if (stats.applied <= 0) return '—'
  return `${Math.round((100 * stats.responded) / stats.applied)}%`
}
