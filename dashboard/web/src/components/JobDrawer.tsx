import { ChevronLeft, ChevronRight, ExternalLink, FileText, Mail } from 'lucide-react'
import { type ReactNode, useId, useState } from 'react'
import { ApiError } from '@/api/client'
import { useJob, useJobs, useSummary } from '@/api/hooks'
import type { JobDetail } from '@/api/types'
import StatusBadge from '@/components/StatusBadge'
import StatusSelect from '@/components/StatusSelect'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { DASH, domainOf, formatDate, formatDateTime, formatRelative, formatSalary, safeUrl } from '@/lib/format'
import {
  descriptionPreview, flagList, formatCost, gateList, neighbours, riskLabel, textList, yearsAsked,
} from '@/lib/jobDetail'
import { useJobsParams } from '@/lib/jobsParams'
import { dimensionScores, scoreTone, sourceLabel } from '@/lib/jobsTable'
import { TONE_CLASSES } from '@/lib/status'
import { cn } from '@/lib/utils'

// Everything below shows posting text as React text children, which React escapes: a posting is untrusted input.

function Section({ title, children }: { title: string; children: ReactNode }) {
  const id = useId()
  return (
    <section aria-labelledby={id} className="space-y-2">
      <h3 id={id} className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{title}</h3>
      {children}
    </section>
  )
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  )
}

function BulletList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null
  return (
    <div>
      <h4 className="mb-1 font-medium">{title}</h4>
      <ul className="list-disc space-y-0.5 pl-5" dir="auto">
        {items.map((item, i) => <li key={i}>{item}</li>)}
      </ul>
    </div>
  )
}

function LinkButton({ href, label, icon }: { href: string; label: string; icon: ReactNode }) {
  return (
    <Button asChild variant="outline" size="sm">
      <a href={href} target="_blank" rel="noopener noreferrer">{icon}{label}</a>
    </Button>
  )
}

function Links({ job }: { job: JobDetail }) {
  const apply = safeUrl(job.apply_url)
  const original = safeUrl(job.canonical_url)
  const links = [
    apply && <LinkButton key="apply" href={apply} label={`Apply on ${domainOf(apply)}`} icon={<ExternalLink aria-hidden="true" />} />,
    original && original !== apply && (
      <LinkButton key="original" href={original} label={`Original posting (${domainOf(original)})`} icon={<ExternalLink aria-hidden="true" />} />
    ),
    job.has_resume && <LinkButton key="resume" href={`/api/jobs/${job.id}/files/resume`} label="Resume (PDF)" icon={<FileText aria-hidden="true" />} />,
    job.has_cover_letter && (
      <LinkButton key="cover" href={`/api/jobs/${job.id}/files/cover_letter`} label="Cover letter (PDF)" icon={<Mail aria-hidden="true" />} />
    ),
  ].filter(Boolean)
  if (!links.length) return null
  return <div className="flex flex-wrap gap-2">{links}</div>
}

