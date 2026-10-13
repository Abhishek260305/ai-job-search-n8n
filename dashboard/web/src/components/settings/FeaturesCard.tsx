import { useState } from 'react'
import { useChangeFeatures } from '@/api/hooks'
import type { SettingsPage } from '@/api/types'
import { FormError } from '@/components/settings/parts'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'

type Key = 'tailoring' | 'telegram' | 'digest' | 'gmail'

const ROWS: { key: Key; label: string; help: string }[] = [
  { key: 'tailoring', label: 'Tailoring', help: 'Write a resume and cover letter for each shortlisted job. It costs about 5 cents per application, inside your budget caps. Off: the pipeline stops at scoring.' },
  { key: 'telegram', label: 'Telegram', help: 'Review applications and run commands from your phone. It also needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.' },
  { key: 'digest', label: 'Digest', help: 'The message listing new matches (it needs Telegram). Its time is under Schedules.' },
  { key: 'gmail', label: 'Email replies', help: 'Read replies to your applications (read-only) and suggest status changes you accept or dismiss on the Email page. Nothing changes without you. It needs the Google login: run scripts/jobsearch gmail login once. Off by default.' },
]

/**
 * The optional parts. A switch saves at once, except Telegram off while the approval mode is "review": with
 * nobody to ask, "review" behaves as "auto" and applications are stored ready with no review step, so that asks first.
 */
export default function FeaturesCard({ features }: { features: SettingsPage['features'] }) {
  const change = useChangeFeatures()
  const [confirmOff, setConfirmOff] = useState(false)
  const approvalWeakened = features.approval_effective !== features.approval

  function toggle(key: Key, on: boolean) {
    if (key === 'telegram' && !on && features.approval === 'review' && features.effective.telegram.on) setConfirmOff(true)
    else change.mutate({ [key]: on })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Optional parts</CardTitle>
        <p className="text-sm text-muted-foreground">Collecting, filtering and scoring always run; everything after scoring is optional.</p>
      </CardHeader>
      <CardContent className="grid gap-4">
        <ul className="grid gap-4" aria-label="Optional parts">
          {ROWS.map(({ key, label, help }) => {
            const flag = features.flags[key]
            const status = features.effective[key]
            return (
              <li key={key} className="flex items-start gap-3">
                <Checkbox id={`feature-${key}`} className="mt-0.5" checked={flag} disabled={change.isPending}
                  aria-describedby={`feature-${key}-help`} onCheckedChange={(v) => toggle(key, v === true)} />
                <div className="grid gap-0.5 text-sm">
                  <Label htmlFor={`feature-${key}`} className="font-medium">{label}</Label>
                  <p id={`feature-${key}-help`} className="text-muted-foreground">{help}</p>
                  <p className={status.on ? 'text-emerald-700 dark:text-emerald-400' : 'text-amber-700 dark:text-amber-400'}>
                    {status.on ? 'In effect' : `Not in effect: ${status.reason}`}
                  </p>
                  {key === 'telegram' && approvalWeakened && (
                    <p role="note" className="rounded-md bg-amber-100 px-2 py-1 text-amber-900 dark:bg-amber-950 dark:text-amber-200">
                      Approval is <strong>{features.approval}</strong> but behaves as <strong>{features.approval_effective}</strong> while Telegram is
                      not in effect: applications are stored as ready with no review step. Choose <code>cli</code> to keep a human decision without Telegram.
                    </p>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
        <FormError error={change.error} />
      </CardContent>
      <Dialog open={confirmOff} onOpenChange={(next) => { if (!change.isPending) setConfirmOff(next) }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Switch Telegram off?</DialogTitle>
            <DialogDescription>
              Your approval mode is <strong>review</strong>: each application waits for your decision on Telegram. With Telegram off there is nobody
              to ask, so applications are stored as ready with no review step, and the digest stops too. To keep a human decision without Telegram,
              set the approval mode to <code>cli</code> first (<code>scripts/jobsearch tailor-settings set --approval cli</code>).
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild><Button type="button" variant="outline" disabled={change.isPending}>Keep Telegram on</Button></DialogClose>
            <Button type="button" disabled={change.isPending} onClick={() => change.mutate({ telegram: false }, { onSuccess: () => setConfirmOff(false) })}>Switch it off</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
