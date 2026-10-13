import { describe, expect, it } from 'vitest'
import { CONFIDENCE_LABEL, FLAG_LABELS, flagLabel, isCareFlag, proposalSentence, SIGNAL_WORDS } from './email'

describe('the email words', () => {
  it('has a plain sentence for every signal an email may carry, and for nothing else', () => {
    expect(Object.keys(SIGNAL_WORDS).sort()).toEqual(['acknowledgement', 'assessment', 'interview', 'offer', 'rejection'])
  })

  it('describes what accepting would do, in the job\'s own status words', () => {
    expect(proposalSentence({ signal: 'rejection', proposed_status: 'rejected', current_status: 'interview' }))
      .toBe('Looks like a rejection. Accepting marks this job Rejected (it is Interview now).')
  })

  it('explains the flags in words, and shows an unknown flag as it is', () => {
    expect(flagLabel('phishing_risk')).toBe('Possible phishing: check who sent it')
    expect(flagLabel('a_flag_from_the_future')).toBe('a_flag_from_the_future')
    expect(Object.values(FLAG_LABELS).every((v) => v.length > 8)).toBe(true)
  })

  it('treats exactly the flags the database asks a second look for as care flags', () => {
    for (const f of ['conflict', 'phishing_risk', 'markup_doubt', 'free_mail_sender', 'suspicious_link', 'scam_pattern', 'injection_attempt', 'sender_domain_not_the_company',
      'unverified_sender', 'display_name_only', 'unfamiliar_domain_ending', 'domain_matches_first_word_only', 'role_mismatch']) {
      expect(isCareFlag(f)).toBe(true)
    }
    for (const f of ['date_from_email', 'classified_by_model', 'template_mixed_signals']) expect(isCareFlag(f)).toBe(false)
  })

  it('names the three confidences', () => {
    expect(CONFIDENCE_LABEL).toEqual({ high: 'High confidence', medium: 'Medium confidence', low: 'Low confidence' })
  })
})
