import { Loader2 } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { useRefreshAfterTailor, useStartTailor, useTailorEstimate, useTailorStatus } from '@/api/hooks'
import type { JobDetail } from '@/api/types'
import { Button } from '@/components/ui/button'
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { canOfferTailor, coverageText, describeOutcome, replaceWarning, usd } from '@/lib/tailor'
import { cn } from '@/lib/utils'

type Job = Pick<JobDetail, 'id' | 'score' | 'job_status' | 'has_resume' | 'has_cover_letter' | 'application_status'>

/** The confirmation: what it costs (typically, and at most), what is left in the budget, what it replaces. Nothing starts before "Tailor now". */
function ConfirmTailor({ job, open, onClose }: { job: Job; open: boolean; onClose: () => void }) {
  const estimate = useTailorEstimate(job.id, open)
  const start = useStartTailor(job.id)
  const e = estimate.data
  const ready = !!e && e.can_run && !start.isPending

  function go() {
    if (!ready || !e) return                 // not before the cost is known, not when the scraper said no, not twice
    start.mutate(e.at_most_usd, { onSuccess: onClose })
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !start.isPending) onClose() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{job.has_resume ? 'Tailor this job again?' : 'Tailor this job?'}</DialogTitle>
          <DialogDescription>
            Claude drafts a resume and cover letter for this one posting from the facts in your resume, and the result is checked against your resume where it can be. It usually takes about a minute.
          </DialogDescription>
        </DialogHeader>
        {estimate.isPending && <p role="status" className="text-sm text-muted-foreground">Checking the cost…</p>}
        {estimate.isError && (
          <p role="alert" className="text-sm text-destructive">
            The cost could not be checked{estimate.error instanceof ApiError ? `: ${estimate.error.detail}` : ''}.
          </p>
        )}
        {e && (
          <div className="grid gap-3 text-sm">
            {e.can_run ? (
              e.uses_model ? (
                <p>
                  <span className="font-medium">Costs about {usd(e.typical_usd)}, at most {usd(e.at_most_usd)}.</span>{' '}
                  <span className="text-muted-foreground">
                    The most is what the spending limit holds back before it starts; you are charged for what is used.
                  </span>
                </p>
              ) : (
                <p><span className="font-medium">Costs nothing.</span> <span className="text-muted-foreground">No call to Claude: your resume is used as it is, because this job already scores high.</span></p>
              )
            ) : (
              <div role="alert" className="grid gap-1 rounded-lg border border-destructive/30 bg-destructive/5 p-2">
                <p>{e.reason}</p>
                {e.reason?.includes('switched off') && (
                  <p className="text-xs text-muted-foreground">
                    Turn Tailoring on under Settings, Optional parts. If you do not want it automatic, set its mode to Manual under Settings, Tailoring.
                  </p>
                )}
              </div>
            )}
            {e.uses_model && (
              <p className="text-muted-foreground">
                Left in the budget: {usd(e.budget.day_remaining)} today, {usd(e.budget.month_remaining)} this month. Model: {e.model}.
              </p>
            )}
            {e.existing && (
              <p className="rounded-lg bg-amber-50 p-2 text-amber-900 ring-1 ring-amber-200 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-900">
                This replaces the resume and cover letter made earlier (status: {e.existing.status.replace(/_/g, ' ')}). {replaceWarning(e.existing.status)}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              Nothing is sent to an employer. {e.mode === 'manual' ? 'Automatic tailoring is off, so this runs only because you asked.' : 'You can also leave tailoring automatic: this just does this one job now.'}
            </p>
          </div>
        )}
        {start.isError && (
          <p role="alert" className="text-sm text-destructive">{start.error instanceof ApiError ? start.error.detail : 'It could not be started.'}</p>
        )}
        <DialogFooter>
          <DialogClose asChild><Button type="button" variant="outline" disabled={start.isPending}>Cancel</Button></DialogClose>
          <Button type="button" onClick={go} aria-disabled={!ready} className="aria-disabled:pointer-events-none aria-disabled:opacity-50">
            {start.isPending && <Loader2 className="animate-spin" aria-hidden="true" />}
            {e && e.uses_model ? `Tailor now (up to ${usd(e.at_most_usd)})` : 'Tailor now'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * "Tailor this job": on demand, for the one job in the panel, whatever the tailoring mode is (manual means nothing happens unless
 * you ask, which is exactly this). A click opens the confirmation; "Tailor now" starts a run in the background that the page watches;
 * it keeps running if the panel is closed, and the result is here when the panel is opened again.
 */
export default function TailorSection({ job }: { job: Job }) {
  const status = useTailorStatus(job.id)
  const refresh = useRefreshAfterTailor()
  const [asking, setAsking] = useState(false)
  const state = status.data?.state
  const was = useRef<string | undefined>(undefined)

  // a run that finishes while this panel is open: show it everywhere (files, board, numbers, history) and say so
  useEffect(() => {
    if (was.current === 'running' && state === 'idle') {
      // the service forgot the run: it was restarted while this one was going. Say so, and look at what is there now.
      void refresh(job.id)
      toast.warning('Tailoring was interrupted (the service restarted?). Check the job\'s files; if it cost anything, Spend shows it.')
    }
    if (was.current === 'running' && (state === 'done' || state === 'failed')) {
      void refresh(job.id)
      const result = status.data?.result
      if (state === 'done' && result) {
        const o = describeOutcome(result)
        if (o.tone === 'bad') toast.warning(o.text)
        else toast.success(`Tailored. ${o.text}`)
      } else if (state === 'failed') {
        toast.error(`Tailoring did not finish.${status.data?.cost_usd ? ` It cost ${usd(status.data.cost_usd)}.` : ''}`)
      }
    }
    was.current = state
  }, [state, status.data?.result, status.data?.cost_usd, status.data?.error, job.id, refresh])

  if (!canOfferTailor(job)) return null
  const running = state === 'running'
  const outcome = state === 'done' && status.data?.result ? describeOutcome(status.data.result) : null
  const coverage = state === 'done' ? coverageText(status.data?.result?.requirements) : ''
  const attention = state === 'done' ? (status.data?.result?.attention ?? []) : []

  return (
    <div className="space-y-2">
      {running ? (
        <p role="status" className="flex items-center gap-2">
          <Loader2 className="size-4 animate-spin" aria-hidden="true" />
          Tailoring this job… usually about a minute. You can close this panel: it keeps going.
        </p>
      ) : (
        <>
          <Button type="button" size="sm" variant="outline" onClick={() => setAsking(true)}>
            {job.has_resume ? 'Tailor again…' : 'Tailor this job…'}
          </Button>
          <p className="text-xs text-muted-foreground">
            Writes a resume and cover letter for this posting only. It works even when automatic tailoring is off (Settings, Tailoring, mode Manual); it asks you first and shows the cost.
          </p>
        </>
      )}
      {outcome && (
        <div
          role="status"
          className={cn('rounded-lg p-2 text-sm ring-1', outcome.tone === 'good' && 'bg-emerald-50 text-emerald-900 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-200 dark:ring-emerald-900',
            outcome.tone === 'attention' && 'bg-amber-50 text-amber-900 ring-amber-200 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-900',
            outcome.tone === 'bad' && 'bg-red-50 text-red-900 ring-red-200 dark:bg-red-950 dark:text-red-200 dark:ring-red-900')}
        >
          <p className="font-medium">{outcome.text}</p>
          {coverage && <p>{coverage}</p>}
          {attention.length > 0 && (
            <ul className="mt-1 list-disc pl-5">
              {attention.map((line, i) => <li key={i} dir="auto">{line}</li>)}
            </ul>
          )}
        </div>
      )}
      {state === 'failed' && (
        <p role="alert" className="rounded-lg bg-red-50 p-2 text-sm text-red-900 ring-1 ring-red-200 dark:bg-red-950 dark:text-red-200 dark:ring-red-900" dir="auto">
          Tailoring did not finish: {status.data?.error ?? 'something went wrong'}{status.data?.cost_usd ? `. It cost ${usd(status.data.cost_usd)}.` : ''}
        </p>
      )}
      {asking && <ConfirmTailor job={job} open onClose={() => setAsking(false)} />}
    </div>
  )
}
