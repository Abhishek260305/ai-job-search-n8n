import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { SearchPage } from '@/api/types'
import { locationOf, renderApp } from '@/test/render'
import { SEARCH, server, unauthorized } from '@/test/server'

const page = (over: Partial<SearchPage> = {}): SearchPage => ({ ...SEARCH, ...over })

/** Records the body and header of every PUT /api/search, answering with `answer` (default: a new version of the same page). */
function saves(answer: (body: { profile: SearchPage['profile']; version: string }) => Response | SearchPage = (b) => ({ ...SEARCH, version: 'v2', edited_by: 'dashboard', profile: b.profile as SearchPage['profile'] })) {
  const seen: { body: { profile: SearchPage['profile']; version: string }; header: string | null }[] = []
  server.use(http.put('/api/search', async ({ request }) => {
    const body = (await request.json()) as { profile: SearchPage['profile']; version: string }
    seen.push({ body, header: request.headers.get('x-requested-with') })
    const out = answer(body)
    return out instanceof Response ? out : HttpResponse.json(out)
  }))
  return seen
}

const refusal = (detail: string, status = 422) => () => HttpResponse.json({ detail }, { status })
const saveButton = () => screen.getByRole('button', { name: 'Save changes' })

async function open() {
  const user = userEvent.setup()
  renderApp('/search')
  await screen.findByRole('heading', { name: 'What to search for' }, { timeout: 5000 })
  return user
}

describe('the search page', () => {
  it('is reached from the header and shows every section filled in', { timeout: 20_000 }, async () => {
    const { router } = renderApp('/')
    await userEvent.click(await screen.findByRole('link', { name: 'Search' }))
    expect(await screen.findByRole('heading', { name: 'Search' }, { timeout: 5000 })).toBeInTheDocument()
    expect(locationOf(router)).toBe('/search')
    for (const name of ['What to search for', 'Filters', 'Sources', 'Company boards']) expect(await screen.findByRole('heading', { name })).toBeInTheDocument()
    expect(screen.getByLabelText('Job titles or keywords')).toHaveValue('backend engineer\npython developer')
    expect(screen.getByLabelText('Locations')).toHaveValue('Pune\nRemote')
    expect(screen.getByLabelText('Must not contain')).toHaveValue('intern\nstaff')
    expect(screen.getByLabelText('Companies to skip')).toHaveValue('Acme')
    expect(screen.getByLabelText('Shortlist score')).toHaveValue('60')
    expect(screen.getByLabelText('Most years of experience asked')).toHaveValue('4')
    expect(screen.getByLabelText('Extra title words')).toHaveValue('software')
    expect(screen.getByRole('link', { name: 'Search' })).toHaveAttribute('aria-current', 'page')
  })

  it('starts with nothing to save', async () => {
    await open()
    expect(saveButton()).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Discard changes' })).toBeDisabled()
    expect(screen.getByText('Saved')).toBeInTheDocument()
  })

  it('reports a failed first load with a retry, and goes to the login page on 401', async () => {
    server.use(http.get('/api/search', () => HttpResponse.json({ detail: 'the scraper is not reachable right now' }, { status: 503 })))
    renderApp('/search')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The search profile could not be loaded.')
    server.use(http.get('/api/search', () => HttpResponse.json(SEARCH)))
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'What to search for' })).toBeInTheDocument()
    server.use(http.get('/api/search', unauthorized), http.get('/api/me', unauthorized))
  })

  it('goes to the login page when the session has ended', async () => {
    server.use(http.get('/api/search', unauthorized), http.get('/api/me', unauthorized))
    const { router } = renderApp('/search')
    await waitFor(() => expect(locationOf(router)).toContain('/login'))
  })

  it('warns when the file on the computer is behind a dashboard edit', async () => {
    server.use(http.get('/api/search', () => HttpResponse.json(page({ edited_by: 'dashboard' }))))
    renderApp('/search')
    const note = await screen.findByRole('note')
    expect(note).toHaveTextContent('config/search.json on your computer is behind it')
    expect(note).toHaveTextContent('scripts/jobsearch search-profile pull')
  })

  it('shows no such warning when the command line wrote last', async () => {
    await open()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })
})

