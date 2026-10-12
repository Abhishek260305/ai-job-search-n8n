import { ApiError } from '@/api/client'
import { useSummary } from '@/api/hooks'
import type { Summary } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { formatCount } from '@/lib/format'
import { DEFAULT_VIEW, type JobsView, useJobsParams, viewSearch } from '@/lib/jobsParams'

type CardKey = keyof Summary['cards']

type CardDef = {
  key: CardKey
  label: string
  hint?: (threshold: number | null) => string
  /** The table view whose total is this card's number (docs/dashboard.md, "Summary cards"). */
  view: Partial<JobsView>
}

const CARDS: readonly CardDef[] = [
  { key: 'found', label: 'Jobs found', hint: () => 'every board, after dedupe', view: { tab: 'all' } },
  {
    key: 'shortlisted',
    label: 'Shortlisted',
    hint: (t) => (t === null ? 'not acted on yet' : `score ≥ ${t}, not acted on yet`),
    view: { status: ['shortlisted'] },
  },
  {
    // the API's "posted within" filter reads the posting date; this card counts first sighting, so newest first
    key: 'new_this_week',
    label: 'New this week',
    hint: () => 'shortlisted, first seen in 7 days',
    view: { status: ['shortlisted'], sort: 'first_seen' },
  },
  { key: 'applied', label: 'Applied', hint: () => 'everything you submitted', view: { tab: 'applied' } },
  { key: 'in_progress', label: 'In progress', view: { tab: 'applied', status: ['in_progress'] } },
  { key: 'interview', label: 'Interview', view: { tab: 'applied', status: ['interview'] } },
  { key: 'offer', label: 'Offer', view: { tab: 'applied', status: ['offer'] } },
  { key: 'rejected', label: 'Rejected', view: { tab: 'applied', status: ['rejected'] } },
]

/** A card opens a fresh view (only the page size is kept), so the table's total matches the number clicked. */
function cardView(card: CardDef, current: JobsView): JobsView {
  return { ...DEFAULT_VIEW, page_size: current.page_size, ...card.view }
}

const GRID = 'grid grid-cols-2 gap-3 md:grid-cols-4 2xl:grid-cols-8'

export default function SummaryCards() {
  const summary = useSummary()
  const { view, setView } = useJobsParams()

  if (summary.isPending) {
    return (
      <div className={GRID} role="status" aria-label="Loading the summary">
        {CARDS.map((c) => <Skeleton key={c.key} className="h-24 rounded-xl" />)}
      </div>
    )
  }

  if (summary.isError) {
    return (
      <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
        <span className="font-medium">The summary could not be loaded.</span>
        <span className="text-muted-foreground">
          {summary.error instanceof ApiError ? summary.error.detail : 'Something went wrong.'}
        </span>
        <Button size="sm" variant="outline" className="ml-auto" onClick={() => summary.refetch()}>Retry</Button>
      </div>
    )
  }

  const { cards, threshold } = summary.data
  const here = viewSearch({ ...view, page: DEFAULT_VIEW.page }).toString()

  return (
    <section aria-label="Summary" className={GRID}>
      {CARDS.map((card) => {
        const target = cardView(card, view)
        const active = viewSearch(target).toString() === here
        return (
          <button
            key={card.key}
            type="button"
            aria-pressed={active}
            onClick={() => setView(target)}
            className="flex flex-col items-start gap-1 rounded-xl bg-card p-4 text-left ring-1 ring-foreground/10 transition-colors outline-none hover:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50 aria-pressed:bg-muted aria-pressed:ring-2 aria-pressed:ring-primary"
          >
            <span className="text-sm text-muted-foreground">{card.label}</span>
            <span className="font-heading text-2xl font-semibold tabular-nums">{formatCount(cards[card.key])}</span>
            {card.hint && <span className="text-xs text-muted-foreground">{card.hint(threshold)}</span>}
          </button>
        )
      })}
    </section>
  )
}
