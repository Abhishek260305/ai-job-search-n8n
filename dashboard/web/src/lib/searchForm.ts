// The search page's rules, apart from the components so they can be checked directly. The scraper validates the whole
// profile for real (the CLI's own checks); these helpers shape what is typed, keep the list editing tidy, and give the
// early hints so most mistakes are seen before a save.
import type { BoardEntry, SearchPage, SearchProfile } from '@/api/types'

export type Registry = SearchPage['registry']
export type Boards = SearchProfile['boards']

/** A textarea's lines as a list: trimmed, empty lines dropped, repeats dropped (the first spelling wins). */
export function splitLines(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim()
    if (line && !seen.has(line.toLowerCase())) {
      seen.add(line.toLowerCase())
      out.push(line)
    }
  }
  return out
}

export const joinLines = (items: readonly string[]): string => items.join('\n')

/** A whole number typed in a box: null when empty, NaN when it is something else (the form shows that). */
export function optionalInt(text: string): number | null {
  const t = text.trim()
  if (t === '') return null
  return /^\d{1,4}$/.test(t) ? Number(t) : Number.NaN
}

export type SourceGroup = { key: string; label: string; note?: string; sources: string[] }

/** The sources in the groups the page shows them in, from the scraper's registry (the single list of what exists). */
export function sourceGroups(registry: Registry): SourceGroup[] {
  const inGroup = new Set(Object.values(registry.groups).flat())
  const loose = registry.sources.filter((s) => !inGroup.has(s))                  // glassdoor, ziprecruiter, google: in no group
  const paid = new Set(registry.paid)
  const free = (names: string[]) => names.filter((n) => !paid.has(n))
  return [
    { key: 'sites', label: 'Job sites', note: 'Free. Searched with your search terms and locations.', sources: [...free(registry.groups.jobspy ?? []), ...loose] },
    { key: 'paid', label: 'Paid', note: 'Uses your Apify credit (the spending caps apply).', sources: registry.paid },
    { key: 'boards', label: 'Company board systems', note: 'Each needs the companies you list below.', sources: registry.board_systems },
    { key: 'feeds', label: 'Remote job feeds', note: 'Free; they need nothing from you.', sources: registry.groups.feeds ?? [] },
  ].filter((g) => g.sources.length > 0)
}

/** Switch one source on or off, keeping the registry's order. */
export function withSource(enabled: readonly string[], name: string, on: boolean, registry: Registry): string[] {
  const set = new Set(enabled)
  if (on) set.add(name)
  else set.delete(name)
  return registry.sources.filter((s) => set.has(s))
}

export const SLUG = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

export function companyCount(boards: Boards): number {
  return Object.values(boards).reduce((n, entries) => n + (entries?.length ?? 0), 0)
}

const sameSlug = (system: string, a: string, b: string) => (system === 'smartrecruiters' ? a === b : a.toLowerCase() === b.toLowerCase())

/** Add a company to a board system, or say why not. SmartRecruiters slugs are case-sensitive, the others are not. */
export function addCompany(boards: Boards, system: string, slug: string, name: string, registry: Registry): { boards: Boards; error: string | null } {
  const s = slug.trim()
  const n = name.trim()
  if (!SLUG.test(s)) {
    return { boards, error: 'A company slug uses letters, digits, dots, dashes and underscores (at most 64), starting with a letter or digit: it is the company\'s name in its job-board address.' }
  }
  if (n.length > registry.limits.max_board_name) return { boards, error: `A display name can be at most ${registry.limits.max_board_name} characters.` }
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(name)) return { boards, error: 'A display name cannot contain control characters.' }
  const current = (boards as Record<string, BoardEntry[] | undefined>)[system] ?? []
  if (current.some((e) => sameSlug(system, e.slug, s))) return { boards, error: `${s} is already listed under ${system}.` }
  if (companyCount(boards) >= registry.limits.max_board_slugs) return { boards, error: `At most ${registry.limits.max_board_slugs} companies in total.` }
  return { boards: { ...boards, [system]: [...current, { slug: s, name: n || null }] }, error: null }
}

export function removeCompany(boards: Boards, system: string, slug: string): Boards {
  const current = (boards as Record<string, BoardEntry[] | undefined>)[system] ?? []
  const next = current.filter((e) => e.slug !== slug)
  const others = Object.entries(boards as Record<string, BoardEntry[]>).filter(([key]) => key !== system)
  return Object.fromEntries(next.length ? [...others, [system, next]] : others) as Boards
}

/** Equal profiles, whatever the order of a list's keys or an empty board list's presence. */
export function sameProfile(a: SearchProfile, b: SearchProfile): boolean {
  const norm = (p: SearchProfile) => JSON.stringify({
    ...p,
    boards: Object.fromEntries(Object.entries(p.boards).filter(([, v]) => v.length).sort(([x], [y]) => x.localeCompare(y))),
    sources: [...p.sources].sort(),                      // the order of the list is the registry's, not a setting
  })
  return norm(a) === norm(b)
}

/** What is wrong before it is sent: the same rules as the scraper's, as short hints (the scraper still decides). */
export function hints(p: SearchProfile): string[] {
  const out: string[] = []
  if (p.search_terms.length === 0) out.push('Add at least one job title or keyword to search for.')
  if (p.locations.length === 0) out.push('Add at least one location.')
  if (p.sources.length === 0) out.push('Switch at least one source on.')
  else if (p.sources.every((s) => ['greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters'].includes(s)) && companyCount(p.boards) === 0) {
    out.push('Only company boards are on and none lists a company, so a collect run would find nothing: add a company or switch another source on.')
  }
  // the size limits the dashboard API puts on one save (dashboard/api/models.py SearchProfileIn)
  const tooMany = (list: readonly string[], max: number, what: string) => (list.length > max ? [`${what}: at most ${max}.`] : [])
  const tooLong = (list: readonly string[], max: number, what: string) => (list.some((x) => x.length > max) ? [`${what}: each at most ${max} characters.`] : [])
  out.push(...tooMany(p.search_terms, 100, 'Job titles'), ...tooLong(p.search_terms, 200, 'Job titles'))
  out.push(...tooMany(p.locations, 100, 'Locations'), ...tooLong(p.locations, 200, 'Locations'))
  out.push(...tooMany(p.include_keywords, 200, 'Must contain'), ...tooLong(p.include_keywords, 200, 'Must contain'))
  out.push(...tooMany(p.exclude_keywords, 200, 'Must not contain'), ...tooLong(p.exclude_keywords, 200, 'Must not contain'))
  out.push(...tooMany(p.exclude_companies, 500, 'Companies to skip'), ...tooLong(p.exclude_companies, 200, 'Companies to skip'))
  out.push(...tooMany(p.board_title_words, 50, 'Extra title words'), ...tooLong(p.board_title_words, 50, 'Extra title words'))
  // the ranges the scraper checks (profile_tools.validate_search)
  if ((p.min_yoe !== null && p.min_yoe > 60) || (p.max_yoe !== null && p.max_yoe > 60)) out.push('Years of experience can be 0 to 60.')
  if (p.max_age_days < 1 || p.max_age_days > 90) out.push('Newest postings only: 1 to 90 days.')
  if (p.score_threshold < 0 || p.score_threshold > 100) out.push('The shortlist score is 0 to 100.')
  if (p.min_yoe !== null && p.max_yoe !== null && p.min_yoe > p.max_yoe) out.push('The least years of experience is above the most.')
  return out
}

/** The scraper joins its problems with "; " (scraper_client): one per line reads better. */
export function problemsOf(detail: string): string[] {
  return detail.split('; ').map((s) => s.trim()).filter(Boolean)
}
