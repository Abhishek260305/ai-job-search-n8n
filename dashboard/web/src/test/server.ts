import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import type { Board, Health, JobDetail, JobPage, JobRow, SearchPage, SettingsPage, Spend, Stats, Summary, JobEvent, Tracking } from '@/api/types'

export const ME = { authenticated: true, expires_at: '2026-10-19T09:30:00Z' }

/** Shaped like the live numbers: most stages still at zero. `generated_at` is set per test where it matters. */
export const SUMMARY: Summary = {
  threshold: 60,
  cards: {
    found: 1234, shortlisted: 43, new_this_week: 7, applied: 5, in_progress: 2, interview: 1, offer: 0, rejected: 2,
    withdrawn: 0,
  },
  by_status: { applied: 0, in_progress: 2, interview: 1, offer: 0, rejected: 2, withdrawn: 0, not_applied: 43 },
  funnel: [
    { step: 'found', count: 1234 },
    { step: 'passed_filters', count: 600 },
    { step: 'scored', count: 590 },
    { step: 'shortlisted', count: 50 },
    { step: 'applied', count: 5 },
    { step: 'interview', count: 1 },
    { step: 'offer', count: 0 },
  ],
  generated_at: '2026-10-12T09:30:00Z',
}

export const HEALTH: Health = {
  generated_at: '2026-10-12T09:30:00Z',
  paused: { paused: false, paused_at: null, reason: null },
  timezone: 'Asia/Kolkata',
  schedules: [
    { name: 'collect', enabled: true, every_minutes: 360, at_time: null, batch: 18, model: null,
      last_run_at: '2026-10-12T06:00:00Z', next_run_at: '2026-10-12T12:00:00Z', overdue: false, feature_off: false },
    { name: 'score', enabled: true, every_minutes: 30, at_time: null, batch: 25, model: 'claude-haiku-5-5',
      last_run_at: '2026-10-12T09:10:00Z', next_run_at: '2026-10-12T09:40:00Z', overdue: false, feature_off: false },
    { name: 'tailor', enabled: false, every_minutes: 15, at_time: null, batch: 3, model: null,
      last_run_at: null, next_run_at: null, overdue: false, feature_off: false },
    { name: 'digest', enabled: true, every_minutes: null, at_time: '08:00:00', batch: 10, model: null,
      last_run_at: '2026-10-12T02:30:00Z', next_run_at: '2026-10-13T02:30:00Z', overdue: false, feature_off: false },
  ],
  queue: { waiting_to_score: 0, waiting_over_3h: 0, last_scored_at: '2026-10-12T09:10:00Z' },
  parked: { total: 0, items: [] },
  sources: { tracked: 31, failing: 0, items: [] },
  alerts: [],
}

export const SPEND: Spend = {
  generated_at: '2026-10-12T09:30:00Z',
  timezone: 'Asia/Kolkata',
  claude: { month_spent: 0.53, day_spent: 0.02, monthly_cap: 10, daily_cap: 1.5, month_remaining: 9.47, day_remaining: 1.48,
            warn: false, warn_pct: 80, blocked: false, blocked_reason: null, month_start: '2026-09-30T18:30:00Z' },
  apify: { month_spent: 0.53, day_spent: 0, monthly_cap: 15, daily_cap: 0.5, month_remaining: 14.47, day_remaining: 0.5,
           blocked: false, blocked_reason: null },
  daily: [
    { day: '2026-10-12', provider: 'anthropic', purpose: 'score', calls: 40, cost_usd: 0.0164 },
    { day: '2026-10-11', provider: 'anthropic', purpose: 'score', calls: 600, cost_usd: 0.24 },
    { day: '2026-10-11', provider: 'anthropic', purpose: 'tailor', calls: 4, cost_usd: 0.21 },
    { day: '2026-10-11', provider: 'apify', purpose: 'search', calls: 3, cost_usd: 0.53 },
  ],
  models: [
    { provider: 'anthropic', model: 'claude-haiku-5-5', calls: 640, cost_usd: 0.2564, input_tokens: 900000, output_tokens: 80000 },
    { provider: 'apify', model: '(none)', calls: 3, cost_usd: 0.53, input_tokens: 0, output_tokens: 0 },
  ],
}

