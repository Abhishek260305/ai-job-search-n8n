import { useState } from 'react'
import { useSetStatus } from '@/api/hooks'
import type { Board, BoardCard, BoardColumnKey } from '@/api/types'
import BoardCardView from '@/components/board/BoardCardView'
import { COLUMN_META, dropTarget } from '@/lib/board'
import { formatCount } from '@/lib/format'
import { statusLabel } from '@/lib/status'
import { useNow } from '@/lib/useNow'
import { cn } from '@/lib/utils'

type Props = { board: Board; threshold: number | null; onOpen: (id: number) => void }

type Dragging = { id: number; status: BoardCard['pipeline_status'] }

/**
 * The columns. A card moves by dragging it onto a column, or with the status menu on the card (the only way on a phone
 * and with a keyboard). Both send the same request as the jobs table: the status the card showed goes with it, so a
 * change made elsewhere is a "reload" and not an overwrite. Nothing moves on screen until the server has answered.
 */
export default function KanbanBoard({ board, threshold, onOpen }: Props) {
  const now = useNow()
  const setStatus = useSetStatus()
  const [dragging, setDragging] = useState<Dragging | null>(null)
  const [over, setOver] = useState<BoardColumnKey | null>(null)
  // every card being saved, so a second drag does not make the first one movable again while its request is in flight
  const [saving, setSaving] = useState<ReadonlySet<number>>(new Set())
  const track = (id: number, on: boolean) => setSaving((current) => {
    const next = new Set(current)
    if (on) next.add(id); else next.delete(id)
    return next
  })

  // a reload can remove the card being dragged, and its dragend never fires: forget the drag when the board changes
  const [seen, setSeen] = useState(board.generated_at)
  if (seen !== board.generated_at) {
    setSeen(board.generated_at)
    setDragging(null)
    setOver(null)
  }

  function drop(column: BoardColumnKey) {
    const moved = dragging
    setDragging(null)
    setOver(null)
    if (!moved) return
    const status = dropTarget(moved.status, column)
    if (status === null) return
    if (saving.has(moved.id)) return
    track(moved.id, true)
    // the hook shows the toast for a refusal; here only the card is released again
    setStatus.mutateAsync({ id: moved.id, status, expected: moved.status }).catch(() => undefined).finally(() => track(moved.id, false))
  }

  return (
    <div className="relative -mx-4 overflow-x-auto px-4 pb-2 md:mx-0 md:px-0">
      <ul aria-label="Board columns" className="grid min-w-max grid-flow-col auto-cols-[16.5rem] gap-3 2xl:min-w-0 2xl:grid-flow-row 2xl:auto-cols-auto 2xl:grid-cols-6">
        {board.columns.map((column) => {
          const meta = COLUMN_META[column.key]
          const target = dragging ? dropTarget(dragging.status, column.key) : null
          return (
            <li key={column.key} className="min-w-0">
              <section
                aria-labelledby={`col-${column.key}`}
                onDragOver={(e) => { if (target !== null) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setOver(column.key) } }}
                onDragLeave={() => setOver((o) => (o === column.key ? null : o))}
                onDrop={(e) => { e.preventDefault(); drop(column.key) }}
                className={cn(
                  'flex h-full flex-col gap-2 rounded-xl border bg-muted/40 p-2',
                  dragging && target !== null && 'border-dashed border-primary/60',
                  over === column.key && 'bg-primary/10',
                )}
              >
                <header className="flex items-baseline justify-between gap-2 px-1">
                  <h3 id={`col-${column.key}`} className="font-heading text-sm font-semibold">{meta.label}</h3>
                  <span className="text-xs text-muted-foreground tabular-nums">{formatCount(column.total)}<span className="sr-only"> jobs</span></span>
                </header>
                {dragging && target !== null && (
                  <p className="px-1 text-xs text-primary" aria-hidden="true">Drop to mark {statusLabel(target)}</p>
                )}
                {column.items.length ? (
                  <ul aria-label={`${meta.label} jobs`} className="space-y-2">
                    {column.items.map((card) => (
                      <BoardCardView
                        key={card.id}
                        card={card}
                        column={column.key}
                        now={now}
                        threshold={threshold}
                        saving={saving.has(card.id)}
                        onOpen={() => onOpen(card.id)}
                        onDragStart={(e) => { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(card.id)); setDragging({ id: card.id, status: card.pipeline_status }) }}
                        onDragEnd={() => { setDragging(null); setOver(null) }}
                      />
                    ))}
                  </ul>
                ) : (
                  <p className="px-1 py-3 text-xs text-muted-foreground">{meta.hint}. Nothing here yet.</p>
                )}
                {column.total > column.items.length && (
                  <p className="px-1 text-xs text-muted-foreground">
                    Showing {column.items.length} of {formatCount(column.total)}. The Jobs page has the rest.
                  </p>
                )}
              </section>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
