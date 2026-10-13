import type { EmailItem, EmailSignal } from '@/api/types'
import { statusLabel } from '@/lib/status'

/** What a reply looks like, in the words a person would use ("looks like ..."). */
export const SIGNAL_WORDS: Record<EmailSignal, string> = {
  acknowledgement: 'a confirmation that your application was received',
  assessment: 'an assessment to take',
  interview: 'an interview invitation',
  offer: 'a job offer',
  rejection: 'a rejection',
}

/** The flags the mail reader can raise, in plain words. A flag this table does not know is shown as it is (the text is escaped by React). */
export const FLAG_LABELS: Record<string, string> = {
  conflict: 'Contradicts what the job is marked as now',
  phishing_risk: 'Possible phishing: check who sent it',
  unverified_sender: 'Google could not verify the sender',
  sender_domain_not_the_company: "The sender's domain is not the company's",
  free_mail_sender: 'Sent from a free mail address',
  display_name_only: 'Only the sender\'s display name matches the company',
  suspicious_link: 'Contains a link that does not look like the company\'s',
  scam_pattern: 'Wording that scams use (money, deposits, urgency)',
  injection_attempt: 'Contains text aimed at software that reads it',
  markup_doubt: 'Odd or hidden formatting: the text shown may not be what you would see',
  unfamiliar_domain_ending: 'An unusual domain ending',
  domain_matches_first_word_only: "Only the first word of the company's name matches the domain",
  role_mismatch: "The role named in the email is not this job's: check it is the one you applied for",
  template_mixed_signals: 'A template that says thank you and no in one',
  date_from_email: "The submission date is taken from this email's date",
  classified_by_model: 'Read by Claude, not by plain rules',
}

export function flagLabel(flag: string): string {
  return FLAG_LABELS[flag] ?? flag
}

/** Flags that deserve attention, as opposed to notes. They match what the database asks a second look for (email_needs_ack), plus
 * role_mismatch, which the database covers through the low confidence it always comes with. */
const CARE = new Set([
  'conflict', 'phishing_risk', 'markup_doubt', 'free_mail_sender', 'suspicious_link', 'scam_pattern', 'injection_attempt', 'sender_domain_not_the_company',
  'unverified_sender', 'display_name_only', 'unfamiliar_domain_ending', 'domain_matches_first_word_only',
  'role_mismatch',
])

export function isCareFlag(flag: string): boolean {
  return CARE.has(flag)
}

/** "Looks like an interview invitation. Accepting moves this job from Applied to Interview." */
export function proposalSentence(item: Pick<EmailItem, 'signal' | 'proposed_status' | 'current_status'>): string {
  return `Looks like ${SIGNAL_WORDS[item.signal]}. Accepting marks this job ${statusLabel(item.proposed_status)} (it is ${statusLabel(item.current_status)} now).`
}

export const CONFIDENCE_LABEL = { high: 'High confidence', medium: 'Medium confidence', low: 'Low confidence' } as const
