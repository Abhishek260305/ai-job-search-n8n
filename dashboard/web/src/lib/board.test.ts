import { describe, expect, it } from 'vitest'
import { COLUMN_META, columnOf, daysSince, dropTarget, inColumnFor } from '@/lib/board'
import { STATUS_META } from '@/lib/status'
import type { PipelineStatus } from '@/api/types'

describe('which column a status belongs to', () => {
  it.each([
    ['shortlisted', 'not_applied'], ['tailored', 'not_applied'], ['drafted', 'not_applied'], ['approved', 'not_applied'],
    ['applied', 'applied'], ['in_progress', 'in_progress'], ['interview', 'interview'], ['offer', 'offer'], ['rejected', 'rejected'],
  ] as const)('%s is in %s', (status, column) => expect(columnOf(status)).toBe(column))

  it.each(['new', 'filtered_out', 'scam_blocked', 'scored', 'withdrawn', 'skipped', 'expired'] as const)('%s is not on the board', (status) => {
    expect(columnOf(status)).toBeNull()
  })

  it('every status is either on the board or deliberately not (a new status must be decided here)', () => {
    const decided = new Set<string>(['new', 'filtered_out', 'scam_blocked', 'scored', 'withdrawn', 'skipped', 'expired'])
    for (const status of Object.keys(STATUS_META) as PipelineStatus[]) {
      expect(columnOf(status) !== null || decided.has(status), status).toBe(true)
    }
  })
})

describe('what a drop does', () => {
  it('sets the column status', () => {
    expect(dropTarget('shortlisted', 'applied')).toBe('applied')
    expect(dropTarget('drafted', 'interview')).toBe('interview')
    expect(dropTarget('applied', 'rejected')).toBe('rejected')
    expect(dropTarget('interview', 'applied')).toBe('applied')     // a mistake can be undone by dragging back
  })

  it('does nothing on the same column or on Not applied (a submitted job is never un-submitted by a drop)', () => {
    expect(dropTarget('applied', 'applied')).toBeNull()
    expect(dropTarget('interview', 'not_applied')).toBeNull()
    expect(dropTarget('shortlisted', 'not_applied')).toBeNull()
  })

  it('only Not applied refuses drops', () => {
    expect(Object.entries(COLUMN_META).filter(([, m]) => m.drop === null).map(([k]) => k)).toEqual(['not_applied'])
  })
})

describe('how long a card has been there', () => {
  const now = new Date('2026-10-12T12:00:00Z')
  it('counts whole days and never goes negative', () => {
    expect(daysSince('2026-10-12T01:00:00Z', now)).toBe(0)
    expect(daysSince('2026-10-11T11:59:00Z', now)).toBe(1)
    expect(daysSince('2026-10-07T12:00:00Z', now)).toBe(5)
    expect(daysSince('2026-10-13T12:00:00Z', now)).toBe(0)      // the server's clock runs ahead
  })
  it('says it in words', () => {
    expect(inColumnFor('2026-10-12T01:00:00Z', now)).toBe('today')
    expect(inColumnFor('2026-10-11T00:00:00Z', now)).toBe('1 day')
    expect(inColumnFor('2026-10-07T12:00:00Z', now)).toBe('5 days')
  })
})
