import { describe, expect, it } from 'vitest'
import openapi from '@/api/openapi.json'
import {
  DONUT_SLICES, EDITABLE_STATUSES, isSubmitted, needsConfirm, STATUS_META, statusLabel, statusTone, TONE_CLASSES,
} from './status'

const schemas = openapi.components.schemas
const PIPELINE: string[] = schemas.JobRow.properties.pipeline_status.enum
const EDITABLE: string[] = schemas.StatusChange.properties.status.enum
const BY_STATUS: string[] = Object.keys(schemas.ByStatus.properties)
const SUBMITTED = ['applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn']

describe('STATUS_META', () => {
  it('covers exactly the statuses the API can send', () => {
    expect(Object.keys(STATUS_META).sort()).toEqual([...PIPELINE].sort())
  })

  it('gives every status a label, a known tone and its own place in the order', () => {
    for (const [status, meta] of Object.entries(STATUS_META)) {
      expect(meta.label.trim(), status).not.toBe('')
      expect(TONE_CLASSES, status).toHaveProperty(meta.tone)
    }
    const orders = Object.values(STATUS_META).map((m) => m.order)
    expect(new Set(orders).size).toBe(orders.length)
  })

  it('uses the words from docs/dashboard.md', () => {
    expect(statusLabel('scored')).toBe('Below threshold')
    expect(statusLabel('shortlisted')).toBe('Not applied')
    expect(statusLabel('tailored')).toBe('Drafted, waiting for review')
    expect(statusLabel('drafted')).toBe('Drafted, waiting for review')
    expect(statusLabel('approved')).toBe('Ready to apply')
    expect(statusLabel('in_progress')).toBe('In progress')
    expect(statusLabel('scam_blocked')).toBe('Scam-blocked')
    expect(statusLabel('new')).toBe('Waiting to be scored')
  })

  it('orders the pipeline from found to closed', () => {
    const byOrder = Object.entries(STATUS_META).sort(([, a], [, b]) => a.order - b.order).map(([s]) => s)
    expect(byOrder.indexOf('new')).toBe(0)
    expect(byOrder.indexOf('shortlisted')).toBeLessThan(byOrder.indexOf('approved'))
    expect(byOrder.indexOf('approved')).toBeLessThan(byOrder.indexOf('applied'))
    expect(byOrder.indexOf('applied')).toBeLessThan(byOrder.indexOf('interview'))
    expect(byOrder.indexOf('interview')).toBeLessThan(byOrder.indexOf('offer'))
  })

  it('still shows something for a status this build does not know', () => {
    expect(statusLabel('on_hold')).toBe('on hold')
    expect(statusTone('on_hold')).toBe('neutral')
    expect(statusTone('offer')).toBe('success')
    expect(statusTone('rejected')).toBe('danger')
  })
})

describe('EDITABLE_STATUSES', () => {
  it('is exactly what set_application_status accepts', () => {
    expect([...EDITABLE_STATUSES].sort()).toEqual([...EDITABLE].sort())
    expect(EDITABLE_STATUSES).toHaveLength(8)
    expect(EDITABLE_STATUSES).not.toContain('drafted')
  })
})

describe('isSubmitted', () => {
  it.each(PIPELINE)('%s', (status) => {
    expect(isSubmitted(status)).toBe(SUBMITTED.includes(status))
  })
})

describe('needsConfirm', () => {
  const pairs = PIPELINE.flatMap((from) => EDITABLE.map((to) => [from, to] as const))

  it.each(pairs)('%s -> %s', (from, to) => {
    const expected = SUBMITTED.includes(from) && (to === 'approved' || to === 'skipped')
    expect(needsConfirm(from, to)).toBe(expected)
  })

  it('spells out the cases that matter', () => {
    expect(needsConfirm('applied', 'approved')).toBe(true)
    expect(needsConfirm('rejected', 'skipped')).toBe(true)
    expect(needsConfirm('shortlisted', 'skipped')).toBe(false)
    expect(needsConfirm('approved', 'applied')).toBe(false)
    expect(needsConfirm('interview', 'rejected')).toBe(false)
  })
})

describe('DONUT_SLICES', () => {
  it('has one slice per by_status key, each with a label and its own colour', () => {
    expect(DONUT_SLICES.map((s) => s.key).sort()).toEqual([...BY_STATUS].sort())
    const colors = DONUT_SLICES.map((s) => s.color.toLowerCase())
    expect(new Set(colors).size).toBe(colors.length)
    for (const slice of DONUT_SLICES) {
      expect(slice.color).toMatch(/^#[0-9a-f]{6}$/i)
      expect(slice.label.trim()).not.toBe('')
    }
  })

  it('labels the shortlist slice "Not applied"', () => {
    expect(DONUT_SLICES.find((s) => s.key === 'not_applied')?.label).toBe('Not applied')
  })
})