export const SETTINGS: SettingsPage = {
  timezone: 'Asia/Kolkata',
  paused: false,
  paused_reason: null,
  schedules: [
    { name: 'collect', enabled: true, every_minutes: 360, at_time: null, batch: 18, model: null, board_batch: 35, run_now: false,
      last_run_at: '2026-10-12T06:00:00Z', next_run_at: '2026-10-12T12:00:00Z' },
    { name: 'score', enabled: true, every_minutes: 30, at_time: null, batch: 25, model: 'claude-haiku-5-5', board_batch: null, run_now: false,
      last_run_at: '2026-10-12T09:10:00Z', next_run_at: '2026-10-12T09:40:00Z' },
    { name: 'tailor', enabled: true, every_minutes: 15, at_time: null, batch: 3, model: null, board_batch: null, run_now: false,
      last_run_at: null, next_run_at: null },
    { name: 'digest', enabled: true, every_minutes: null, at_time: '08:00', batch: 10, model: null, board_batch: null, run_now: false,
      last_run_at: '2026-10-12T02:30:00Z', next_run_at: '2026-10-13T02:30:00Z' },
  ],
  features: {
    flags: { tailoring: false, telegram: true, digest: true },
    effective: {
      tailoring: { on: false, reason: 'switched off (jobsearch features set tailoring on)' },
      telegram: { on: true, reason: '' },
      digest: { on: true, reason: '' },
    },
    approval: 'review',
    approval_effective: 'review',
  },
  tailoring: { mode: 'auto', min_score: null, max_score: 80, daily_limit: 5, approval: 'review', cover_letter: true, max_pages: 2, paper: 'a4', model: 'claude-sonnet-5-5' },
  budget: { monthly_cap_usd: 10, daily_cap_usd: 1.5, warn_pct: 80, apify_monthly_cap_usd: 15, apify_daily_cap_usd: 0.5 },
  limits: {
    min_every: { collect: 15, score: 5, tailor: 5, digest: 60 }, max_every: 10080,
    max_batch: { collect: 50, score: 50, tailor: 10, digest: 50 }, max_board_batch: 200,
  },
}

export const SEARCH: SearchPage = {
  version: '2026-10-12T09:30:00.123456+00:00',
  edited_by: 'cli',
  profile: {
    search_terms: ['backend engineer', 'python developer'], locations: ['Pune', 'Remote'], sources: ['indeed', 'greenhouse', 'remotive'],
    allow_remote: true, min_yoe: 0, max_yoe: 4, include_keywords: [], exclude_keywords: ['intern', 'staff'], exclude_companies: ['Acme'],
    max_age_days: 7, score_threshold: 60,
    boards: { greenhouse: [{ slug: 'stripe', name: null }, { slug: 'notion', name: 'Notion' }] }, board_title_words: ['software'],
  },
  registry: {
    sources: ['indeed', 'naukri', 'linkedin', 'linkedin_apify', 'glassdoor', 'ziprecruiter', 'google', 'greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters', 'remotive', 'remoteok', 'weworkremotely', 'himalayas', 'arbeitnow'],
    groups: { jobspy: ['indeed', 'naukri', 'linkedin'], apify: ['linkedin_apify'], boards: ['greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters'], feeds: ['remotive', 'remoteok', 'weworkremotely', 'himalayas', 'arbeitnow'] },
    board_systems: ['greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters'], paid: ['linkedin_apify'],
    descriptions: {
      indeed: 'Indeed job search through JobSpy; free, no key needed.', naukri: 'Naukri job search through JobSpy; free, no key needed.',
      linkedin: 'LinkedIn job search through JobSpy; free, no key needed, but it can be rate limited.',
      linkedin_apify: 'LinkedIn job search through Apify; it uses your Apify credit.', glassdoor: 'Glassdoor job search through JobSpy.',
      ziprecruiter: 'ZipRecruiter job search through JobSpy.', google: 'Google Jobs search through JobSpy.',
      greenhouse: 'Jobs from company boards hosted on Greenhouse; needs the company slugs you list.',
      lever: 'Jobs from company boards hosted on Lever; needs the company slugs you list.',
      ashby: 'Jobs from company boards hosted on Ashby; needs the company slugs you list.',
      workable: 'Jobs from company boards hosted on Workable; needs the company slugs you list.',
      smartrecruiters: 'Jobs from company boards hosted on SmartRecruiters; needs the company slugs you list.',
      remotive: "Remotive's public remote-jobs feed; needs nothing.", remoteok: "Remote OK's public remote-jobs feed; needs nothing.",
      weworkremotely: "We Work Remotely's public remote-jobs feed; needs nothing.", himalayas: "Himalayas' public remote-jobs feed; needs nothing.",
      arbeitnow: "Arbeitnow's public jobs feed; needs nothing.",
    },
    limits: { max_board_slugs: 300, max_title_words: 50, max_board_name: 100 },
  },
}

