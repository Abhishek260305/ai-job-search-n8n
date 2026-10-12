import { ChevronDown, Loader2 } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useSetStatus } from '@/api/hooks'
import type { EditableStatus, JobRow } from '@/api/types'
import NativeSelect from '@/components/NativeSelect'
import StatusBadge from '@/components/StatusBadge'
import { Button } from '@/components/ui/button'
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Label } from '@/components/ui/label'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { EDITABLE_STATUSES, needsConfirm, statusLabel } from '@/lib/status'

const NOTE_MAX = 500

type Row = Pick<JobRow, 'id' | 'pipeline_status' | 'job_status'>

/** Why a move back asks first: what was submitted stays submitted (applied_at is kept). */
function backwardWarning(to: EditableStatus): string {
  return `This job was already applied. Move it back to ${statusLabel(to)}? The applied date is kept.`
}

/**
 * The status of one job, and the only way the dashboard changes it. Nothing changes on screen until the server has
 * confirmed and the data has been reloaded; the browser always sends the status it showed (`expected_status`).
 */
export default function StatusSelect({ row }: { row: Row }) {
  const current = row.pipeline_status
  const setStatus = useSetStatus()
  // a dialog for a move back (asks first) or for a change with a note
  const [dialog, setDialog] = useState<{ status: EditableStatus | ''; withNote: boolean } | null>(null)
  const [note, setNote] = useState('')

  if (row.job_status === 'scam_blocked') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="inline-flex rounded-md outline-none focus-visible:ring-3 focus-visible:ring-ring/50">
            <Button variant="ghost" size="sm" disabled aria-label={`Status: ${statusLabel(current)}. Status changes are disabled`} className="h-7 px-1">
              <StatusBadge status={current} />
            </Button>
          </span>
        </TooltipTrigger>
        <TooltipContent>Scam-blocked: status changes are disabled</TooltipContent>
      </Tooltip>
    )
  }

  function save(status: EditableStatus, text?: string) {
    setStatus.mutate({ id: row.id, status, expected: current, note: text }, { onSettled: () => setDialog(null) })
  }

  function choose(status: EditableStatus) {
    if (status === current) return
    if (needsConfirm(current, status)) {
      setNote('')
      setDialog({ status, withNote: false })
    } else {
      save(status)
    }
  }

  function openNote() {
    setNote('')
    // A job that is not at an editable status yet (shortlisted, drafted) starts with no status chosen: Save stays
    // off until the user picks one, so "Add a note" can never mark a job applied by itself.
    setDialog({ status: (EDITABLE_STATUSES as readonly string[]).includes(current) ? (current as EditableStatus) : '', withNote: true })
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (dialog && dialog.status) save(dialog.status, note)
  }

  const saving = setStatus.isPending
  const backward = dialog !== null && dialog.status !== '' && needsConfirm(current, dialog.status)

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1 px-1"
            disabled={saving}
            aria-busy={saving}
            aria-label={`Status: ${statusLabel(current)}. Change status`}
          >
            <StatusBadge status={current} />
            {saving ? <Loader2 className="animate-spin" aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-52">
          <DropdownMenuLabel>Mark as</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={current} onValueChange={(s) => choose(s as EditableStatus)}>
            {EDITABLE_STATUSES.map((s) => (
              <DropdownMenuRadioItem key={s} value={s}>{statusLabel(s)}</DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={openNote}>Add a note…</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={dialog !== null} onOpenChange={(open) => { if (!open && !saving) setDialog(null) }}>
        <DialogContent className="sm:max-w-md">
          <form onSubmit={submit} className="grid gap-4">
            <DialogHeader>
              <DialogTitle>{dialog?.withNote ? 'Change the status' : `Move back to ${dialog?.status ? statusLabel(dialog.status) : ''}?`}</DialogTitle>
              <DialogDescription>
                {backward && dialog?.status ? backwardWarning(dialog.status) : `Now: ${statusLabel(current)}.`}
              </DialogDescription>
            </DialogHeader>
            {dialog?.withNote && (
              <div className="grid gap-2">
                <Label htmlFor={`status-${row.id}`}>New status</Label>
                <NativeSelect
                  id={`status-${row.id}`}
                  value={dialog.status}
                  onChange={(e) => setDialog({ status: e.target.value as EditableStatus | '', withNote: true })}
                >
                  {dialog.status === '' && <option value="" disabled>Choose a status…</option>}
                  {EDITABLE_STATUSES.map((s) => <option key={s} value={s}>{statusLabel(s)}</option>)}
                </NativeSelect>
              </div>
            )}
            <div className="grid gap-2">
              <Label htmlFor={`note-${row.id}`}>Note (optional)</Label>
              <textarea
                id={`note-${row.id}`}
                value={note}
                maxLength={NOTE_MAX}
                rows={3}
                onChange={(e) => setNote(e.target.value)}
                aria-describedby={`note-count-${row.id}`}
                className="min-h-20 rounded-lg border border-input bg-background px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              />
              <span id={`note-count-${row.id}`} className="text-right text-xs text-muted-foreground tabular-nums">
                {note.length}/{NOTE_MAX}
              </span>
            </div>
            <DialogFooter>
              <DialogClose asChild>
                <Button type="button" variant="outline" disabled={saving}>Cancel</Button>
              </DialogClose>
              <Button type="submit" disabled={saving || !dialog?.status}>
                {saving && <Loader2 className="animate-spin" aria-hidden="true" />}
                {backward ? 'Move back' : 'Save'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
