import { Play } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useChangeSchedule, useRunSchedule } from '@/api/hooks'
import type { ScheduleSetting, SettingsPage } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import NativeSelect from '@/components/NativeSelect'
import { INPUT_CLASS, Field, FormError } from '@/components/settings/parts'
import { formatDateTime, formatRelative } from '@/lib/format'
import { stageLabel } from '@/lib/ops'
import { everyHint, isEmpty, scheduleChanges, scheduleForm, type ScheduleForm, type Unit } from '@/lib/settingsForm'

const WORK: Record<string, string> = { collect: 'Searches per run', score: 'Jobs per run', tailor: 'Applications per run', digest: 'Matches per message' }

function RunNow({ name, paused, featureOff }: { name: ScheduleSetting['name']; paused: boolean; featureOff: boolean }) {
  const run = useRunSchedule(name)
  const [open, setOpen] = useState(false)
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!run.isPending) setOpen(next) }}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" className="h-10 gap-1.5" disabled={paused || featureOff}
          title={paused ? 'Everything is paused: resume on the Health page first' : featureOff ? 'Its feature is switched off, so the scheduler skips it' : undefined}>
          <Play aria-hidden="true" /> Run now
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Run {stageLabel(name).toLowerCase()} now?</DialogTitle>
          <DialogDescription>
            It starts at the next scheduler tick, within 5 minutes, and then continues on its schedule. Collecting, scoring and
            tailoring spend money as usual, inside your budget caps; the digest sends its Telegram message.
          </DialogDescription>
        </DialogHeader>
        <FormError error={run.error} />
        <DialogFooter>
          <DialogClose asChild><Button type="button" variant="outline" disabled={run.isPending}>Cancel</Button></DialogClose>
          <Button type="button" disabled={run.isPending} onClick={() => run.mutate(undefined, { onSuccess: () => setOpen(false) })}>Run now</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** One stage's settings. The saved values come in as props; the parent re-keys this when they change. */
export default function ScheduleCard({ saved, limits, paused, featureOff }: { saved: ScheduleSetting; limits: SettingsPage['limits']; paused: boolean; featureOff: boolean }) {
  const [form, setForm] = useState<ScheduleForm>(() => scheduleForm(saved))
  const [touched, setTouched] = useState(false)
  const change = useChangeSchedule(saved.name)
  const built = scheduleChanges(saved, form)
  const dirty = !isEmpty(built.changes)
  const id = `schedule-${saved.name}`
  const set = (patch: Partial<ScheduleForm>) => {
    setTouched(true)
    setForm((f) => ({ ...f, ...patch }))
  }
  const digest = saved.name === 'digest'

  function submit(event: FormEvent) {
    event.preventDefault()
    if (dirty && !built.error) change.mutate(built.changes)
  }

  return (
    <form onSubmit={submit} aria-label={`${stageLabel(saved.name)} schedule`} className="grid content-start gap-3 rounded-xl border p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="font-medium">{stageLabel(saved.name)}</h3>
        {saved.run_now && <Badge variant="secondary">Run requested</Badge>}
        {saved.enabled && featureOff && <Badge variant="secondary" title="The scheduler skips this stage while its feature is switched off">Feature off</Badge>}
        <div className="ml-auto flex items-center gap-2">
          <Checkbox id={`${id}-on`} checked={form.enabled} onCheckedChange={(v) => set({ enabled: v === true })} />
          <Label htmlFor={`${id}-on`}>On</Label>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        Last run {saved.last_run_at ? <time dateTime={saved.last_run_at} title={formatDateTime(saved.last_run_at)}>{formatRelative(saved.last_run_at)}</time> : 'never'}
      </p>

      {digest && (
        <fieldset className="grid gap-1.5">
          <legend className="mb-1 text-sm font-medium">When</legend>
          <label className="flex items-center gap-2"><input type="radio" name={`${id}-when`} checked={form.daily} onChange={() => set({ daily: true })} /> Once a day at</label>
          <label className="flex items-center gap-2"><input type="radio" name={`${id}-when`} checked={!form.daily} onChange={() => set({ daily: false })} /> At an interval</label>
        </fieldset>
      )}

      {digest && form.daily ? (
        <Field id={`${id}-time`} label="Time of day (HH:MM, 24-hour)">
          <input id={`${id}-time`} className={INPUT_CLASS} inputMode="numeric" value={form.atTime} onChange={(e) => set({ atTime: e.target.value })} />
        </Field>
      ) : (
        <Field id={`${id}-every`} label="Runs every" hint={`${everyHint(limits.min_every[saved.name], limits.max_every)}${saved.name === 'collect' ? ', kept gentle on the job boards' : ''}`}>
          <div className="flex gap-2">
            <input id={`${id}-every`} aria-describedby={`${id}-every-hint`} className={INPUT_CLASS} inputMode="decimal" value={form.every.value}
              onChange={(e) => set({ every: { ...form.every, value: e.target.value } })} />
            <NativeSelect aria-label="Unit" className="h-10" value={form.every.unit} onChange={(e) => set({ every: { ...form.every, unit: e.target.value as Unit } })}>
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
              <option value="days">days</option>
            </NativeSelect>
          </div>
        </Field>
      )}

      <Field id={`${id}-batch`} label={WORK[saved.name]} hint={`1 to ${limits.max_batch[saved.name]}`}>
        <input id={`${id}-batch`} aria-describedby={`${id}-batch-hint`} className={INPUT_CLASS} inputMode="numeric" value={form.batch} onChange={(e) => set({ batch: e.target.value })} />
      </Field>

      {saved.name === 'collect' && saved.board_batch !== null && (
        <Field id={`${id}-boards`} label="Company boards and feeds per run" hint={`1 to ${limits.max_board_batch}; a longer list is visited in turns`}>
          <input id={`${id}-boards`} aria-describedby={`${id}-boards-hint`} className={INPUT_CLASS} inputMode="numeric" value={form.boardBatch} onChange={(e) => set({ boardBatch: e.target.value })} />
        </Field>
      )}

      {saved.name === 'score' && saved.model && (
        <p className="text-xs text-muted-foreground">
          Model: <code>{saved.model}</code>. It decides what each scored job costs, so it is changed with <code>scripts/jobsearch schedule set score --model …</code>.
        </p>
      )}

      <FormError error={change.error} local={touched ? built.error : null} />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" className="h-10" disabled={!dirty || built.error !== null || change.isPending}>Save</Button>
        <RunNow name={saved.name} paused={paused} featureOff={featureOff} />
      </div>
    </form>
  )
}
