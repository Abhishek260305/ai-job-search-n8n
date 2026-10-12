import { describe, expect, it } from 'vitest'
import type { BudgetCaps, ScheduleSetting, TailoringSettings } from '@/api/types'
import {
  budgetChanges, capsForm, everyHint, scheduleChanges, scheduleForm, splitMinutes, tailoringChanges, tailoringForm, toMinutes, wholeNumber,
} from './settingsForm'

const schedule = (over: Partial<ScheduleSetting> = {}): ScheduleSetting => ({
  name: 'score', enabled: true, every_minutes: 30, at_time: null, batch: 25, model: 'claude-haiku-5-5', board_batch: null,
  run_now: false, last_run_at: null, next_run_at: null, ...over,
})

describe('minutes and units', () => {
  it('splits into the biggest unit that divides evenly', () => {
    expect(splitMinutes(360)).toEqual({ value: '6', unit: 'hours' })
    expect(splitMinutes(30)).toEqual({ value: '30', unit: 'minutes' })
    expect(splitMinutes(90)).toEqual({ value: '90', unit: 'minutes' })
    expect(splitMinutes(2880)).toEqual({ value: '2', unit: 'days' })
    expect(splitMinutes(null)).toEqual({ value: '', unit: 'hours' })
  })

  it('turns what is typed into whole minutes, or nothing', () => {
    expect(toMinutes('6', 'hours')).toBe(360)
    expect(toMinutes('1.5', 'hours')).toBe(90)
    expect(toMinutes('2.05', 'hours')).toBe(123)                          // floating point says 122.99999999999999
    expect(toMinutes('8.20', 'hours')).toBe(492)
    expect(toMinutes('0.25', 'days')).toBe(360)
    expect(toMinutes('45', 'minutes')).toBe(45)
    expect(toMinutes('2', 'days')).toBe(2880)
    for (const bad of ['', 'abc', '0', '-5', '1.234', '0.5', '1e3', '99999999']) expect(toMinutes(bad, 'minutes')).toBeNull()
    expect(toMinutes('0.01', 'hours')).toBeNull()                         // 0.6 minutes is not whole
  })

  it('reads whole numbers only', () => {
    expect(wholeNumber(' 25 ')).toBe(25)
    for (const bad of ['', '2.5', '-1', 'x', '1e3', '1234567']) expect(wholeNumber(bad)).toBeNull()
  })

  it('describes a range', () => {
    expect(everyHint(15, 10080)).toBe('15 minutes to 7 days')
    expect(everyHint(60, 10080)).toBe('1 hour to 7 days')
  })
})

describe('scheduleChanges', () => {
  it('sends nothing when nothing changed', () => {
    const s = schedule()
    expect(scheduleChanges(s, scheduleForm(s))).toEqual({ changes: {}, error: null })
  })

  it('sends only what changed', () => {
    const s = schedule()
    const form = { ...scheduleForm(s), enabled: false, batch: '10', every: { value: '1', unit: 'hours' as const } }
    expect(scheduleChanges(s, form).changes).toEqual({ enabled: false, batch: 10, every_minutes: 60 })
  })

  it('knows the boards only for collect, and never sends a model', () => {
    const collect = schedule({ name: 'collect', every_minutes: 360, batch: 18, model: null, board_batch: 35 })
    expect(scheduleChanges(collect, { ...scheduleForm(collect), boardBatch: '40' }).changes).toEqual({ board_batch: 40 })
    expect(scheduleChanges(collect, { ...scheduleForm(collect), boardBatch: '' }).error).toMatch(/Boards and feeds/)
    const score = schedule()
    expect(Object.keys(scheduleForm(score))).not.toContain('model')
    expect(scheduleChanges(score, scheduleForm(score)).changes).toEqual({})
  })

  it('does not block a collect card whose scraper does not report board_batch yet', () => {
    const collect = schedule({ name: 'collect', every_minutes: 360, batch: 18, model: null, board_batch: null })
    expect(scheduleChanges(collect, { ...scheduleForm(collect), batch: '20' })).toEqual({ changes: { batch: 20 }, error: null })
  })

  it('refuses text that is not a number before sending anything', () => {
    const s = schedule()
    expect(scheduleChanges(s, { ...scheduleForm(s), batch: 'ten' }).error).toMatch(/whole number/)
    expect(scheduleChanges(s, { ...scheduleForm(s), every: { value: 'soon', unit: 'hours' } }).error).toMatch(/How often/)
  })

  describe('the digest, daily or every N', () => {
    const daily = schedule({ name: 'digest', every_minutes: null, at_time: '08:00:00', batch: 10, model: null })
    const hourly = schedule({ name: 'digest', every_minutes: 360, at_time: null, batch: 10, model: null })

    it('keeps a daily time as it is, and sends a new one', () => {
      expect(scheduleChanges(daily, scheduleForm(daily)).changes).toEqual({})
      expect(scheduleChanges(daily, { ...scheduleForm(daily), atTime: '9:30' }).changes).toEqual({ at_time: '9:30' })
      expect(scheduleChanges(daily, { ...scheduleForm(daily), atTime: '25:00' }).error).toMatch(/HH:MM/)
    })

    it('sends the time when switching from every-N to daily, even at the same text', () => {
      expect(scheduleChanges(hourly, { ...scheduleForm(hourly), daily: true, atTime: '08:00' }).changes).toEqual({ at_time: '08:00' })
    })

    it('sends the interval when switching from daily to every-N', () => {
      const form = { ...scheduleForm(daily), daily: false, every: { value: '6', unit: 'hours' as const } }
      expect(scheduleChanges(daily, form).changes).toEqual({ every_minutes: 360 })
    })
  })
})

