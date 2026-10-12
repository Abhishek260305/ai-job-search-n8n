import { type FormEvent, useState } from 'react'
import { useLowerBudget } from '@/api/hooks'
import type { SettingsPage } from '@/api/types'
import { Field, FormError, INPUT_CLASS } from '@/components/settings/parts'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { budgetChanges, CAP_KEYS, CAP_LABELS, capsForm, isEmpty, type CapKey } from '@/lib/settingsForm'
import { formatMoney } from '@/lib/ops'

/** The four spending caps. They can only be lowered here: raising one is what lets more money flow, so it stays a command. */
export default function BudgetCard({ saved }: { saved: SettingsPage['budget'] }) {
  const [typed, setTyped] = useState<Record<CapKey, string>>(() => capsForm(saved))
  const [touched, setTouched] = useState(false)
  const change = useLowerBudget()
  const built = budgetChanges(saved, typed)
  const dirty = !isEmpty(built.changes)

  function submit(event: FormEvent) {
    event.preventDefault()
    if (dirty && !built.error) change.mutate(built.changes)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Spending caps</CardTitle>
        <p className="text-sm text-muted-foreground">
          A cap stops spending when it is reached. You can <strong>lower</strong> a cap here; to <strong>raise</strong> one, run{' '}
          <code>scripts/jobsearch budget set</code>.
        </p>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} aria-label="Spending caps" className="grid gap-4 sm:grid-cols-2">
          {CAP_KEYS.map((key) => (
            <Field key={key} id={`cap-${key}`} label={`${CAP_LABELS[key]} (US dollars)`} hint={`Now ${formatMoney(saved[key])}. Lower only.`}>
              <input id={`cap-${key}`} aria-describedby={`cap-${key}-hint`} className={INPUT_CLASS} inputMode="decimal" value={typed[key]}
                onChange={(e) => { setTouched(true); setTyped((t) => ({ ...t, [key]: e.target.value })) }} />
            </Field>
          ))}
          <div className="grid gap-2 sm:col-span-2">
            <FormError error={change.error} local={touched ? built.error : null} />
            <div><Button type="submit" className="h-10" disabled={!dirty || built.error !== null || change.isPending}>Lower the caps</Button></div>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
