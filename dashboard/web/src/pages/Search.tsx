import { useEffect, useMemo, useState } from 'react'
import { ApiError } from '@/api/client'
import { useSaveSearch, useSearch } from '@/api/hooks'
import type { SearchPage, SearchProfile, SearchProfileIn } from '@/api/types'
import AppFooter from '@/components/AppFooter'
import AppHeader from '@/components/AppHeader'
import { PanelError, PanelSkeleton } from '@/components/ops/PanelState'
import BoardsCard from '@/components/search/BoardsCard'
import { FiltersCard, type NumberText, SearchCard } from '@/components/search/SearchCards'
import SourcesCard from '@/components/search/SourcesCard'
import { Button } from '@/components/ui/button'
import { hints, optionalInt, problemsOf, sameProfile } from '@/lib/searchForm'

const text = (n: number | null) => (n === null ? '' : String(n))

/** The draft, the cards and the save bar for one version of the profile. A new version (after a save) remounts it. */
function SearchEditor({ page, reload, discard }: { page: SearchPage; reload: () => void; discard: () => void }) {
  const save = useSaveSearch()
  const [draft, setDraft] = useState<SearchProfile>(page.profile)
  const [numbers, setNumbers] = useState<NumberText>({
    min_yoe: text(page.profile.min_yoe), max_yoe: text(page.profile.max_yoe),
    max_age_days: text(page.profile.max_age_days), score_threshold: text(page.profile.score_threshold),
  })

  // the numbers are typed as text; the profile that is compared and saved has them parsed
  const parsed = useMemo(() => ({
    min_yoe: optionalInt(numbers.min_yoe), max_yoe: optionalInt(numbers.max_yoe),
    max_age_days: numbers.max_age_days.trim() === '' ? Number.NaN : optionalInt(numbers.max_age_days),
    score_threshold: numbers.score_threshold.trim() === '' ? Number.NaN : optionalInt(numbers.score_threshold),
  }), [numbers])
  const numbersOk = !Number.isNaN(parsed.min_yoe) && !Number.isNaN(parsed.max_yoe) && parsed.max_age_days !== null && !Number.isNaN(parsed.max_age_days)
    && parsed.score_threshold !== null && !Number.isNaN(parsed.score_threshold)
  const profile: SearchProfile = { ...draft, min_yoe: parsed.min_yoe as number | null, max_yoe: parsed.max_yoe as number | null,
    max_age_days: (parsed.max_age_days ?? 7) as number, score_threshold: (parsed.score_threshold ?? 0) as number }

  const dirty = !numbersOk || !sameProfile(profile, page.profile)
  const problems = [...(numbersOk ? [] : ['The numbers must be whole numbers (the least and most years may be empty).']), ...hints(profile)]
  const conflict = save.error instanceof ApiError && save.error.status === 409

  // a half-edited profile is easy to lose by closing the tab
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const set = (patch: Partial<SearchProfile>) => setDraft((d) => ({ ...d, ...patch }))

  return (
    <>
      <SearchCard draft={draft} set={set} numbers={numbers} setNumber={(key, value) => setNumbers((n) => ({ ...n, [key]: value }))} />
      <FiltersCard draft={draft} set={set} />
      <SourcesCard draft={draft} registry={page.registry} set={set} />
      <BoardsCard draft={draft} registry={page.registry} set={set} />

      <div className="sticky bottom-0 z-10 -mx-4 border-t bg-background/95 px-4 py-3 backdrop-blur md:-mx-6 md:px-6">
        <div className="mx-auto grid max-w-[96rem] gap-2">
          {problems.length > 0 && dirty && (
            <ul aria-label="To fix before saving" className="grid gap-0.5 text-sm text-amber-700 dark:text-amber-400">
              {problems.map((p) => <li key={p}>{p}</li>)}
            </ul>
          )}
          {save.error && (
            <div role="alert" className="grid gap-1 text-sm text-destructive">
              {conflict ? (
                <p>{save.error.message} <Button type="button" variant="link" className="h-auto p-0" onClick={reload}>Reload the saved profile</Button> (your edits on this page are discarded).</p>
              ) : (
                problemsOf(save.error instanceof ApiError ? save.error.detail : 'The profile could not be saved.').map((p) => <p key={p}>{p}</p>)
              )}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" className="h-10" disabled={!dirty || problems.length > 0 || save.isPending}
              onClick={() => save.mutate({ profile: profile as SearchProfileIn, version: page.version })}>
              {save.isPending ? 'Saving…' : 'Save changes'}
            </Button>
            <Button type="button" variant="outline" className="h-10" disabled={!dirty || save.isPending}
              onClick={() => { save.reset(); discard() }}>
              Discard changes
            </Button>
            <span className="text-sm text-muted-foreground" aria-live="polite">{dirty ? 'Unsaved changes' : 'Saved'}</span>
          </div>
        </div>
      </div>
    </>
  )
}

export default function Search() {
  const search = useSearch()
  const page = search.data
  // Discard remounts the editor, so every field (the textareas keep their own text) goes back to the saved profile
  const [discarded, setDiscarded] = useState(0)
  return (
    <>
      <AppHeader page="search" />
      <main className="mx-auto max-w-[96rem] space-y-4 p-4 md:p-6">
        <h2 className="font-heading text-lg font-semibold">Search</h2>
        <p className="text-sm text-muted-foreground">
          What the pipeline searches for and where it looks. Saved changes apply from the next collect run and use the same checks as the command line.
        </p>
        {page?.edited_by === 'dashboard' && (
          <p role="note" className="rounded-lg bg-amber-100 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
            This profile was last saved here, so <code>config/search.json</code> on your computer is behind it. Before you edit that file or run{' '}
            <code>search-profile apply</code>, run <code>scripts/jobsearch search-profile pull</code>: the command refuses to overwrite these changes until you do.
          </p>
        )}
        {page ? (
          <SearchEditor key={`${page.version}:${discarded}`} page={page} reload={() => { void search.refetch() }} discard={() => setDiscarded((n) => n + 1)} />
        ) : search.isError ? (
          <PanelError what="The search profile" error={search.error} retry={() => search.refetch()} />
        ) : (
          <PanelSkeleton label="Loading the search profile" />
        )}
      </main>
      <AppFooter />
    </>
  )
}