export function boardCard(id: number, overrides: Partial<Board['columns'][number]['items'][number]> = {}): Board['columns'][number]['items'][number] {
  return {
    id, title: `Engineer ${id}`, company: `Company ${id}`, location: 'Pune', is_remote: false, score: 80, pipeline_status: 'shortlisted',
    applied_at: null, deadline: null, closing_soon: false, has_resume: false, has_cover_letter: false, needs_attention: false,
    follow_up: null, status_since: '2026-10-10T10:00:00Z', ...overrides,
  }
}

export const BOARD: Board = {
  generated_at: '2026-10-12T09:30:00Z',
  columns: [
    { key: 'not_applied', total: 43, items: [boardCard(1), boardCard(2, { pipeline_status: 'drafted', has_resume: true, score: 75 })] },
    { key: 'applied', total: 1, items: [boardCard(3, { pipeline_status: 'applied', applied_at: '2026-10-08T09:00:00Z', status_since: '2026-10-08T09:00:00Z' })] },
    { key: 'in_progress', total: 0, items: [] },
    { key: 'interview', total: 1, items: [boardCard(4, { pipeline_status: 'interview', status_since: '2026-10-11T09:00:00Z' })] },
    { key: 'offer', total: 0, items: [] },
    { key: 'rejected', total: 0, items: [] },
  ],
  today: '2026-10-12',
  follow_up_days: 7,
  follow_ups_total: 2,
  follow_ups: [
    { id: 3, title: 'Engineer 3', company: 'Company 3', location: 'Pune', score: 80, pipeline_status: 'applied', reason: 'waiting', follow_up_on: null,
      applied_on: '2026-10-01', days_waiting: 11, contact_name: 'Priya N', contact_email: 'priya@example.com', referral: null },
    { id: 4, title: 'Engineer 4', company: 'Company 4', location: 'Pune', score: 80, pipeline_status: 'interview', reason: 'date', follow_up_on: '2026-10-10',
      applied_on: null, days_waiting: null, contact_name: null, contact_email: null, referral: 'Sam' },
  ],
  closed: { withdrawn: 2, skipped: 1 },
}

export const TRACKING: Tracking = {
  notes: 'call Priya on Friday', contact_name: 'Priya N', contact_email: 'priya@example.com', referral: 'via Sam',
  follow_up_on: '2026-10-16', version: '2026-10-12T09:30:00.123456Z', today: '2026-10-12',
}

export const NO_TRACKING: Tracking = { notes: null, contact_name: null, contact_email: null, referral: null, follow_up_on: null, version: null, today: '2026-10-12' }

export const STATS: Stats = {
  generated_at: '2026-10-12T09:30:00Z', timezone: 'Asia/Kolkata', applied: 8, responded: 3, reached_interview: 2, reached_offer: 1,
  avg_days_to_reply: 6.5, replies_measured: 2,
  weekly: Array.from({ length: 12 }, (_, i) => ({ week_start: `2026-08-${String(10 + i).padStart(2, '0')}`, found: 20 + i, shortlisted: 3, applied: i === 11 ? 2 : 0 })),
}

