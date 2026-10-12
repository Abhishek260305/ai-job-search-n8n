import { useSettings, useSummary } from '@/api/hooks'
import AppFooter from '@/components/AppFooter'
import AppHeader from '@/components/AppHeader'
import { PanelError, PanelSkeleton } from '@/components/ops/PanelState'
import BudgetCard from '@/components/settings/BudgetCard'
import FeaturesCard from '@/components/settings/FeaturesCard'
import ScheduleCard from '@/components/settings/ScheduleCard'
import TailoringCard from '@/components/settings/TailoringCard'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

export default function Settings() {
  const settings = useSettings()
  const summary = useSummary()
  const page = settings.data
  return (
    <>
      <AppHeader page="settings" />
      <main className="mx-auto max-w-[96rem] space-y-4 p-4 md:p-6">
        <h2 className="font-heading text-lg font-semibold">Settings</h2>
        <p className="text-sm text-muted-foreground">
          Changes are saved as you confirm them and use the same checks as the command line. What changes how money or approvals
          flow (raising a cap, the approval mode) stays a command.
        </p>
        {page ? (
          <>
            <FeaturesCard features={page.features} />
            <Card>
              <CardHeader>
                <CardTitle role="heading" aria-level={3}>Schedules</CardTitle>
                <p className="text-sm text-muted-foreground">
                  {page.paused ? 'Everything is paused (Health page): nothing runs until you resume. ' : ''}
                  The scheduler checks every 5 minutes, so a change applies at the next check. Daily times are in {page.timezone}; the
                  time zone is changed with <code>scripts/jobsearch schedule timezone</code>, because it also moves the day and month the budget counts from.
                </p>
              </CardHeader>
              <CardContent>
                <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
                  {page.schedules.map((s) => (
                    // Re-keyed by what is editable only: a background reload of "last run" must not wipe a half-typed form.
                    <ScheduleCard key={[s.name, s.enabled, s.every_minutes, s.at_time, s.batch, s.board_batch, s.model].join('|')}
                      saved={s} limits={page.limits} paused={page.paused}
                      featureOff={(s.name === 'tailor' && !page.features.effective.tailoring.on) || (s.name === 'digest' && !page.features.effective.digest.on)} />
                  ))}
                </div>
              </CardContent>
            </Card>
            <TailoringCard key={JSON.stringify(page.tailoring)} saved={page.tailoring} threshold={summary.data?.threshold ?? null} />
            <BudgetCard key={JSON.stringify(page.budget)} saved={page.budget} />
          </>
        ) : settings.isError ? (
          <PanelError what="The settings" error={settings.error} retry={() => settings.refetch()} />
        ) : (
          <PanelSkeleton label="Loading the settings" />
        )}
      </main>
      <AppFooter />
    </>
  )
}
