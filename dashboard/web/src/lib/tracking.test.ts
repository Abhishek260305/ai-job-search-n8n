import { describe, expect, it } from 'vitest'
import type { Tracking } from '@/api/types'
import { addDays, changesOf, dueText, emailProblem, forgetDraft, isDirty, mailtoHref, recalledDraft, rememberDraft, toDraft } from '@/lib/tracking'

const saved: Tracking = { notes: 'call Priya', contact_name: 'Priya N', contact_email: 'priya@example.com', referral: null, follow_up_on: '2026-10-16', version: 'v1', today: '2026-10-12' }

describe('the draft', () => {
  it('starts from what is saved, with nothing as an empty string', () => {
    expect(toDraft(saved)).toEqual({ notes: 'call Priya', contact_name: 'Priya N', contact_email: 'priya@example.com', referral: '', follow_up_on: '2026-10-16' })
  })

  it('is not dirty until something differs, and trimming is not a difference', () => {
    const d = toDraft(saved)
    expect(isDirty(d, saved)).toBe(false)
    expect(isDirty({ ...d, notes: '  call Priya  ' }, saved)).toBe(false)
    expect(isDirty({ ...d, referral: '   ' }, saved)).toBe(false)
    expect(isDirty({ ...d, notes: 'call Priya today' }, saved)).toBe(true)
  })

  it('sends only the fields that changed, with an emptied field as null', () => {
    const d = toDraft(saved)
    expect(changesOf({ ...d, notes: 'moved', referral: 'via Sam' }, saved)).toEqual({ notes: 'moved', referral: 'via Sam' })
    expect(changesOf({ ...d, contact_email: '', follow_up_on: '' }, saved)).toEqual({ contact_email: null, follow_up_on: null })
    expect(changesOf({ ...d, follow_up_on: '2026-11-01' }, saved)).toEqual({ follow_up_on: '2026-11-01' })
    expect(changesOf(d, saved)).toEqual({})
  })
})

describe('email', () => {
  it('accepts an empty or ordinary address and refuses the rest, as the database does', () => {
    for (const ok of ['', '  ', 'a@b.co', 'first.last+tag@example.com']) expect(emailProblem(ok), ok).toBeNull()
    for (const bad of ['nope', 'a@b', 'a b@c.de', '@x.de', 'a@@b.de']) expect(emailProblem(bad), bad).not.toBeNull()
  })

  it('only links an ordinary address', () => {
    expect(mailtoHref('priya@example.com')).toBe('mailto:priya@example.com')
    expect(mailtoHref('a?subject=x@b.de')).toBeNull()          // valid for the database, but not worth a link
    expect(mailtoHref('priya@example.com?cc=boss@example.com')).toBeNull()      // nothing may follow the domain
    expect(mailtoHref('priya@example.com\n')).toBeNull()
    expect(mailtoHref('a%0D%0ABcc%3Ax@evil.example.com')).toBeNull()             // an encoded line break is a header injection
    expect(mailtoHref('a%2Cb@evil.example.com')).toBeNull()
    expect(mailtoHref('x@y.z')).toBeNull()
    expect(mailtoHref(null)).toBeNull()
  })
})

describe('dates', () => {
  it('moves a calendar day by whole days across month ends, years and leap days, with no time zone in it', () => {
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01')
    expect(addDays('2026-12-28', 7)).toBe('2027-01-04')
    expect(addDays('2026-02-27', 3)).toBe('2026-03-02')
    expect(addDays('2028-02-27', 3)).toBe('2028-03-01')                    // a leap year
    expect(addDays('2026-03-07', 7)).toBe('2026-03-14')                    // a daylight-saving change somewhere is still 7 days
    expect(addDays('2026-11-01', 7)).toBe('2026-11-08')
    expect(addDays('2026-10-12', 0)).toBe('2026-10-12')
    expect(addDays('2026-10-12', -3)).toBe('2026-10-09')
  })

  it('says why a job is due', () => {
    expect(dueText({ reason: 'date', follow_up_on: '2026-10-12', days_waiting: null }, '2026-10-12')).toBe('Your follow-up date is today')
    expect(dueText({ reason: 'date', follow_up_on: '2026-10-10', days_waiting: null }, '2026-10-12')).toBe('Your follow-up date was 2026-10-10')
    expect(dueText({ reason: 'waiting', follow_up_on: null, days_waiting: 1 }, '2026-10-12')).toBe('Applied 1 day ago, no reply')
    expect(dueText({ reason: 'waiting', follow_up_on: null, days_waiting: 11 }, '2026-10-12')).toBe('Applied 11 days ago, no reply')
  })
})


describe('the unsaved draft', () => {
  const draft = { ...toDraft(saved), notes: 'half-typed' }

  it('comes back for the same job and the same stored version', () => {
    rememberDraft(7, 'v1', draft)
    expect(recalledDraft(7, 'v1')).toEqual(draft)
    expect(recalledDraft(8, 'v1')).toBeNull()                            // another job
    forgetDraft(7)
  })

  it('is dropped when the stored version has moved on, and when it is forgotten', () => {
    rememberDraft(7, 'v1', draft)
    expect(recalledDraft(7, 'v2')).toBeNull()
    expect(recalledDraft(7, 'v1')).toBeNull()                            // gone for good
    rememberDraft(7, null, draft)
    expect(recalledDraft(7, null)).toEqual(draft)                        // a job with nothing saved yet
    forgetDraft(7)
    expect(recalledDraft(7, null)).toBeNull()
  })
})
