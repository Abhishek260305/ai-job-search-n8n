import { useBoard, useSummary } from '@/api/hooks'
import AppFooter from '@/components/AppFooter'
import AppHeader from '@/components/AppHeader'
import FollowUps from '@/components/board/FollowUps'
import KanbanBoard from '@/components/board/KanbanBoard'
import StatsCards from '@/components/board/StatsCards'
import WeeklyTable from '@/components/board/WeeklyTable'
import JobDrawer from '@/components/JobDrawer'
import { PanelError, PanelSkeleton } from '@/components/ops/PanelState'
import { useJobsParams } from '@/lib/jobsParams'

export default function BoardPage() {
  const board = useBoard()
  const summary = useSummary()
  const { view, setView } = useJobsParams()
  return (
    <>
      <AppHeader page="board" />
      <main className="mx-auto max-w-[96rem] space-y-4 p-4 md:p-6">
        <div>
          <h2 className="font-heading text-lg font-semibold">Board</h2>
          <p className="text-sm text-muted-foreground">
            Drag a job to the stage it has reached, or use the status menu on the card. You decide and you apply: nothing here sends anything.
          </p>
        </div>
        <StatsCards />
        {board.data ? (
          <>
            <FollowUps board={board.data} onOpen={(id) => setView(view, id)} />
            <KanbanBoard board={board.data} threshold={summary.data?.threshold ?? null} onOpen={(id) => setView(view, id)} />
            <p className="text-sm text-muted-foreground">
              Not shown: {board.data.closed.withdrawn} withdrawn and {board.data.closed.skipped} skipped. They are on the Jobs page under All.
            </p>
          </>
        ) : board.isError ? (
          <PanelError what="The board" error={board.error} retry={() => board.refetch()} />
        ) : (
          <PanelSkeleton label="Loading the board" />
        )}
        <WeeklyTable />
      </main>
      <AppFooter />
      <JobDrawer neighbours={false} />
    </>
  )
}
