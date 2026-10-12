import { describe, expect, it } from 'vitest'
import openapi from '@/api/openapi.json'
import { DEFAULT_VIEW } from '@/lib/jobsParams'
import { SOURCES, TABS } from '@/lib/jobsParams'
import {
  clearFilters, dimensionScores, emptyMessage, hasFilters, nextSort, pageCount, pageRange, scoreTone, sourceLabel,
  statusOptions, TAB_LABELS,
} from './jobsTable'

const PIPELINE: string[] = openapi.components.schemas.JobRow.properties.pipeline_status.enum

describe('TAB_LABELS', () => {
  it('names every tab the API has, once', () => {
    expect(TAB_LABELS.map((t) => t.tab).sort()).toEqual([...TABS].sort())
    expect(TAB_LABELS[0]).toEqual({ tab: 'shortlisted', label: 'Shortlisted' })
  })
})

describe('statusOptions', () => {
  it('offers only statuses that exist', () => {
    for (const { tab } of TAB_LABELS) for (const s of statusOptions(tab)) expect(PIPELINE).toContain(s)
  })

  it('matches what each tab can hold', () => {
    expect(statusOptions('shortlisted')).toEqual(['shortlisted', 'tailored', 'drafted', 'approved'])
    expect(statusOptions('applied')).toEqual(['applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn', 'approved', 'skipped'])
    expect(statusOptions('filtered')).toEqual([])
    expect(statusOptions('scam')).toEqual([])
    expect(statusOptions('scored')).not.toContain('new')
    expect(statusOptions('scored')).not.toContain('filtered_out')
    expect(statusOptions('all')).toHaveLength(16)
    expect(statusOptions('all')[0]).toBe('new')
  })
})

describe('nextSort', () => {
  it('flips the order on the sorted column', () => {
    expect(nextSort(DEFAULT_VIEW, 'score')).toEqual({ sort: 'score', order: 'asc' })
    expect(nextSort({ ...DEFAULT_VIEW, sort: 'company', order: 'asc' }, 'company')).toEqual({ sort: 'company', order: 'desc' })
  })

  it('starts text A to Z and numbers or dates highest first', () => {
    expect(nextSort(DEFAULT_VIEW, 'company')).toEqual({ sort: 'company', order: 'asc' })
    expect(nextSort(DEFAULT_VIEW, 'title')).toEqual({ sort: 'title', order: 'asc' })
    expect(nextSort(DEFAULT_VIEW, 'status')).toEqual({ sort: 'status', order: 'asc' })
    expect(nextSort({ ...DEFAULT_VIEW, sort: 'company', order: 'asc' }, 'posted')).toEqual({ sort: 'posted', order: 'desc' })
    expect(nextSort({ ...DEFAULT_VIEW, sort: 'title', order: 'asc' }, 'score')).toEqual({ sort: 'score', order: 'desc' })
  })
})

describe('pageRange and pageCount', () => {
  it.each([
    [1, 50, 263, 50, '1–50 of 263'],
    [6, 50, 263, 13, '251–263 of 263'],
    [2, 25, 30, 5, '26–30 of 30'],
    [1, 50, 0, 0, '0 of 0'],
    [9, 50, 20, 0, '0 of 20'],
    [3, 100, 12_345, 100, '201–300 of 12,345'],
  ])('page %d of size %d with %d in total (%d shown) reads %s', (page, size, total, shown, expected) => {
    expect(pageRange(page, size, total, shown)).toBe(expected)
  })

  it('counts at least one page', () => {
    expect(pageCount(50, 0)).toBe(1)
    expect(pageCount(50, 50)).toBe(1)
    expect(pageCount(50, 51)).toBe(2)
    expect(pageCount(25, 263)).toBe(11)
  })
})

describe('hasFilters and clearFilters', () => {
  it.each([
    [{ q: 'acme' }], [{ status: ['offer' as const] }], [{ source: ['lever' as const] }], [{ min_score: 0 }],
    [{ remote: true }], [{ posted_days: 7 }],
  ])('%j is a filter', (patch) => {
    expect(hasFilters({ ...DEFAULT_VIEW, ...patch })).toBe(true)
    expect(hasFilters(clearFilters({ ...DEFAULT_VIEW, ...patch }))).toBe(false)
  })

  it('does not count the tab, sort or paging', () => {
    expect(hasFilters({ ...DEFAULT_VIEW, tab: 'all', sort: 'company', order: 'asc', page: 3, page_size: 100 })).toBe(false)
  })

  it('keeps the tab, sort and page size but starts again at page 1', () => {
    const view = { ...DEFAULT_VIEW, tab: 'applied' as const, sort: 'company' as const, order: 'asc' as const, page: 4, page_size: 100, q: 'x', remote: true }
    expect(clearFilters(view)).toEqual({ ...DEFAULT_VIEW, tab: 'applied', sort: 'company', order: 'asc', page_size: 100 })
  })
})

describe('emptyMessage', () => {
  it('speaks to the tab, or to the filters when there are some', () => {
    expect(emptyMessage(DEFAULT_VIEW)).toBe('No shortlisted jobs right now. New matches arrive after each collect.')
    expect(emptyMessage({ ...DEFAULT_VIEW, tab: 'filtered' })).toBe('Nothing was filtered out.')
    expect(emptyMessage({ ...DEFAULT_VIEW, tab: 'filtered', remote: true })).toBe('No jobs match these filters.')
    for (const { tab } of TAB_LABELS) expect(emptyMessage({ ...DEFAULT_VIEW, tab })).not.toBe('')
  })
})

describe('scoreTone', () => {
  it.each([
    [75, 60, 'success'], [60, 60, 'success'], [59, 60, 'warning'], [50, 60, 'warning'], [49, 60, 'muted'],
    [80, null, 'neutral'], [null, 60, 'muted'],
  ] as const)('%s against %s is %s', (score, threshold, tone) => {
    expect(scoreTone(score, threshold)).toBe(tone)
  })
})

describe('dimensionScores', () => {
  it('keeps the numbers, in the order given, with readable names', () => {
    expect(dimensionScores({ technical_fit: 72, seniority: 65, notes: 'x', nested: { a: 1 }, bad: Number.NaN }))
      .toEqual([['technical fit', 72], ['seniority', 65]])
    expect(dimensionScores(null)).toEqual([])
  })
})

describe('sourceLabel', () => {
  it('has a name for every board and passes others through', () => {
    for (const s of SOURCES) expect(sourceLabel(s)).not.toBe(s)
    expect(sourceLabel('linkedin_apify')).toBe('LinkedIn (Apify)')
    expect(sourceLabel('some_new_board')).toBe('some_new_board')
  })
})
