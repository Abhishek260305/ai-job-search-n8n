import { describe, expect, it } from 'vitest'
import {
  descriptionPreview, DESCRIPTION_LINES, flagList, formatCost, gateList, neighbours, riskLabel, textList, yearsAsked,
} from '@/lib/jobDetail'

describe('descriptionPreview', () => {
  it('leaves a short description whole', () => {
    expect(descriptionPreview('one\ntwo')).toBeNull()
    expect(descriptionPreview(Array.from({ length: DESCRIPTION_LINES }, (_, i) => `line ${i}`).join('\n'))).toBeNull()
  })

  it('keeps the first 40 lines of a longer one', () => {
    const text = Array.from({ length: DESCRIPTION_LINES + 1 }, (_, i) => `line ${i}`).join('\n')
    const preview = descriptionPreview(text)!
    expect(preview.split('\n')).toHaveLength(DESCRIPTION_LINES)
    expect(preview.endsWith('line 39…')).toBe(true)
  })

  it('collapses a long description without line breaks', () => {
    const text = 'word '.repeat(1000)
    const preview = descriptionPreview(text)!
    expect(preview.length).toBeLessThan(text.length)
    expect(preview.length).toBeLessThanOrEqual(4001)
    expect(preview.endsWith('…')).toBe(true)
    expect(descriptionPreview('x'.repeat(4000))).toBeNull()
  })
})

describe('gateList', () => {
  it('lists the gates with their verdicts and marks a failed one', () => {
    expect(gateList({
      eligibility: { verdict: 'PASS', note: '' },
      work_location: { verdict: 'fail', note: ' onsite in Berlin ' },
      language: { verdict: 'FLAG', note: 'German preferred' },
      visa: { verdict: 'UNKNOWN' },
    })).toEqual([
      { name: 'eligibility', verdict: 'PASS', label: 'Pass', tone: 'success', note: '', failed: false },
      { name: 'work location', verdict: 'FAIL', label: 'Fail', tone: 'danger', note: 'onsite in Berlin', failed: true },
      { name: 'language', verdict: 'FLAG', label: 'Check', tone: 'warning', note: 'German preferred', failed: false },
      { name: 'visa', verdict: 'UNKNOWN', label: 'Unknown', tone: 'muted', note: '', failed: false },
    ])
  })

  it('skips anything that is not a gate', () => {
    expect(gateList(null)).toEqual([])
    expect(gateList({ a: null, b: 'PASS', c: { verdict: 3 }, d: { note: 'x' } })).toEqual([])
    expect(gateList({ e: { verdict: 'MAYBE', note: 7 } })).toEqual([
      { name: 'e', verdict: 'MAYBE', label: 'MAYBE', tone: 'muted', note: '', failed: false },
    ])
  })
})

describe('flagList', () => {
  it('names the flags, the most severe first', () => {
    expect(flagList([
      { code: 'UNVERIFIED_HOST', severity: 1, detail: 'apply link: unknown host (x.io)' },
      { code: 'PROMPT_INJECTION', severity: 3, detail: ' instructions to the reader ' },
      { code: 'NO_COMPANY' },
    ])).toEqual([
      { code: 'PROMPT_INJECTION', label: 'Prompt injection', severity: 3, detail: 'instructions to the reader' },
      { code: 'UNVERIFIED_HOST', label: 'Unverified host', severity: 1, detail: 'apply link: unknown host (x.io)' },
      { code: 'NO_COMPANY', label: 'No company', severity: null, detail: '' },
    ])
  })

  it('skips anything that is not a flag', () => {
    expect(flagList(null)).toEqual([])
    expect(flagList(['FREE_EMAIL', null, { code: '' }, { code: 4 }, { severity: 2 }])).toEqual([])
  })
})

it('textList keeps the non-blank strings', () => {
  expect(textList([' Python ', '', 3, null, 'Kafka'])).toEqual(['Python', 'Kafka'])
  expect(textList(null)).toEqual([])
})

it('yearsAsked says what the posting asks for', () => {
  expect(yearsAsked(3, 5)).toBe('3–5 years')
  expect(yearsAsked(1, 1)).toBe('1 year')
  expect(yearsAsked(0, 2)).toBe('0–2 years')
  expect(yearsAsked(3, null)).toBe('3+ years')
  expect(yearsAsked(null, 1)).toBe('Up to 1 year')
  expect(yearsAsked(null, null)).toBeNull()
})

it('formatCost', () => {
  expect(formatCost(0.0342)).toBe('$0.034')
  expect(formatCost(1.5)).toBe('$1.500')
  expect(formatCost(0.0004)).toBe('<$0.001')
  expect(formatCost(0)).toBe('$0')
  expect(formatCost(null)).toBe('—')
})

it('riskLabel', () => {
  expect(riskLabel('medium')).toBe('Medium risk')
  expect(riskLabel('high')).toBe('High risk')
})

describe('neighbours', () => {
  it('finds the jobs around one on the page', () => {
    expect(neighbours([4, 8, 15], 8)).toEqual({ prev: 4, next: 15, position: 2 })
    expect(neighbours([4, 8, 15], 4)).toEqual({ prev: null, next: 8, position: 1 })
    expect(neighbours([4, 8, 15], 15)).toEqual({ prev: 8, next: null, position: 3 })
  })

  it('has none for a job that is not on the page', () => {
    expect(neighbours([4, 8], 99)).toEqual({ prev: null, next: null, position: null })
    expect(neighbours([], null)).toEqual({ prev: null, next: null, position: null })
  })
})
