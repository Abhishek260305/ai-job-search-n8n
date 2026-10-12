import { useSpend } from '@/api/hooks'
import AppFooter from '@/components/AppFooter'
import AppHeader from '@/components/AppHeader'
import ModelsCard from '@/components/ops/ModelsCard'
import { PanelError, PanelSkeleton } from '@/components/ops/PanelState'
import SpendChart from '@/components/ops/SpendChart'
import SpendMeters from '@/components/ops/SpendMeters'

export default function Spend() {
  const spend = useSpend()
  return (
    <>
      <AppHeader page="spend" />
      <main className="mx-auto max-w-[96rem] space-y-4 p-4 md:p-6">
        <h2 className="font-heading text-lg font-semibold">Spend</h2>
        {spend.data ? (
          <>
            <SpendMeters spend={spend.data} />
            <SpendChart spend={spend.data} />
            <ModelsCard models={spend.data.models} />
          </>
        ) : spend.isError ? (
          <PanelError what="The spend report" error={spend.error} retry={() => spend.refetch()} />
        ) : (
          <PanelSkeleton label="Loading the spend report" />
        )}
      </main>
      <AppFooter />
    </>
  )
}
