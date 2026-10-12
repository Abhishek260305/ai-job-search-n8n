import { Plus, Search, X } from 'lucide-react'
import { type FormEvent, useState } from 'react'
import { useFindBoard } from '@/api/hooks'
import type { SearchProfile } from '@/api/types'
import ListField from '@/components/search/ListField'
import { Field, FormError, INPUT_CLASS } from '@/components/settings/parts'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { sourceLabel } from '@/lib/jobsTable'
import { addCompany, removeCompany, type Registry } from '@/lib/searchForm'

type Entries = { slug: string; name?: string | null }[]
const entriesOf = (boards: SearchProfile['boards'], system: string): Entries => (boards as Record<string, Entries | undefined>)[system] ?? []

/** One board system's add form: a slug, an optional display name. */
function AddCompany({ system, onAdd }: { system: string; onAdd: (slug: string, name: string) => string | null }) {
  const [slug, setSlug] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  function submit(event: FormEvent) {
    event.preventDefault()
    const problem = onAdd(slug, name)
    setError(problem)
    if (!problem) { setSlug(''); setName('') }
  }
  return (
    <form onSubmit={submit} aria-label={`Add a ${sourceLabel(system)} company`} className="grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
      <Field id={`add-${system}-slug`} label="Company slug">
        <input id={`add-${system}-slug`} className={INPUT_CLASS} spellCheck={false} autoCapitalize="none" value={slug} onChange={(e) => setSlug(e.target.value)} />
      </Field>
      <Field id={`add-${system}-name`} label="Display name (optional)">
        <input id={`add-${system}-name`} className={INPUT_CLASS} value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Button type="submit" variant="outline" className="h-10 gap-1.5" disabled={slug.trim() === ''}><Plus aria-hidden="true" /> Add</Button>
      {error && <p role="alert" className="text-sm text-destructive sm:col-span-3">{error}</p>}
    </form>
  )
}

/**
 * The companies whose job boards are read, per board system, and "find this company": it asks the five boards'
 * public APIs which one a company uses (at most 20 read-only requests) and offers the matches to add.
 */
export default function BoardsCard({ draft, registry, set }: { draft: SearchProfile; registry: Registry; set: (patch: Partial<SearchProfile>) => void }) {
  const find = useFindBoard()
  const [name, setName] = useState('')
  const [addError, setAddError] = useState<string | null>(null)

  function add(system: string, slug: string, display: string): string | null {
    const result = addCompany(draft.boards, system, slug, display, registry)
    if (result.error) return result.error
    set({ boards: result.boards })
    return null
  }

  function submitFind(event: FormEvent) {
    event.preventDefault()
    if (name.trim()) find.mutate(name.trim())
  }

  const listed = (system: string, slug: string) => entriesOf(draft.boards, system).some((e) => (system === 'smartrecruiters' ? e.slug === slug : e.slug.toLowerCase() === slug.toLowerCase()))

  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Company boards</CardTitle>
        <p className="text-sm text-muted-foreground">
          Companies whose own job board is read directly, with no search needed. A company's slug is its name in its job-board address
          (for example <code>boards.greenhouse.io/stripe</code>). Big companies with their own career site or on Workday are not covered.
        </p>
      </CardHeader>
      <CardContent className="grid gap-6">
        <form onSubmit={submitFind} aria-label="Find a company" className="grid gap-2 rounded-xl border p-3">
          <Field id="find-name" label="Find a company" hint="Type its name; it looks for the company on Greenhouse, Lever, Ashby, Workable and SmartRecruiters.">
            <div className="flex gap-2">
              <input id="find-name" aria-describedby="find-name-hint" className={INPUT_CLASS} value={name} onChange={(e) => setName(e.target.value)} />
              <Button type="submit" variant="outline" className="h-10 gap-1.5" disabled={name.trim() === '' || find.isPending}>
                <Search aria-hidden="true" /> {find.isPending ? 'Looking…' : 'Find'}
              </Button>
            </div>
          </Field>
          <FormError error={find.error} />
          {find.data && (
            find.data.matches.length > 0 ? (
              <ul aria-label="Matches" className="grid gap-1.5 text-sm">
                {find.data.matches.map((m) => (
                  <li key={`${m.system}/${m.slug}`} className="flex flex-wrap items-center gap-x-3 gap-y-1">
                    <span className="font-medium">{sourceLabel(m.system)}</span>
                    <code>{m.slug}</code>
                    <span className="text-muted-foreground">
                      {m.jobs} open {m.jobs === 1 ? 'job' : 'jobs'}{m.jobs === 0 ? ': it may not be this company\'s board, check before adding' : ''}
                    </span>
                    <Button type="button" size="sm" variant="outline" className="ml-auto" disabled={listed(m.system, m.slug)}
                      aria-label={`Add ${m.slug} to ${sourceLabel(m.system)}`}
                      onClick={() => setAddError(add(m.system, m.slug, ''))}>
                      {listed(m.system, m.slug) ? 'Listed' : 'Add'}
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p role="status" className="text-sm text-muted-foreground">
                No match for "{find.data.name}" (tried {find.data.tried.join(', ')}). Open the company's careers page, click a job and look at the
                address: the slug is the part after the board's name. Then add it below.
              </p>
            )
          )}
          {addError && <p role="alert" className="text-sm text-destructive">{addError}</p>}
        </form>

        {registry.board_systems.map((system) => {
          const entries = entriesOf(draft.boards, system)
          return (
            <section key={system} aria-label={`${sourceLabel(system)} companies`} className="grid gap-2">
              <h4 className="text-sm font-medium">{sourceLabel(system)} <span className="font-normal text-muted-foreground">({entries.length})</span></h4>
              {entries.length > 0 && (
                <ul aria-label={`${sourceLabel(system)} list`} className="grid gap-1.5 text-sm sm:grid-cols-2">
                  {entries.map((e) => (
                    <li key={e.slug} className="flex items-center gap-2 rounded-lg border px-2.5 py-1.5">
                      <span className="min-w-0 flex-1 truncate"><code>{e.slug}</code>{e.name ? <span className="text-muted-foreground"> · {e.name}</span> : null}</span>
                      <Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Remove ${e.slug} from ${sourceLabel(system)}`}
                        onClick={() => set({ boards: removeCompany(draft.boards, system, e.slug) })}>
                        <X aria-hidden="true" />
                      </Button>
                    </li>
                  ))}
                </ul>
              )}
              <AddCompany system={system} onAdd={(slug, display) => add(system, slug, display)} />
            </section>
          )
        })}

        <ListField id="board-title-words" label="Extra title words" value={draft.board_title_words} rows={3}
          hint="A company-board or feed job whose title has one of these words is kept even if it matches none of your job titles, for example software. One per line."
          onChange={(board_title_words) => set({ board_title_words })} />
      </CardContent>
    </Card>
  )
}
