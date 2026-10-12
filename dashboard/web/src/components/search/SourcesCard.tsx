import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import type { SearchProfile } from '@/api/types'
import { companyCount, sourceGroups, withSource, type Registry } from '@/lib/searchForm'
import { sourceLabel } from '@/lib/jobsTable'
import { TONE_CLASSES } from '@/lib/status'

/** Which places jobs come from: one switch each. A company board that is on needs companies (below). */
export default function SourcesCard({ draft, registry, set }: { draft: SearchProfile; registry: Registry; set: (patch: Partial<SearchProfile>) => void }) {
  // a paid source asks first when it is switched on: it spends real money (inside the caps), like `sources on apify` says
  const [confirmPaid, setConfirmPaid] = useState<string | null>(null)
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Sources</CardTitle>
        <p className="text-sm text-muted-foreground">Every collect run reads the sources that are on. A change applies from the next run.</p>
      </CardHeader>
      <CardContent className="grid gap-5">
        {sourceGroups(registry).map((group) => (
          <section key={group.key} aria-label={group.label} className="grid gap-2">
            <div>
              <h4 className="text-sm font-medium">{group.label}</h4>
              {group.note && <p className="text-xs text-muted-foreground">{group.note}</p>}
            </div>
            <ul className="grid gap-2 md:grid-cols-2">
              {group.sources.map((name) => {
                const on = draft.sources.includes(name)
                const n = (draft.boards as Record<string, unknown[] | undefined>)[name]?.length ?? 0
                const boardWithNone = on && registry.board_systems.includes(name) && n === 0
                return (
                  <li key={name} className="flex items-start gap-3 rounded-lg border p-2.5">
                    <Checkbox id={`source-${name}`} className="mt-0.5" checked={on} aria-describedby={`source-${name}-about`}
                      onCheckedChange={(v) => {
                        if (v === true && registry.paid.includes(name)) setConfirmPaid(name)
                        else set({ sources: withSource(draft.sources, name, v === true, registry) })
                      }} />
                    <div className="grid gap-0.5 text-sm">
                      <div className="flex flex-wrap items-center gap-2">
                        <Label htmlFor={`source-${name}`} className="font-medium">{sourceLabel(name)}</Label>
                        {registry.paid.includes(name) && <Badge className={TONE_CLASSES.warning}>Paid</Badge>}
                        {registry.board_systems.includes(name) && <Badge variant="outline">{n} {n === 1 ? 'company' : 'companies'}</Badge>}
                      </div>
                      <p id={`source-${name}-about`} className="text-xs text-muted-foreground">{registry.descriptions[name]}</p>
                      {boardWithNone && <p className="text-xs text-amber-700 dark:text-amber-400">On, but no company is listed under it yet: it will find nothing.</p>}
                    </div>
                  </li>
                )
              })}
            </ul>
          </section>
        ))}
        <p className="text-xs text-muted-foreground">{companyCount(draft.boards)} companies listed in total across the company boards.</p>
      </CardContent>
      <Dialog open={confirmPaid !== null} onOpenChange={(open) => { if (!open) setConfirmPaid(null) }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Switch on {confirmPaid ? sourceLabel(confirmPaid) : ''}?</DialogTitle>
            <DialogDescription>
              This source uses your Apify credit on every collect run. The Apify spending caps apply (see the Spend page): when a cap is
              reached, Apify stops for the day or month while the free sources keep running. The change only takes effect when you save.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild><Button type="button" variant="outline">Keep it off</Button></DialogClose>
            <Button type="button" onClick={() => { if (confirmPaid) set({ sources: withSource(draft.sources, confirmPaid, true, registry) }); setConfirmPaid(null) }}>Switch it on</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