function WhyThisScore({ job, threshold }: { job: JobDetail; threshold: number | null }) {
  const dims = dimensionScores(job.scores)
  const gates = gateList(job.gates)
  const yoe = yearsAsked(job.yoe_min, job.yoe_max)
  return (
    <Section title="Why this score">
      {job.filter_reason && <p><span className="font-medium">Filtered out:</span> {job.filter_reason}</p>}
      {job.score === null ? (
        <p className="text-muted-foreground">Not scored yet.</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn('inline-flex h-7 min-w-10 items-center justify-center rounded-md px-2 text-sm font-semibold tabular-nums', TONE_CLASSES[scoreTone(job.score, threshold)])}>
              {job.score}
            </span>
            {job.verdict && <span className="font-medium" dir="auto">{job.verdict}</span>}
            {threshold !== null && <span className="text-muted-foreground">threshold {threshold}</span>}
          </div>
          {dims.length > 0 && (
            <ul aria-label="Dimension scores" className="space-y-1.5">
              {dims.map(([name, value]) => (
                <li key={name} className="grid grid-cols-[8rem_1fr_2.5rem] items-center gap-2">
                  <span className="capitalize">{name}</span>
                  <span aria-hidden="true" className="h-2 overflow-hidden rounded-full bg-muted">
                    <span className="block h-full rounded-full bg-primary" style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
                  </span>
                  <span className="text-right tabular-nums">{value}</span>
                </li>
              ))}
            </ul>
          )}
          {job.score_reason && <p dir="auto">{job.score_reason}</p>}
          <div className="grid gap-3 sm:grid-cols-2">
            <BulletList title="Strengths" items={textList(job.strengths)} />
            <BulletList title="Gaps" items={textList(job.gaps)} />
          </div>
          {gates.length > 0 && (
            <div>
              <h4 className="mb-1 font-medium">Gates</h4>
              <ul className="space-y-1">
                {gates.map((gate) => (
                  <li
                    key={gate.name}
                    className={cn('flex flex-wrap items-baseline gap-2 rounded-md px-2 py-1', gate.failed && 'bg-red-50 ring-1 ring-red-200 dark:bg-red-950 dark:ring-red-900')}
                  >
                    <span className="capitalize">{gate.name}</span>
                    <span className={cn('rounded px-1.5 text-xs font-medium', TONE_CLASSES[gate.tone])}>{gate.label}</span>
                    {gate.note && <span className="text-muted-foreground" dir="auto">{gate.note}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {yoe && <p><span className="text-muted-foreground">Experience asked:</span> {yoe}</p>}
        </div>
      )}
    </Section>
  )
}

function Safety({ job }: { job: JobDetail }) {
  if (!job.scam_risk || job.scam_risk === 'low') return null
  const flags = flagList(job.scam_flags)
  return (
    <Section title="Safety">
      <div className="space-y-2 rounded-lg bg-amber-50 p-3 ring-1 ring-amber-200 dark:bg-amber-950 dark:ring-amber-900">
        <p>
          <span className="font-medium">{riskLabel(job.scam_risk)}</span>
          {job.scam_score !== null && <span className="text-muted-foreground"> · score {job.scam_score}</span>}
        </p>
        {flags.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-5">
            {flags.map((flag, i) => (
              <li key={i}><span className="font-medium">{flag.label}</span>{flag.detail && <>: <span dir="auto">{flag.detail}</span></>}</li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  )
}

function Description({ text }: { text: string | null }) {
  const [expanded, setExpanded] = useState(false)
  const id = useId()
  if (!text?.trim()) {
    return <Section title="Description"><p className="text-muted-foreground">No description was collected.</p></Section>
  }
  const preview = descriptionPreview(text)
  return (
    <Section title="Description">
      <pre id={id} dir="auto" className="font-sans text-sm leading-relaxed break-words whitespace-pre-wrap">
        {preview !== null && !expanded ? preview : text}
      </pre>
      {preview !== null && (
        <Button variant="link" size="sm" className="h-auto px-0" aria-expanded={expanded} aria-controls={id} onClick={() => setExpanded(!expanded)}>
          {expanded ? 'Show less' : 'Show all'}
        </Button>
      )}
    </Section>
  )
}

function FoundOn({ job }: { job: JobDetail }) {
  if (!job.sources_detail.length) return null
  const now = new Date()
  // a board's clock can be ahead of ours; a listing was never seen in the future
  const seen = (when: string) => (new Date(when) > now ? now : when)
  return (
    <Section title="Found on">
      <ul className="divide-y rounded-lg ring-1 ring-foreground/10">
        {job.sources_detail.map((listing, i) => {
          const link = safeUrl(listing.url) ?? safeUrl(listing.apply_url)
          return (
            <li key={i} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
              <span className="font-medium">{sourceLabel(listing.source)}</span>
              {link && (
                <a href={link} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline">
                  {domainOf(link)}<ExternalLink className="size-3.5" aria-hidden="true" />
                </a>
              )}
              <span className="ml-auto text-muted-foreground">
                first seen <time dateTime={listing.first_seen} title={formatDateTime(listing.first_seen)}>{formatDate(listing.first_seen)}</time>
                {listing.last_seen && (
                  <>, last seen <time dateTime={listing.last_seen} title={formatDateTime(listing.last_seen)}>{formatRelative(seen(listing.last_seen), now)}</time></>
                )}
              </span>
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

function Application({ job }: { job: JobDetail }) {
  if (!job.application_status) return null
  return (
    <Section title="Application">
      <dl className="space-y-1.5">
        <Field label="Status"><StatusBadge status={job.application_status} /></Field>
        <Field label="Applied">{job.applied_at ? formatDateTime(job.applied_at) : 'Not yet'}</Field>
        {job.reviewed_at && <Field label="Reviewed">{formatDateTime(job.reviewed_at)}</Field>}
        {job.review_note && <Field label="Review note"><span className="whitespace-pre-wrap" dir="auto">{job.review_note}</span></Field>}
        {job.notes && <Field label="Notes"><span className="whitespace-pre-wrap" dir="auto">{job.notes}</span></Field>}
        {job.attention_reason && (
          <Field label="Needs attention"><span className="whitespace-pre-wrap text-amber-900 dark:text-amber-200" dir="auto">{job.attention_reason}</span></Field>
        )}
        {job.variant && <Field label="Resume">{job.variant === 'base' ? 'Base resume' : 'Tailored'}</Field>}
        {job.cost_usd !== null && (
          <Field label="Tailoring cost">{formatCost(job.cost_usd)}{job.model && <span className="text-muted-foreground"> · {job.model}</span>}</Field>
        )}
      </dl>
    </Section>
  )
}

function JobBody({ job }: { job: JobDetail }) {
  const summary = useSummary()
  const salary = formatSalary(job.salary_min, job.salary_max, job.salary_currency)
  return (
    <>
      <SheetHeader className="gap-2 border-b">
        <SheetTitle className="text-lg leading-snug" dir="auto">{job.title}</SheetTitle>
        <SheetDescription className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {[job.company, job.location].filter(Boolean).map((part, i) => (
            <span key={i} className="contents">
              {i > 0 && <span aria-hidden="true">·</span>}
              <span dir="auto">{part}</span>
            </span>
          ))}
          {job.is_remote && <Badge variant="secondary">Remote</Badge>}
        </SheetDescription>
        <div className="flex flex-wrap items-center gap-2">
          <StatusSelect row={job} />
          {salary !== DASH && <span className="tabular-nums">{salary}</span>}
          {job.closing_soon && (
            <Badge className={TONE_CLASSES.danger}>Closing soon{job.deadline ? `: ${formatDate(job.deadline)}` : ''}</Badge>
          )}
        </div>
      </SheetHeader>
      <div className="flex-1 space-y-6 overflow-y-auto p-4">
        <Links job={job} />
        <WhyThisScore job={job} threshold={summary.data?.threshold ?? null} />
        <Safety job={job} />
        <Description text={job.description} />
        <FoundOn job={job} />
        <Application job={job} />
      </div>
    </>
  )
}

function JobPanel({ id }: { id: number }) {
  const job = useJob(id)
  if (job.isPending) {
    return (
      <div role="status" aria-label="Loading the job" className="space-y-4 p-4">
        <SheetTitle className="sr-only">Loading the job</SheetTitle>
        <SheetDescription className="sr-only">The details appear here in a moment.</SheetDescription>
        <Skeleton className="h-6 w-3/4" />
        <Skeleton className="h-4 w-1/2" />
        <Skeleton className="h-24" />
        <Skeleton className="h-48" />
      </div>
    )
  }
  if (job.isError) {
    const gone = job.error instanceof ApiError && job.error.status === 404
    return (
      <div className="space-y-3 p-4">
        <SheetTitle>{gone ? 'Job not found' : 'The job could not be loaded'}</SheetTitle>
        <SheetDescription role="alert">
          {gone ? 'This job is not in the database any more.' : job.error instanceof ApiError ? job.error.detail : 'Something went wrong.'}
        </SheetDescription>
        {!gone && <Button size="sm" variant="outline" onClick={() => job.refetch()}>Retry</Button>}
      </div>
    )
  }
  return <JobBody job={job.data} />
}

/** The details of the job in `?job=`, beside the table. Escape, the close button or a click outside clear it. */
export default function JobDrawer() {
  const { view, jobId, setView } = useJobsParams()
  const jobs = useJobs(view)
  // the last job stays rendered while the panel slides out
  const [shown, setShown] = useState(jobId)
  if (jobId !== null && jobId !== shown) setShown(jobId)
  const { prev, next, position } = neighbours(jobs.data?.items.map((r) => r.id) ?? [], jobId)

  function focusRow(event: Event) {
    const title = document.querySelector<HTMLElement>(`[data-job-title="${shown}"]`)
    if (title) {
      event.preventDefault()
      title.focus()
    }
  }

  return (
    <Sheet open={jobId !== null} onOpenChange={(open) => { if (!open) setView(view, null) }}>
      <SheetContent className="gap-0 data-[side=right]:w-full data-[side=right]:sm:max-w-2xl" onCloseAutoFocus={focusRow}>
        <nav aria-label="Jobs on this page" className="flex h-12 shrink-0 items-center gap-1 border-b px-3 pr-12">
          <Button variant="ghost" size="icon-sm" aria-label="Previous job" disabled={prev === null} onClick={() => setView(view, prev)}>
            <ChevronLeft aria-hidden="true" />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="Next job" disabled={next === null} onClick={() => setView(view, next)}>
            <ChevronRight aria-hidden="true" />
          </Button>
          {position !== null && jobs.data && (
            <span className="text-xs text-muted-foreground tabular-nums">{position} of {jobs.data.items.length} on this page</span>
          )}
        </nav>
        {shown !== null && <JobPanel key={shown} id={shown} />}
      </SheetContent>
    </Sheet>
  )
}
