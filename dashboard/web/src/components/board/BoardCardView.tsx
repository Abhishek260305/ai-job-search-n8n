import { Loader2 } from 'lucide-react'
import type { DragEvent } from 'react'
import type { BoardCard, BoardColumnKey } from '@/api/types'
import StatusSelect from '@/components/StatusSelect'
import { Badge } from '@/components/ui/badge'
import { inColumnFor } from '@/lib/board'
import { formatDate } from '@/lib/format'
import { scoreTone } from '@/lib/jobsTable'
import { TONE_CLASSES } from '@/lib/status'
import { cn } from '@/lib/utils'

type Props = {
  card: BoardCard
  column: BoardColumnKey
  now: Date
  threshold: number | null
  saving: boolean
  /** Whether the browser can drag it (a card being saved cannot be moved again). */
  onOpen: () => void
  onDragStart: (event: DragEvent<HTMLElement>) => void
  onDragEnd: () => void
}

/** How long ago, in words, as a <time> (the exact moment is in the markup). */
function Since({ at, now, column }: { at: string; now: Date; column: BoardColumnKey }) {
  const text = inColumnFor(at, now)
  const words = column === 'not_applied'
    ? (text === 'today' ? 'Scored today' : `Scored ${text} ago`)
    : (text === 'today' ? 'Moved here today' : `${text} in this stage`)
  return <time dateTime={at}>{words}</time>
}

// Everything on a card is posting text shown as React text, which React escapes.
export default function BoardCardView({ card, column, now, threshold, saving, onOpen, onDragStart, onDragEnd }: Props) {
  const place = [card.company, card.location].filter(Boolean).join(' · ')
  return (
    <li
      draggable={!saving}
      onDragStart={(e) => { if (e.target === e.currentTarget) onDragStart(e) }}      // not a drag that began in the note box
      onDragEnd={onDragEnd}
      aria-busy={saving}
      className={cn(
        'cursor-grab rounded-lg border bg-card p-3 text-sm shadow-xs active:cursor-grabbing',
        saving && 'opacity-60',
      )}
    >
      <div className="flex items-start gap-2">
        <button
          type="button"
          data-job-title={card.id}
          onClick={onOpen}
          className="min-w-0 flex-1 text-left font-medium leading-snug outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 rounded-sm"
          dir="auto"
        >
          {card.title}
        </button>
        {card.score !== null && (
          <span
            title="Score"
            className={cn('inline-flex h-6 min-w-8 shrink-0 items-center justify-center rounded-md px-1.5 text-xs font-semibold tabular-nums', TONE_CLASSES[scoreTone(card.score, threshold)])}
          >
            {card.score}
          </span>
        )}
      </div>
      {place && <p className="mt-0.5 truncate text-muted-foreground" dir="auto">{place}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {card.closing_soon && <Badge className={TONE_CLASSES.danger}>Closing soon{card.deadline ? `: ${formatDate(card.deadline)}` : ''}</Badge>}
        {card.follow_up && <Badge className={TONE_CLASSES.warning}>Follow up</Badge>}
        {card.needs_attention && <Badge className={TONE_CLASSES.warning}>Needs attention</Badge>}
        {card.has_resume && <Badge variant="secondary">Resume</Badge>}
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
        <span className="text-xs text-muted-foreground"><Since at={card.status_since} now={now} column={column} /></span>
        {saving ? <Loader2 className="size-4 animate-spin text-muted-foreground" role="img" aria-label="Saving" /> : <StatusSelect row={{ id: card.id, pipeline_status: card.pipeline_status, job_status: 'shortlisted' }} />}
      </div>
    </li>
  )
}