const tailoring = (over: Partial<TailoringSettings> = {}): TailoringSettings => ({
  mode: 'auto', min_score: null, max_score: 80, daily_limit: 5, approval: 'review', cover_letter: true, max_pages: 2, paper: 'a4',
  model: 'claude-sonnet-5-5', ...over,
})

describe('tailoringChanges', () => {
  it('sends nothing when nothing changed', () => {
    const t = tailoring()
    expect(tailoringChanges(t, tailoringForm(t))).toEqual({ changes: {}, error: null })
  })

  it('sends changed values, and clears an emptied optional number', () => {
    const t = tailoring()
    const form = { ...tailoringForm(t), mode: 'manual' as const, minScore: '70', maxScore: '', dailyLimit: '3', coverLetter: false, maxPages: '1', paper: 'letter' as const }
    expect(tailoringChanges(t, form).changes).toEqual({
      mode: 'manual', min_score: 70, daily_limit: 3, cover_letter: false, max_pages: 1, paper: 'letter', clear: ['max_score'],
    })
  })

  it('does not clear what is already none', () => {
    const t = tailoring({ min_score: null })
    expect(tailoringChanges(t, { ...tailoringForm(t), minScore: '' }).changes).toEqual({})
  })

  it('refuses a number that is not whole', () => {
    const t = tailoring()
    expect(tailoringChanges(t, { ...tailoringForm(t), dailyLimit: '2.5' }).error).toMatch(/daily limit must be a whole number/)
    expect(tailoringChanges(t, { ...tailoringForm(t), minScore: 'high' }).error).toMatch(/lowest score/)
  })
})

const caps: BudgetCaps = { monthly_cap_usd: 10, daily_cap_usd: 1.5, warn_pct: 80, apify_monthly_cap_usd: 15, apify_daily_cap_usd: 0.5 }

describe('budgetChanges: caps only go down', () => {
  it('sends nothing when nothing changed', () => {
    expect(budgetChanges(caps, capsForm(caps))).toEqual({ changes: {}, error: null })
  })

  it('sends the caps that are lower', () => {
    expect(budgetChanges(caps, { ...capsForm(caps), monthly_cap_usd: '7.5', apify_daily_cap_usd: '0.25' }).changes)
      .toEqual({ monthly_cap_usd: 7.5, apify_daily_cap_usd: 0.25 })
  })

  it('refuses a raise, and says how to do it properly', () => {
    const r = budgetChanges(caps, { ...capsForm(caps), daily_cap_usd: '1.51' })
    expect(r.changes).toEqual({})
    expect(r.error).toMatch(/Claude, per day can only be lowered here.*scripts\/jobsearch budget set/)
  })

  it('refuses a raise even when another cap went down', () => {
    expect(budgetChanges(caps, { ...capsForm(caps), monthly_cap_usd: '5', apify_monthly_cap_usd: '20' }).error).toMatch(/Apify, per month/)
  })

  it('refuses empty and malformed amounts', () => {
    expect(budgetChanges(caps, { ...capsForm(caps), monthly_cap_usd: '' }).error).toMatch(/enter an amount/)
    for (const bad of ['abc', '-1', '1,5', '1e3', '$5']) expect(budgetChanges(caps, { ...capsForm(caps), monthly_cap_usd: bad }).error).toMatch(/dollars/)
  })

  it('does not lock the form on a stored cap that prints oddly, as long as it is left alone', () => {
    const odd: BudgetCaps = { ...caps, apify_daily_cap_usd: 1e-7, monthly_cap_usd: 1234.56789 }
    const form = capsForm(odd)
    expect(form.apify_daily_cap_usd).toBe('0')
    expect(form.monthly_cap_usd).toBe('1234.568')
    expect(budgetChanges(odd, form)).toEqual({ changes: {}, error: null })
    expect(budgetChanges(odd, { ...form, daily_cap_usd: '1' }).changes).toEqual({ daily_cap_usd: 1 })
  })

  it('lets a cap go to zero', () => {
    expect(budgetChanges(caps, { ...capsForm(caps), apify_daily_cap_usd: '0' }).changes).toEqual({ apify_daily_cap_usd: 0 })
  })
})
