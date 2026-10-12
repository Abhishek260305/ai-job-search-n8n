import { Pause, Play } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useSetPause } from '@/api/hooks'
import type { PauseState } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { formatDateTime, formatRelative } from '@/lib/format'

const REASON_MAX = 200

/**
 * The emergency brake: the same switch as `scripts/jobsearch schedule pause`. Pausing only ever stops things, so one
 * confirmation is enough; resuming starts the schedules again, still inside every budget cap.
 */
export default function PauseCard({ state }: { state: PauseState }) {
  const setPause = useSetPause()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')

  function pause(event: FormEvent) {
    event.preventDefault()
    setPause.mutate({ pause: true, reason }, { onSuccess: () => setOpen(false) })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>{state.paused ? 'Paused' : 'Running'}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-3 text-sm">
        {state.paused ? (
          <>
            <p className="min-w-0 flex-1 text-muted-foreground">
              Nothing scheduled runs: no collecting, scoring, tailoring or digest
              {state.paused_at && (
                <>, since <time dateTime={state.paused_at} title={formatDateTime(state.paused_at)}>{formatRelative(state.paused_at)}</time></>
              )}
              {state.reason ? <>. Reason: {state.reason}</> : null}.
            </p>
            <Button onClick={() => setPause.mutate({ pause: false })} disabled={setPause.isPending} className="h-10 gap-1.5">
              <Play aria-hidden="true" /> Resume
            </Button>
          </>
        ) : (
          <>
            <p className="min-w-0 flex-1 text-muted-foreground">Collecting, scoring, tailoring and the digest run on their schedules.</p>
            <Dialog open={open} onOpenChange={(next) => { if (!setPause.isPending) { setOpen(next); if (next) setReason('') } }}>
              <DialogTrigger asChild>
                <Button variant="outline" className="h-10 gap-1.5"><Pause aria-hidden="true" /> Pause everything</Button>
              </DialogTrigger>
              <DialogContent className="sm:max-w-md">
                <form onSubmit={pause} className="grid gap-4">
                  <DialogHeader>
                    <DialogTitle>Pause everything?</DialogTitle>
                    <DialogDescription>
                      Collecting, scoring, tailoring and the digest stop, even "run now", until you resume. Nothing is lost,
                      and jobs already collected stay as they are. This is the same switch as <code>schedule pause</code>.
                    </DialogDescription>
                  </DialogHeader>
                  <div className="grid gap-2">
                    <Label htmlFor="pause-reason">Reason (optional)</Label>
                    <input
                      id="pause-reason"
                      value={reason}
                      maxLength={REASON_MAX}
                      onChange={(e) => setReason(e.target.value)}
                      className="h-10 rounded-lg border border-input bg-background px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                    />
                  </div>
                  <DialogFooter>
                    <DialogClose asChild><Button type="button" variant="outline" disabled={setPause.isPending}>Cancel</Button></DialogClose>
                    <Button type="submit" disabled={setPause.isPending}>Pause everything</Button>
                  </DialogFooter>
                </form>
              </DialogContent>
            </Dialog>
          </>
        )}
      </CardContent>
    </Card>
  )
}