export const EVENTS: JobEvent[] = [
  { id: 1, from_status: null, to_status: 'drafted', note: null, source: null, at: '2026-10-09T09:00:00Z', undoable: false },
  { id: 2, from_status: 'drafted', to_status: 'applied', note: 'sent my CV', source: 'dashboard', at: '2026-10-10T09:00:00Z', undoable: true },
]

/** A row shaped like the API's JobRow. */
export function jobRow(id: number, overrides: Partial<JobRow> = {}): JobRow {
  return {
    id, title: `Engineer ${id}`, company: `Company ${id}`, location: 'Pune', is_remote: false, salary_min: null,
    salary_max: null, salary_currency: null, score: 70, verdict: 'good fit', scores: { technical: 72, seniority: 65 },
    scam_risk: 'low', job_status: 'shortlisted', filter_reason: null, posted_at: '2026-10-10T09:30:00Z',
    first_seen: '2026-10-10T10:00:00Z', last_seen: '2026-10-12T04:20:00Z', deadline: null, closing_soon: false,
    application_status: null, applied_at: null, needs_attention: false, has_resume: false, has_cover_letter: false,
    pipeline_status: 'shortlisted', sources: ['greenhouse'], apply_url: `https://boards.greenhouse.io/c${id}/jobs/${id}`,
    ...overrides,
  }
}

/** A job shaped like the API's JobDetail: the row plus the details panel's fields. */
export function jobDetail(id: number, overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    ...jobRow(id),
    description: 'About the role.\nYou will build data pipelines.', canonical_url: `https://boards.greenhouse.io/c${id}/jobs/${id}`,
    score_reason: 'Strong Python and SQL match.', strengths: ['Python'], gaps: ['No Kafka'],
    gates: { eligibility: { verdict: 'PASS', note: '' } }, yoe_min: 3, yoe_max: 5, scam_score: 5, scam_flags: [],
    posting_language: 'English', variant: null, notes: null, review_note: null, reviewed_at: null,
    attention_reason: null, cost_usd: null, model: null,
    sources_detail: [{
      source: 'greenhouse', url: `https://boards.greenhouse.io/c${id}/jobs/${id}`, apply_url: null,
      first_seen: '2026-10-10T10:00:00Z', last_seen: '2026-10-12T04:20:00Z',
    }],
    ...overrides,
  }
}

export function jobPage(items: JobRow[], total = items.length, page = 1, page_size = 50): JobPage {
  return { items, total, page, page_size }
}

/** The API as a logged-in user sees it; tests override single routes with server.use(...). */
export const server = setupServer(
  http.get('/api/me', () => HttpResponse.json(ME)),
  http.get('/api/summary', () => HttpResponse.json(SUMMARY)),
  http.get('/api/pipeline', () => HttpResponse.json(HEALTH)),
  http.get('/api/spend', () => HttpResponse.json(SPEND)),
  http.get('/api/settings', () => HttpResponse.json(SETTINGS)),
  http.get('/api/search', () => HttpResponse.json(SEARCH)),
  http.get('/api/jobs', () => HttpResponse.json(jobPage([jobRow(1), jobRow(2)]))),
  http.get('/api/board', () => HttpResponse.json(BOARD)),
  http.get('/api/stats', () => HttpResponse.json(STATS)),
  http.get('/api/jobs/:id/events', () => HttpResponse.json(EVENTS)),
  http.get('/api/jobs/:id/tracking', () => HttpResponse.json(NO_TRACKING)),
  http.get('/api/jobs/:id/tailor', () => HttpResponse.json({ state: 'idle' })),
  http.get('/api/jobs/:id', ({ params }) => HttpResponse.json(jobDetail(Number(params.id)))),
)

export const unauthorized = () => HttpResponse.json({ detail: 'not logged in' }, { status: 401 })