describe('editing', () => {
  it('saves what changed, as a whole profile, with the version and the write header', async () => {
    const seen = saves()
    const user = await open()
    const terms = screen.getByLabelText('Job titles or keywords')
    await user.clear(terms); await user.type(terms, '  SRE {Enter}platform engineer{Enter}{Enter}sre')
    await user.clear(screen.getByLabelText('Shortlist score')); await user.type(screen.getByLabelText('Shortlist score'), '55')
    await user.clear(screen.getByLabelText('Most years of experience asked'))
    await user.click(screen.getByRole('checkbox', { name: 'Also keep remote jobs, whatever the location' }))
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()
    await user.click(saveButton())
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0].header).toBe('jobsearch-dashboard')
    expect(seen[0].body.version).toBe(SEARCH.version)
    expect(seen[0].body.profile).toEqual({
      ...SEARCH.profile, search_terms: ['SRE', 'platform engineer'], score_threshold: 55, max_yoe: null, allow_remote: false,
    })
    expect(await screen.findByText('Search profile saved')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Saved')).toBeInTheDocument())
    expect(saveButton()).toBeDisabled()
  })

  it('discards the draft, the typed text in the lists included', async () => {
    const user = await open()
    await user.type(screen.getByLabelText('Companies to skip'), '{Enter}Globex')
    await user.type(screen.getByLabelText('Job titles or keywords'), '{Enter}SRE')
    await user.type(screen.getByRole('textbox', { name: 'Extra title words' }), '{Enter}platform')
    await user.clear(screen.getByLabelText('Shortlist score')); await user.type(screen.getByLabelText('Shortlist score'), '10')
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.getByLabelText('Shortlist score')).toHaveValue('60'))
    expect(screen.getByLabelText('Companies to skip')).toHaveValue('Acme')
    expect(screen.getByLabelText('Job titles or keywords')).toHaveValue('backend engineer\npython developer')
    expect(screen.getByLabelText('Extra title words')).toHaveValue('software')
    expect(saveButton()).toBeDisabled()
    expect(screen.getByText('Saved')).toBeInTheDocument()
  })

  it('does not replace a half-edited draft when the tab is focused again', async () => {
    let reads = 0
    server.use(http.get('/api/search', () => { reads++; return HttpResponse.json(SEARCH) }))
    const user = await open()
    await user.type(screen.getByLabelText('Companies to skip'), '{Enter}Globex')
    window.dispatchEvent(new Event('focus'))
    document.dispatchEvent(new Event('visibilitychange'))
    await new Promise((r) => setTimeout(r, 300))
    expect(reads).toBe(1)
    expect(screen.getByLabelText('Companies to skip')).toHaveValue('Acme\nGlobex')
  })

  it('blocks a save that the scraper would refuse, and says what to fix', async () => {
    const seen = saves()
    const user = await open()
    await user.clear(screen.getByLabelText('Job titles or keywords'))
    const fix = await screen.findByRole('list', { name: 'To fix before saving' })
    expect(fix).toHaveTextContent('Add at least one job title or keyword to search for.')
    expect(saveButton()).toBeDisabled()
    await user.type(screen.getByLabelText('Job titles or keywords'), 'sre')
    await user.clear(screen.getByLabelText('Shortlist score')); await user.type(screen.getByLabelText('Shortlist score'), 'high')
    expect(await screen.findByText(/must be whole numbers/)).toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
    await user.clear(screen.getByLabelText('Shortlist score')); await user.type(screen.getByLabelText('Shortlist score'), '559')
    expect(await screen.findByText('The shortlist score is 0 to 100.')).toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
    expect(seen).toEqual([])
  })

  it('shows the scraper\'s own refusal, one problem per line, and keeps the draft', async () => {
    saves(refusal('exclude_companies: a rule the page could not foresee; boards.lever: invalid slug'))
    const user = await open()
    await user.type(screen.getByLabelText('Companies to skip'), '{Enter}Globex')
    await user.click(saveButton())
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('exclude_companies: a rule the page could not foresee')).toBeInTheDocument()
    expect(within(alert).getByText('boards.lever: invalid slug')).toBeInTheDocument()
    expect(screen.getByLabelText('Companies to skip')).toHaveValue('Acme\nGlobex')
  })

  it('on a conflict says so, offers to reload, and reloads the saved profile', async () => {
    saves(refusal('the search profile was changed somewhere else (the command line, or another tab) since you opened it: reload to see it', 409))
    const user = await open()
    await user.type(screen.getByLabelText('Companies to skip'), '{Enter}Globex')
    await user.click(saveButton())
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('changed somewhere else')
    server.use(http.get('/api/search', () => HttpResponse.json(page({ version: 'v-new', profile: { ...SEARCH.profile, exclude_companies: ['Initech'] } }))))
    await user.click(within(alert).getByRole('button', { name: 'Reload the saved profile' }))
    await waitFor(() => expect(screen.getByLabelText('Companies to skip')).toHaveValue('Initech'))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('sources', () => {
  it('lists every source in its group with what it is, and which are on', async () => {
    await open()
    for (const label of ['Job sites', 'Paid', 'Company board systems', 'Remote job feeds']) expect(screen.getByRole('region', { name: label })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Indeed' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Naukri' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Remotive' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Glassdoor' })).toBeInTheDocument()          // in no group, still a job site
    const paid = screen.getByRole('region', { name: 'Paid' })
    expect(within(paid).getByText('Paid', { selector: '[data-slot=badge]' })).toBeInTheDocument()
    expect(within(paid).getByText('Uses your Apify credit (the spending caps apply).')).toBeInTheDocument()
  })

  it('switches sources and saves the list in registry order', async () => {
    const seen = saves()
    const user = await open()
    await user.click(screen.getByRole('checkbox', { name: 'Naukri' }))
    await user.click(screen.getByRole('checkbox', { name: 'Remotive' }))
    await user.click(screen.getByRole('checkbox', { name: 'Remote OK' }))
    await user.click(saveButton())
    await waitFor(() => expect(seen[0]?.body.profile.sources).toEqual(['indeed', 'naukri', 'greenhouse', 'remoteok']))
  })

  it('warns about a company board that is on with no company, and refuses to save only such boards', async () => {
    const user = await open()
    await user.click(screen.getByRole('checkbox', { name: 'Lever' }))
    expect(await screen.findByText('On, but no company is listed under it yet: it will find nothing.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remove stripe from Greenhouse' }))
    await user.click(screen.getByRole('button', { name: 'Remove notion from Greenhouse' }))      // no company listed anywhere
    for (const name of ['Indeed', 'Remotive', 'Greenhouse']) await user.click(screen.getByRole('checkbox', { name }))   // only Lever is left on
    expect(await screen.findByText(/Only company boards are on and none lists a company/)).toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
  })

  it('asks before switching the paid source on, and not before switching it off', async () => {
    const seen = saves()
    server.use(http.get('/api/search', () => HttpResponse.json(page({ profile: { ...SEARCH.profile, sources: ['indeed', 'linkedin_apify'] } }))))
    const user = await open()
    await user.click(screen.getByRole('checkbox', { name: 'LinkedIn (Apify)' }))                    // it is on: switching it off needs no question
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'LinkedIn (Apify)' })).not.toBeChecked()
    await user.click(screen.getByRole('checkbox', { name: 'LinkedIn (Apify)' }))
    const dialog = await screen.findByRole('dialog', { name: 'Switch on LinkedIn (Apify)?' })
    expect(dialog).toHaveTextContent('uses your Apify credit')
    await user.click(within(dialog).getByRole('button', { name: 'Keep it off' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('checkbox', { name: 'LinkedIn (Apify)' })).not.toBeChecked()
    await user.click(screen.getByRole('checkbox', { name: 'LinkedIn (Apify)' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Switch it on' }))
    expect(screen.getByRole('checkbox', { name: 'LinkedIn (Apify)' })).toBeChecked()
    expect(seen).toEqual([])                                                                        // nothing is saved until Save changes
  })

  it('refuses to save with every source off', async () => {
    const user = await open()
    for (const name of ['Indeed', 'Greenhouse', 'Remotive']) await user.click(screen.getByRole('checkbox', { name }))
    expect(await screen.findByText('Switch at least one source on.')).toBeInTheDocument()
    expect(saveButton()).toBeDisabled()
  })
})

describe('company boards', () => {
  const section = (name: string) => within(screen.getByRole('region', { name }))

  it('lists the companies with their display names and counts', async () => {
    await open()
    const gh = section('Greenhouse companies')
    expect(gh.getByRole('heading', { name: /Greenhouse \(2\)/ })).toBeInTheDocument()
    const items = within(gh.getByRole('list', { name: 'Greenhouse list' })).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('stripe')
    expect(items[1]).toHaveTextContent('notion · Notion')
    expect(screen.getByRole('checkbox', { name: 'Greenhouse' }).closest('li')).toHaveTextContent('2 companies')
  })

  it('adds a company with and without a name, and saves bare slugs and named entries', async () => {
    const seen = saves()
    const user = await open()
    const lever = section('Lever companies')
    await user.type(lever.getByLabelText('Company slug'), ' spotify ')
    await user.click(lever.getByRole('button', { name: 'Add' }))
    expect(lever.getByRole('heading', { name: /Lever \(1\)/ })).toBeInTheDocument()
    await user.type(lever.getByLabelText('Company slug'), 'figma'); await user.type(lever.getByLabelText('Display name (optional)'), 'Figma Inc')
    await user.click(lever.getByRole('button', { name: 'Add' }))
    expect(lever.getByLabelText('Company slug')).toHaveValue('')
    await user.click(saveButton())
    await waitFor(() => expect(seen).toHaveLength(1))
    expect(seen[0].body.profile.boards).toEqual({
      greenhouse: [{ slug: 'stripe', name: null }, { slug: 'notion', name: 'Notion' }],
      lever: [{ slug: 'spotify', name: null }, { slug: 'figma', name: 'Figma Inc' }],
    })
  })

  it('refuses a bad or repeated slug with the reason', async () => {
    const user = await open()
    const gh = section('Greenhouse companies')
    await user.type(gh.getByLabelText('Company slug'), '../evil')
    await user.click(gh.getByRole('button', { name: 'Add' }))
    expect(await gh.findByRole('alert')).toHaveTextContent('letters, digits, dots, dashes and underscores')
    await user.clear(gh.getByLabelText('Company slug')); await user.type(gh.getByLabelText('Company slug'), 'STRIPE')
    await user.click(gh.getByRole('button', { name: 'Add' }))
    expect(await gh.findByRole('alert')).toHaveTextContent('STRIPE is already listed under greenhouse')
    expect(gh.getByRole('heading', { name: /Greenhouse \(2\)/ })).toBeInTheDocument()
  })

  it('removes a company', async () => {
    const seen = saves()
    const user = await open()
    await user.click(screen.getByRole('button', { name: 'Remove stripe from Greenhouse' }))
    expect(section('Greenhouse companies').getByRole('heading', { name: /Greenhouse \(1\)/ })).toBeInTheDocument()
    await user.click(saveButton())
    await waitFor(() => expect(seen[0]?.body.profile.boards).toEqual({ greenhouse: [{ slug: 'notion', name: 'Notion' }] }))
  })

  it('finds a company, offers the matches, and adds one', async () => {
    const asked: unknown[] = []
    server.use(http.post('/api/search/boards/find', async ({ request }) => {
      asked.push(await request.json())
      return HttpResponse.json({ name: 'Spotify', tried: ['spotify', 'Spotify'], matches: [{ system: 'lever', slug: 'spotify', jobs: 9 }, { system: 'greenhouse', slug: 'stripe', jobs: 3 }] })
    }))
    const user = await open()
    await user.type(screen.getByRole('textbox', { name: 'Find a company' }), 'Spotify')
    await user.click(screen.getByRole('button', { name: 'Find' }))
    const matches = await screen.findByRole('list', { name: 'Matches' })
    expect(asked).toEqual([{ name: 'Spotify' }])
    const [lever, gh] = within(matches).getAllByRole('listitem')
    expect(lever).toHaveTextContent('Lever'); expect(lever).toHaveTextContent('9 open jobs')
    expect(within(gh).getByRole('button', { name: 'Add stripe to Greenhouse' })).toBeDisabled()          // already listed
    expect(within(gh).getByText('Listed')).toBeInTheDocument()
    await user.click(within(lever).getByRole('button', { name: 'Add spotify to Lever' }))
    expect(section('Lever companies').getByRole('heading', { name: /Lever \(1\)/ })).toBeInTheDocument()
    expect(within(lever).getByText('Listed')).toBeInTheDocument()
  })

  it('says how to find the slug by hand when there is no match', async () => {
    server.use(http.post('/api/search/boards/find', () => HttpResponse.json({ name: 'Nowhere Inc', tried: ['nowhereinc', 'nowhere-inc', 'nowhere'], matches: [] })))
    const user = await open()
    await user.type(screen.getByRole('textbox', { name: 'Find a company' }), 'Nowhere Inc')
    await user.click(screen.getByRole('button', { name: 'Find' }))
    const note = await screen.findByRole('status')
    expect(note).toHaveTextContent('No match for "Nowhere Inc" (tried nowhereinc, nowhere-inc, nowhere)')
    expect(note).toHaveTextContent('look at the address')
  })

  it('shows the scraper\'s message when a name cannot make a slug', async () => {
    server.use(http.post('/api/search/boards/find', refusal('that company name does not make a usable slug: give a shorter name, for example: boards find Stripe')))
    const user = await open()
    await user.type(screen.getByRole('textbox', { name: 'Find a company' }), '!!!')
    await user.click(screen.getByRole('button', { name: 'Find' }))
    expect(await screen.findByText(/does not make a usable slug/)).toBeInTheDocument()
  })

  it('does not search for an empty name', async () => {
    await open()
    expect(screen.getByRole('button', { name: 'Find' })).toBeDisabled()
  })
})
