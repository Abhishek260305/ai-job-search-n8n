import { describe, expect, it } from 'vitest'
import { canOfferTailor, coverageText, describeOutcome, replaceWarning, usd } from '@/lib/tailor'

describe('who is offered the button', () => {
  it('offers it for a scored job that has not been submitted', () => {
    for (const job_status of ['shortlisted', 'tailored', 'approved', 'scored', 'skipped', 'new'] as const) {
      expect(canOfferTailor({ score: 80, job_status }), job_status).toBe(true)
    }
  })
  it('does not when the job still looks open but its application was submitted', () => {
    for (const application_status of ['applied', 'in_progress', 'interview', 'offer', 'rejected', 'withdrawn']) {
      expect(canOfferTailor({ score: 80, job_status: 'scored', application_status }), application_status).toBe(false)
    }
    expect(canOfferTailor({ score: 80, job_status: 'scored', application_status: 'drafted' })).toBe(true)
    expect(canOfferTailor({ score: 80, job_status: 'scored', application_status: null })).toBe(true)
  })
  it('does not for a job that was never scored, is blocked, filtered out, expired or already applied', () => {
    expect(canOfferTailor({ score: null, job_status: 'shortlisted' })).toBe(false)
    for (const job_status of ['scam_blocked', 'filtered_out', 'expired', 'applied'] as const) {
      expect(canOfferTailor({ score: 80, job_status }), job_status).toBe(false)
    }
  })
})

describe('money', () => {
  it('is shown in cents, and a tiny cost is not shown as zero', () => {
    expect(usd(0.047)).toBe('$0.05')
    expect(usd(0.31)).toBe('$0.31')
    expect(usd(1.4)).toBe('$1.40')
    expect(usd(0)).toBe('$0.00')
    expect(usd(0.002)).toBe('under $0.01')
  })
})

describe('what a finished run says', () => {
  it('is good news when it is ready, and a flag when something needs a look', () => {
    expect(describeOutcome({ status: 'drafted', needs_attention: false, cost_usd: 0.047, attention: [] }))
      .toEqual({ tone: 'good', text: 'Ready for your review. It cost $0.05.' })
    expect(describeOutcome({ status: 'approved', needs_attention: true, cost_usd: 0.05, attention: ['x'] }).tone).toBe('attention')
    expect(describeOutcome({ status: 'approved', needs_attention: false, cost_usd: 0, attention: [] }).text).toBe('Ready to apply.')
  })
  it('says plainly when nothing was spent', () => {
    expect(describeOutcome({ status: 'budget_paused', reason: 'not enough budget left for this job', attention: [] }))
      .toEqual({ tone: 'bad', text: 'Not run: not enough budget left for this job. Nothing was spent.' })
    expect(describeOutcome({ status: 'api_error', reason: 'overloaded', attention: [] }).text).toContain('Nothing was spent')
  })
  it('says what it cost when it failed after spending', () => {
    expect(describeOutcome({ status: 'failed', reason: 'the draft was not valid', cost_usd: 0.03, attention: [] }).text)
      .toBe('It did not work: the draft was not valid. It cost $0.03.')
  })
  it('names an unknown outcome instead of hiding it', () => {
    expect(describeOutcome({ status: 'something_new', attention: [] }).text).toContain('something new')
  })
})

describe('how much of the posting is covered', () => {
  it('is a sentence, and empty when there is nothing to say', () => {
    expect(coverageText({ matched: 4, bridged: 1, gap: 0 })).toBe("Matches 4 of the posting's 5 requirements, 1 close.")
    expect(coverageText({ matched: 3, bridged: 0, gap: 2 })).toBe("Matches 3 of the posting's 5 requirements, 2 not covered.")
    expect(coverageText({ matched: 0, bridged: 0, gap: 0 })).toBe('')
    expect(coverageText(null)).toBe('')
    expect(coverageText(undefined)).toBe('')
  })
})


describe('what re-tailoring does to a decision', () => {
  it('says so for approved and skipped, and nothing for a draft', () => {
    expect(replaceWarning('approved')).toContain('approval and note are cleared')
    expect(replaceWarning('skipped')).toContain('no longer skipped')
    expect(replaceWarning('drafted')).toBe('')
  })
})
