// Notes, contact and follow-up date of a job (dashboard version 3b): the form's draft, what changed, and the date maths.
import type { FollowUp, Tracking } from '@/api/types'
import type { TrackingChanges } from '@/api/hooks'

/** The same numbers as the database's limits (migration 019); tests/test_store_pg.py checks the API's agree. */
export const LIMITS = { notes: 2000, contact_name: 100, contact_email: 200, referral: 200 } as const

export type Draft = { notes: string; contact_name: string; contact_email: string; referral: string; follow_up_on: string }

export const EMPTY_DRAFT: Draft = { notes: '', contact_name: '', contact_email: '', referral: '', follow_up_on: '' }

export function toDraft(t: Tracking): Draft {
  return {
    notes: t.notes ?? '', contact_name: t.contact_name ?? '', contact_email: t.contact_email ?? '',
    referral: t.referral ?? '', follow_up_on: t.follow_up_on ?? '',
  }
}

const TEXT_FIELDS = ['notes', 'contact_name', 'contact_email', 'referral'] as const

/** The fields whose text differs from what is saved (compared as the server stores them: trimmed, empty = none). */
export function changesOf(draft: Draft, saved: Tracking): TrackingChanges {
  const out: TrackingChanges = {}
  for (const field of TEXT_FIELDS) {
    const now = draft[field].trim() || null
    if (now !== (saved[field] ?? null)) out[field] = now
  }
  const date = draft.follow_up_on || null
  if (date !== (saved.follow_up_on ?? null)) out.follow_up_on = date
  return out
}

export function isDirty(draft: Draft, saved: Tracking): boolean {
  return Object.keys(changesOf(draft, saved)).length > 0
}

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
// no % here: an encoded line break or comma (%0D%0A, %2C) must not reach a mailto link
const SAFE_EMAIL = /^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/

/** A short message when the address will be refused (the database checks the same shape), else null. Empty is fine. */
export function emailProblem(value: string): string | null {
  const v = value.trim()
  return v === '' || EMAIL.test(v) ? null : 'That does not look like an email address.'
}

/** A mailto link for an ordinary address; anything unusual is shown as text only. */
export function mailtoHref(email: string | null): string | null {
  return email && SAFE_EMAIL.test(email) ? `mailto:${email}` : null
}

/** `iso` (a YYYY-MM-DD calendar day) moved by whole days. Plain calendar arithmetic: no time zone, no daylight saving. */
export function addDays(iso: string, days: number): string {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10)
}

// What is typed in a job's notes form and not saved yet, kept while the panel is closed or moved to another job, so a
// stray Escape or "next job" never throws it away. It only lives as long as the page; a draft is dropped when the
// stored version has moved on (somebody else saved) or when it is saved or discarded.
const DRAFTS = new Map<number, { version: string | null; draft: Draft }>()

export function rememberDraft(id: number, version: string | null, draft: Draft): void {
  DRAFTS.set(id, { version, draft })
}

export function forgetDraft(id: number): void {
  DRAFTS.delete(id)
}

/** The unsaved draft of this job, if it was started from the version that is stored now. */
export function recalledDraft(id: number, version: string | null): Draft | null {
  const kept = DRAFTS.get(id)
  if (!kept) return null
  if (kept.version !== version) {
    DRAFTS.delete(id)
    return null
  }
  return kept.draft
}

/** Why a job is on the follow-up list, in words. */
export function dueText(f: Pick<FollowUp, 'reason' | 'follow_up_on' | 'days_waiting'>, today: string): string {
  if (f.reason === 'date') {
    return f.follow_up_on === today ? 'Your follow-up date is today' : `Your follow-up date was ${f.follow_up_on}`
  }
  const n = f.days_waiting ?? 0
  return `Applied ${n} day${n === 1 ? '' : 's'} ago, no reply`
}
