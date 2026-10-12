import type { SearchProfile } from '@/api/types'
import ListField from '@/components/search/ListField'
import { Field, INPUT_CLASS } from '@/components/settings/parts'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

export type NumberText = { min_yoe: string; max_yoe: string; max_age_days: string; score_threshold: string }

const bad = (text: string, optional: boolean) => (optional && text.trim() === '' ? false : !/^\d{1,4}$/.test(text.trim()))

/** What to search for: the title words and places, and the free filters that run before any AI is paid for. */
export function SearchCard({ draft, set, numbers, setNumber }: {
  draft: SearchProfile
  set: (patch: Partial<SearchProfile>) => void
  numbers: NumberText
  setNumber: (key: keyof NumberText, text: string) => void
}) {
  const num = (key: keyof NumberText, label: string, hint: string, optional = false) => (
    <Field id={`search-${key}`} label={label} hint={hint}>
      <input id={`search-${key}`} aria-describedby={`search-${key}-hint`} aria-invalid={bad(numbers[key], optional)} className={INPUT_CLASS} inputMode="numeric"
        value={numbers[key]} onChange={(e) => setNumber(key, e.target.value)} />
    </Field>
  )
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>What to search for</CardTitle>
        <p className="text-sm text-muted-foreground">Each job title is searched in each place, on every job site that is on.</p>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-2">
        <ListField id="search-terms" label="Job titles or keywords" hint="One per line, for example backend engineer" value={draft.search_terms}
          onChange={(search_terms) => set({ search_terms })} />
        <ListField id="search-locations" label="Locations" hint="One per line, for example Pune or Remote" value={draft.locations}
          onChange={(locations) => set({ locations })} />
        <div className="flex items-center gap-2 md:col-span-2">
          <Checkbox id="search-remote" checked={draft.allow_remote} onCheckedChange={(v) => set({ allow_remote: v === true })} />
          <Label htmlFor="search-remote">Also keep remote jobs, whatever the location</Label>
        </div>
        {num('min_yoe', 'Least years of experience asked', 'Empty: no lower limit', true)}
        {num('max_yoe', 'Most years of experience asked', 'Jobs asking for more are filtered out before scoring. Empty: no limit', true)}
        {num('max_age_days', 'Newest postings only (days old)', '1 to 90')}
        {num('score_threshold', 'Shortlist score', 'A scored job at or above this joins your shortlist (0 to 100)')}
      </CardContent>
    </Card>
  )
}

/** The free rule filters: words a title or posting must or must not have, and companies to skip. */
export function FiltersCard({ draft, set }: { draft: SearchProfile; set: (patch: Partial<SearchProfile>) => void }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle role="heading" aria-level={3}>Filters</CardTitle>
        <p className="text-sm text-muted-foreground">These run before any AI is called, so they cost nothing.</p>
      </CardHeader>
      <CardContent className="grid gap-4 md:grid-cols-3">
        <ListField id="filter-include" label="Must contain one of" hint="One per line. Empty: no requirement" value={draft.include_keywords}
          onChange={(include_keywords) => set({ include_keywords })} />
        <ListField id="filter-exclude" label="Must not contain" hint="One per line, for example intern, staff, principal" value={draft.exclude_keywords}
          onChange={(exclude_keywords) => set({ exclude_keywords })} />
        <ListField id="filter-companies" label="Companies to skip" hint="One per line" value={draft.exclude_companies}
          onChange={(exclude_companies) => set({ exclude_companies })} />
      </CardContent>
    </Card>
  )
}
