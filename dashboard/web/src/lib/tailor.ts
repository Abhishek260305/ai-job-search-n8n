// Tailoring one job on request (dashboard version 3c): who is offered the button, and how a run is described.
import type { JobDetail, TailorOutcome } from '@/api/types'
import { isSubmitted, statusLabel } from '@/lib/status'

/**
 * Jobs that can be tailored: scored, not blocked or filtered out or expired, and neither the job nor its application submitted
 * (the job's own status can lag behind the application's). The scraper decides again.
 */
export function canOfferTailor(job: Pick<JobDetail, 'score' | 'job_status'> & { application_status?: string | null }): boolean {
  if (job.score === null) return false
  if (['scam_blocked', 'filtered_out', 'expired', 'applied'].includes(job.job_status)) return false
  return !(job.application_status && isSubmitted(job.application_status))
}

/** What re-tailoring does to a decision already made: a drafted application only has its files replaced; approving or skipping is undone. */
export function replaceWarning(status: string): string {
  if (status === 'approved') return 'Your approval and note are cleared: it goes back to review (unless approval is automatic).'
  if (status === 'skipped') return 'It is no longer skipped: it goes back to review.'
  return ''
}

/** Dollars as the page shows them: "$0.05", never "$0.0471234". Under a cent shows as "under $0.01". */
export function usd(value: number): string {
  if (value > 0 && value < 0.005) return 'under $0.01'
  return `$${value.toFixed(2)}`
}

/** One line for a finished run, in words. */
export function describeOutcome(result: TailorOutcome): { tone: 'good' | 'attention' | 'bad'; text: string } {
  const cost = result.cost_usd ? ` It cost ${usd(result.cost_usd)}.` : ''
  switch (result.status) {
    case 'drafted':
      return { tone: result.needs_attention ? 'attention' : 'good', text: `Ready for your review.${cost}` }
    case 'approved':
      return { tone: result.needs_attention ? 'attention' : 'good', text: `Ready to apply.${cost}` }
    case 'budget_paused':
      return { tone: 'bad', text: `Not run: ${result.reason ?? 'not enough budget left for this job'}. Nothing was spent.` }
    case 'api_error':
      return { tone: 'bad', text: `Claude could not be reached${result.reason ? ` (${result.reason})` : ''}.${cost || ' Nothing was spent.'}` }
    case 'failed':
      return { tone: 'bad', text: `It did not work${result.reason ? `: ${result.reason}` : ''}.${cost}` }
    default:
      return { tone: 'attention', text: `${statusLabel(result.status)}.${cost}` }
  }
}

/** The covered / partly / missing counts of the posting's requirements, as a sentence; empty when there are none. */
export function coverageText(requirements: TailorOutcome['requirements']): string {
  if (!requirements) return ''
  // the tailoring service counts requirements as matched (in the resume), bridged (close) and gap (not covered)
  const covered = requirements.matched ?? 0
  const partly = requirements.bridged ?? 0
  const missing = requirements.gap ?? 0
  if (covered + partly + missing === 0) return ''
  return `Matches ${covered} of the posting's ${covered + partly + missing} requirements${partly ? `, ${partly} close` : ''}${missing ? `, ${missing} not covered` : ''}.`
}
