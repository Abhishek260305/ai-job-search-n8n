import { describe, expect, it } from 'vitest'
import { SUMMARY } from '@/test/server'
import { donutDescription, donutSlices, funnelDescription, funnelRows, share, thresholdNote, totalOf } from './charts'

describe('donutSlices', () => {
  it('lists the submitted statuses in pipeline order, zeros included', () => {
    const slices = donutSlices(SUMMARY.by_status, false)
    expect(slices.map((s) => [s.key, s.count])).toEqual([
      ['applied', 0], ['in_progress', 2], ['interview', 1], ['offer', 0], ['rejected', 2], ['withdrawn', 0],
    ])
    expect(totalOf(slices)).toBe(5)
  })

  it('adds the shortlist last when asked', () => {
    const slices = donutSlices(SUMMARY.by_status, true)
    expect(slices.at(-1)).toMatchObject({ key: 'not_applied', label: 'Not applied', count: 43 })
    expect(totalOf(slices)).toBe(48)
  })

  it('describes the ring in words', () => {
    expect(donutDescription(donutSlices(SUMMARY.by_status, false))).toBe(
      'Applications by status: Applied 0, In progress 2, Interview 1, Offer 0, Rejected 2, Withdrawn 0. 5 in total.')
    expect(donutDescription(donutSlices({ ...SUMMARY.by_status, not_applied: 1200 }, true)))
      .toMatch(/Not applied 1,200\. 1,205 in total\.$/)
  })
})

describe('share', () => {
  it.each([
    [600, 1234, '49%'],
    [5, 5, '100%'],
    [0, 5, '0%'],
    [1, 500, '<1%'],
    [1, 199, '<1%'],
    [1, 100, '1%'],
    [2, 3, '67%'],
    [0, 0, '—'],
    [3, 0, '—'],
    [3, Number.NaN, '—'],
  ])('%d of %d is %s', (count, previous, expected) => {
    expect(share(count, previous)).toBe(expected)
  })
})

describe('funnelRows', () => {
  it('labels each step with its count and its share of the step before', () => {
    expect(funnelRows(SUMMARY.funnel).map((r) => [r.label, r.text])).toEqual([
      ['Found', '1,234'],
      ['Passed filters', '600 · 49%'],
      ['Scored', '590 · 98%'],
      ['Shortlisted', '50 · 8%'],
      ['Applied', '5 · 10%'],
      ['Interview', '1 · 20%'],
      ['Offer', '0 · 0%'],
    ])
  })

  it('shows "—" after an empty step instead of dividing by zero', () => {
    const rows = funnelRows([{ step: 'applied', count: 0 }, { step: 'interview', count: 0 }, { step: 'offer', count: 0 }])
    expect(rows.map((r) => r.share)).toEqual([null, '—', '—'])
    expect(rows[1].text).toBe('0 · —')
  })

  it('still names a step this build does not know', () => {
    expect(funnelRows([{ step: 'phone_screen', count: 2 }])[0].label).toBe('phone screen')
  })

  it('describes the bars in words', () => {
    const rows = funnelRows([{ step: 'found', count: 1234 }, { step: 'passed_filters', count: 600 }, { step: 'scored', count: 0 }, { step: 'shortlisted', count: 0 }])
    expect(funnelDescription(rows)).toBe(
      'Where jobs drop out, each step as a share of the one before: Found 1,234, Passed filters 600 (49%), Scored 0 (0%), Shortlisted 0 (—).')
  })
})

describe('thresholdNote', () => {
  it('names the threshold when there is one', () => {
    expect(thresholdNote(60)).toBe('Shortlisted uses your current threshold (60).')
    expect(thresholdNote(0)).toBe('Shortlisted uses your current threshold (0).')
    expect(thresholdNote(null)).toBe('Shortlisted uses your current score threshold.')
  })
})