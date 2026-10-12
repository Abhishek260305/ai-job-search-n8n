import { usePipeline } from '@/api/hooks'
import AppFooter from '@/components/AppFooter'
import AppHeader from '@/components/AppHeader'
import AlertsCard from '@/components/ops/AlertsCard'
import { PanelError, PanelSkeleton } from '@/components/ops/PanelState'
import PauseCard from '@/components/ops/PauseCard'
import QueueCard from '@/components/ops/QueueCard'
import SchedulesCard from '@/components/ops/SchedulesCard'
import SourcesCard from '@/components/ops/SourcesCard'

export default function Health() {
  const health = usePipeline()
  return (
    <>
      <AppHeader page="health" />
      <main className="mx-auto max-w-[96rem] space-y-4 p-4 md:p-6">
        <h2 className="font-heading text-lg font-semibold">Health</h2>
        {health.data ? (
          <>
            <AlertsCard alerts={health.data.alerts} />
            <PauseCard state={health.data.paused} />
            <SchedulesCard schedules={health.data.schedules} paused={health.data.paused.paused} timezone={health.data.timezone} />
            <div className="grid gap-4 lg:grid-cols-2">
              <QueueCard queue={health.data.queue} parked={health.data.parked} />
              <SourcesCard sources={health.data.sources} />
            </div>
          </>
        ) : health.isError ? (
          <PanelError what="The health report" error={health.error} retry={() => health.refetch()} />
        ) : (
          <PanelSkeleton label="Loading the health report" />
        )}
      </main>
      <AppFooter />
    </>
  )
}
