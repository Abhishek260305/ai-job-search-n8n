import { describe, expect, it } from 'vitest'
import { cadence, chartData, chartDescription, dateIn, formatMoney, meter, seriesKey, stageLabel } from './ops'

describe('stageLabel and cadence', () => {
  it('names the four stages and falls back to the raw name', () => {
    expect(stageLabel('collect')).toBe('Collect jobs')
    expect(stageLabel('digest')).toBe('Send the digest')
    expect(stageLabel('new_thing')).toBe('new thing')
  })

  it('says how often a stage runs', () => {
    expect(cadence({ enabled: true, every_minutes: 360, at_time: null })).toBe('every 6 h')
    expect(cadence({ enabled: true, every_minutes: 30, at_time: null })).toBe('every 30 min')
    expect(cadence({ enabled: true, every_minutes: 90, at_time: null })).toBe('every 90 min')
    expect(cadence({ enabled: true, every_minutes: null, at_time: '08:00:00' })).toBe('daily at 08:00')
    expect(cadence({ enabled: false, every_minutes: 30, at_time: null })).toBe('off')
    expect(cadence({ enabled: true, every_minutes: null, at_time: null })).toBe('—')
  })
})

describe('formatMoney', () => {
  it('shows cents, and fractions of a cent for single runs', () => {
    expect(formatMoney(10)).toBe('$10.00')
    expect(formatMoney(0.53)).toBe('$0.53')
    expect(formatMoney(0.0164)).toBe('$0.016')
    expect(formatMoney(0)).toBe('$0.00')
    expect(formatMoney(null)).toBe('—')
    expect(formatMoney(Number.NaN)).toBe('—')
  })
})

describe('meter', () => {
  it('turns amber at the warning line and red at the cap', () => {
    expect(meter(2, 10, 80)).toEqual({ pct: 20, width: 20, tone: 'ok' })
    expect(meter(8, 10, 80).tone).toBe('warn')
    expect(meter(10, 10, 80)).toEqual({ pct: 100, width: 100, tone: 'bad' })
    expect(meter(12, 10, 80)).toEqual({ pct: 120, width: 100, tone: 'bad' })
  })

  it('has no warning line for Apify, and the server\'s blocked flag always wins', () => {
    expect(meter(9, 10, null).tone).toBe('ok')
    expect(meter(1, 10, 80, true).tone).toBe('bad')
  })

  it('judges the exact share, not the rounded one', () => {
    expect(meter(9.96, 10, 80)).toMatchObject({ pct: 100, tone: 'warn' })      // reads "100%" but the cap is not reached
    expect(meter(7.96, 10, 80)).toMatchObject({ pct: 80, tone: 'ok' })         // reads "80%" but the warning line is 80.0
    expect(meter(8, 10, 80).tone).toBe('warn')
  })

  it('copes with a cap of zero', () => {
    expect(meter(0, 0, 80)).toEqual({ pct: 0, width: 0, tone: 'ok' })
    expect(meter(0.1, 0, 80).tone).toBe('bad')
  })
})

const spend = (daily: Parameters<typeof chartData>[0]['daily']) => ({ daily, generated_at: '2026-10-12T05:00:00Z', timezone: 'Asia/Kolkata' })

describe('chartData', () => {
  it('gives one point per day ending today in the server\'s time zone, with empty days as zero', () => {
    const { points } = chartData(spend([]))
    expect(points).toHaveLength(30)
    expect(points[29].day).toBe('2026-10-12')
    expect(points[0].day).toBe('2026-09-13')
    expect(points.every((p) => p.total === 0)).toBe(true)
  })

  it('uses the time zone of the server, not of the browser, for "today"', () => {
    // 20:00 UTC on the 11th is already the 12th in Kolkata
    expect(dateIn('2026-10-11T20:00:00Z', 'Asia/Kolkata')).toBe('2026-10-12')
    expect(dateIn('2026-10-11T20:00:00Z', 'UTC')).toBe('2026-10-11')
    expect(dateIn('2026-10-11T20:00:00Z', 'Not/AZone')).toBe('2026-10-11')
  })

  it('stacks purposes, sums a day, and puts Apify after Claude\'s purposes', () => {
    const { points, series, total } = chartData(spend([
      { day: '2026-10-12', provider: 'anthropic', purpose: 'score', calls: 10, cost_usd: 0.25 },
      { day: '2026-10-12', provider: 'anthropic', purpose: 'tailor', calls: 1, cost_usd: 0.5 },
      { day: '2026-10-12', provider: 'apify', purpose: 'search', calls: 1, cost_usd: 0.1 },
      { day: '2026-10-11', provider: 'anthropic', purpose: 'score', calls: 4, cost_usd: 0.1 },
    ]))
    expect(series.map((s) => s.label)).toEqual(['Scoring', 'Tailoring', 'Apify'])
    expect(new Set(series.map((s) => s.color)).size).toBe(3)
    expect(points[29]).toMatchObject({ day: '2026-10-12', score: 0.25, tailor: 0.5, 'apify:search': 0.1, total: 0.85 })
    expect(points[28]).toMatchObject({ day: '2026-10-11', score: 0.1, tailor: 0, total: 0.1 })
    expect(total).toBe(0.95)
  })

  it('ignores a day outside the window', () => {
    const { total } = chartData(spend([{ day: '2026-08-01', provider: 'anthropic', purpose: 'score', calls: 1, cost_usd: 9 }]))
    expect(total).toBe(0)
  })

  it('has a stable series key per provider and purpose', () => {
    expect(seriesKey({ provider: 'anthropic', purpose: 'score' })).toBe('score')
    expect(seriesKey({ provider: 'apify', purpose: 'search' })).toBe('apify:search')
  })
})

describe('chartDescription', () => {
  it('says so when nothing was spent', () => {
    const { points, series, total } = chartData(spend([]))
    expect(chartDescription(points, series, total)).toBe('No spending in the last 30 days.')
  })

  it('names the total, the priciest day and the split', () => {
    const { points, series, total } = chartData(spend([
      { day: '2026-10-12', provider: 'anthropic', purpose: 'score', calls: 10, cost_usd: 0.25 },
      { day: '2026-10-11', provider: 'anthropic', purpose: 'tailor', calls: 1, cost_usd: 0.5 },
    ]))
    const text = chartDescription(points, series, total)
    expect(text).toContain('$0.75 in total')
    expect(text).toContain('most on 11 Oct ($0.50)')
    expect(text).toContain('Scoring, Tailoring')
  })
})
