import { type FormEvent, useState } from 'react'
import { useChangeTailoring } from '@/api/hooks'
import type { SettingsPage } from '@/api/types'
import NativeSelect from '@/components/NativeSelect'
import { Field, FormError, INPUT_CLASS } from '@/components/settings/parts'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { isEmpty, tailoringChanges, tailoringForm, type TailoringForm } from '@/lib/settingsForm'

/**
 * How tailoring behaves. The approval mode is shown but not editable here: it decides whether applications wait for
 * you, and "auto" would approve them for you, so that choice stays a command (or the Telegram /set).
 */
export default function TailoringCard({ saved, threshold }: { saved: SettingsPage['tailoring']; threshold: number | null }) {
  const [form, setForm] = useState<TailoringForm>(() => tailoringForm(saved))
  const [touched, setTouched] = useState(false)
  const change = useChangeTailoring()
  const built = tailoringChanges(saved, form)
  const dirty = !isEmpty(built.changes)
  const set = (patch: Partial<TailoringForm>) => {
    setTouched(true)
    setForm((f) => ({ ...f, ...patch }))
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (dirty && !built.error) change.mutate(built.changes)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Tailoring</CardTitle>
        <p className="text-sm text-muted-foreground">
          Approval is <strong>{saved.approval}</strong>: it decides whether applications wait for your decision, so it is changed with{' '}
          <code>scripts/jobsearch tailor-settings set --approval …</code>, not here.
        </p>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} aria-label="Tailoring settings" className="grid gap-4 sm:grid-cols-2">
          <Field id="tailor-mode" label="Mode" hint="Auto: shortlisted jobs are tailored by the schedule. Manual: only when you ask.">
            <NativeSelect id="tailor-mode" className="h-10" aria-describedby="tailor-mode-hint" value={form.mode} onChange={(e) => set({ mode: e.target.value as TailoringForm['mode'] })}>
              <option value="auto">Auto</option>
              <option value="manual">Manual</option>
            </NativeSelect>
          </Field>
          <Field id="tailor-daily" label="Most applications per day" hint="Empty: no limit (the spending caps still apply)">
            <input id="tailor-daily" aria-describedby="tailor-daily-hint" className={INPUT_CLASS} inputMode="numeric" value={form.dailyLimit} onChange={(e) => set({ dailyLimit: e.target.value })} />
          </Field>
          <Field id="tailor-min" label="Lowest score to tailor" hint={threshold === null ? 'Empty: your search threshold' : `Empty: your search threshold (${threshold})`}>
            <input id="tailor-min" aria-describedby="tailor-min-hint" className={INPUT_CLASS} inputMode="numeric" value={form.minScore} onChange={(e) => set({ minScore: e.target.value })} />
          </Field>
          <Field id="tailor-max" label="Score above which your base resume is used as written" hint="Empty: never. At or above it only the cover letter is written.">
            <input id="tailor-max" aria-describedby="tailor-max-hint" className={INPUT_CLASS} inputMode="numeric" value={form.maxScore} onChange={(e) => set({ maxScore: e.target.value })} />
          </Field>
          <Field id="tailor-pages" label="Resume pages">
            <NativeSelect id="tailor-pages" className="h-10" value={form.maxPages} onChange={(e) => set({ maxPages: e.target.value })}>
              <option value="1">1 page</option>
              <option value="2">2 pages</option>
            </NativeSelect>
          </Field>
          <Field id="tailor-paper" label="Paper">
            <NativeSelect id="tailor-paper" className="h-10" value={form.paper} onChange={(e) => set({ paper: e.target.value as TailoringForm['paper'] })}>
              <option value="a4">A4</option>
              <option value="letter">Letter</option>
            </NativeSelect>
          </Field>
          <div className="flex items-center gap-2 sm:col-span-2">
            <Checkbox id="tailor-cover" checked={form.coverLetter} onCheckedChange={(v) => set({ coverLetter: v === true })} />
            <Label htmlFor="tailor-cover">Write a cover letter</Label>
          </div>
          <div className="grid gap-2 sm:col-span-2">
            <FormError error={change.error} local={touched ? built.error : null} />
            <div><Button type="submit" className="h-10" disabled={!dirty || built.error !== null || change.isPending}>Save</Button></div>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
