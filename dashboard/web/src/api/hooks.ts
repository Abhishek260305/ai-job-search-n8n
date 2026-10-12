import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api, ApiError, toQuery } from '@/api/client'
import type {
  BudgetChange, EditableStatus, FeaturesChange, Health, JobDetail, JobPage, JobsParams, Me, PauseState, PipelineStatus, RunRequested,
  FindResult, ScheduleChange, ScheduleName, SearchPage, SearchProfileIn, SettingsPage, Spend, StatusResult, Summary, TailoringChange,
} from '@/api/types'
import { statusLabel } from '@/lib/status'

export const keys = {
  me: ['me'] as const,
  summary: ['summary'] as const,
  jobs: (params: JobsParams) => ['jobs', params] as const,
  allJobs: ['jobs'] as const,
  job: (id: number) => ['job', id] as const,
  pipeline: ['pipeline'] as const,
  spend: ['spend'] as const,
  settings: ['settings'] as const,
  search: ['search'] as const,
}

export function useMe() {
  return useQuery({ queryKey: keys.me, queryFn: () => api<Me>('/api/me'), retry: false, staleTime: 60_000 })
}

export function useSummary() {
  return useQuery({
    queryKey: keys.summary,
    queryFn: () => api<Summary>('/api/summary'),
    refetchInterval: 5 * 60_000,
  })
}

export function useJobs(params: JobsParams) {
  return useQuery({
    queryKey: keys.jobs(params),
    queryFn: () => api<JobPage>(`/api/jobs${toQuery(params)}`),
    // the previous page stays on screen while the next one loads, instead of an empty table
    placeholderData: keepPreviousData,
  })
}

export function useJob(id: number | null) {
  return useQuery({
    queryKey: keys.job(id ?? 0),
    queryFn: () => api<JobDetail>(`/api/jobs/${id}`),
    enabled: id !== null,
  })
}

export type SetStatusInput = { id: number; status: EditableStatus; expected: PipelineStatus; note?: string }

export function useSetStatus() {
  const client = useQueryClient()
  const refresh = (id: number) =>
    Promise.all([
      client.invalidateQueries({ queryKey: keys.summary }),
      client.invalidateQueries({ queryKey: keys.allJobs }),
      client.invalidateQueries({ queryKey: keys.job(id) }),
    ])
  return useMutation({
    mutationFn: ({ id, status, expected, note }: SetStatusInput) =>
      api<StatusResult>(`/api/jobs/${id}/status`, {
        method: 'PATCH',
        json: { status, expected_status: expected, ...(note?.trim() ? { note: note.trim() } : {}) },
      }),
    // the mutation stays pending until the refetch is done, so the old label never shows as if saved
    onSuccess: async (_result, { id, status }) => {
      await refresh(id)
      toast.success(`Marked ${statusLabel(status)}`)
    },
    onError: async (error, { id }) => {
      if (error instanceof ApiError && error.status === 409) {
        await refresh(id)
        const now = error.currentStatus ? statusLabel(error.currentStatus) : 'something else'
        toast.warning(`Changed elsewhere, now ${now}. Reloaded.`)
      } else if (!(error instanceof ApiError && error.status === 401)) {
        toast.error(error instanceof ApiError ? error.detail : 'The status could not be changed.')
      }
    },
  })
}

export function useLogin() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (password: string) => api<void>('/api/login', { method: 'POST', json: { password } }),
    // whatever was cached belonged to the logged-out state (mostly failed requests)
    onSuccess: () => client.clear(),
  })
}

export function useLogout() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => api<void>('/api/logout', { method: 'POST' }),
    onSettled: () => client.clear(),
  })
}

/** The health panel. It is what you look at when something seems wrong, so it refreshes every minute. */
export function usePipeline() {
  return useQuery({
    queryKey: keys.pipeline,
    queryFn: () => api<Health>('/api/pipeline'),
    refetchInterval: 60_000,
  })
}

export function useSpend() {
  return useQuery({
    queryKey: keys.spend,
    queryFn: () => api<Spend>('/api/spend'),
    refetchInterval: 5 * 60_000,
  })
}

export type PauseInput = { pause: true; reason?: string } | { pause: false }

/** The same switch as `scripts/jobsearch schedule pause` and `resume`. Nothing changes on screen until it is saved. */
export function useSetPause() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: PauseInput) =>
      api<PauseState>(input.pause ? '/api/pipeline/pause' : '/api/pipeline/resume', {
        method: 'POST',
        json: input.pause && input.reason?.trim() ? { reason: input.reason.trim() } : {},
      }),
    onSuccess: async (_state, input) => {
      void client.invalidateQueries({ queryKey: keys.settings })      // the settings page shows the pause too
      await client.invalidateQueries({ queryKey: keys.pipeline })
      toast.success(input.pause ? 'Everything is paused' : 'Resumed: the schedules run again')
    },
    onError: (error) => {
      if (!(error instanceof ApiError && error.status === 401)) {
        toast.error(error instanceof ApiError ? error.detail : 'The change could not be saved.')
      }
    },
  })
}

/** The settings page. It is read from the scraper, so it is loaded on demand and not refreshed by a timer. */
export function useSettings() {
  return useQuery({ queryKey: keys.settings, queryFn: () => api<SettingsPage>('/api/settings') })
}

/**
 * A change to a setting. The server answers with the page as it now is, which replaces what is on screen, and the
 * health and spend pages (which show schedules and caps too) reload next time. A refusal is left on the mutation for
 * the form to show next to its fields: the message is the scraper's own.
 */
function useSettingsChange<V>(send: (value: V) => Promise<SettingsPage>, saved: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: send,
    onSuccess: (page) => {
      client.setQueryData(keys.settings, page)
      void client.invalidateQueries({ queryKey: keys.pipeline })
      void client.invalidateQueries({ queryKey: keys.spend })
      toast.success(saved)
    },
  })
}

const patch = (path: string, json: unknown) => api<SettingsPage>(`/api/settings${path}`, { method: 'PATCH', json })

export function useChangeSchedule(name: ScheduleName) {
  return useSettingsChange((changes: ScheduleChange) => patch(`/schedules/${name}`, changes), 'Schedule saved')
}

export function useChangeFeatures() {
  return useSettingsChange((changes: FeaturesChange) => patch('/features', changes), 'Saved')
}

export function useChangeTailoring() {
  return useSettingsChange((changes: TailoringChange) => patch('/tailoring', changes), 'Tailoring settings saved')
}

export function useLowerBudget() {
  return useSettingsChange((changes: BudgetChange) => patch('/budget', changes), 'Caps lowered')
}

export function useRunSchedule(name: ScheduleName) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => api<RunRequested>(`/api/settings/schedules/${name}/run`, { method: 'POST' }),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: keys.settings })
      toast.success(`Run requested. ${result.note.charAt(0).toUpperCase()}${result.note.slice(1)}.`)
    },
  })
}

/**
 * The search profile, loaded when the page opens. It is edited as a draft on screen, so it is never refetched behind the
 * user's back (coming back to the tab would bring a new version and replace the draft): a change made elsewhere shows up
 * as a conflict when saving, which says so and offers a reload.
 */
export function useSearch() {
  return useQuery({ queryKey: keys.search, queryFn: () => api<SearchPage>('/api/search'), staleTime: Infinity, refetchOnWindowFocus: false })
}

/**
 * Save the whole profile with the version it was read at. The server answers with the profile as it now is (a new
 * version), which replaces what is cached; a refusal (the scraper's checks, or 409 when it changed elsewhere) stays on
 * the mutation for the page to show.
 */
export function useSaveSearch() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: { profile: SearchProfileIn; version: string }) => api<SearchPage>('/api/search', { method: 'PUT', json: input }),
    onSuccess: (page) => {
      client.setQueryData(keys.search, page)
      void client.invalidateQueries({ queryKey: keys.summary })        // the funnel and cards use the threshold
      void client.invalidateQueries({ queryKey: keys.settings })       // the tailoring card names it too
      toast.success('Search profile saved')
    },
  })
}

export function useFindBoard() {
  return useMutation({ mutationFn: (name: string) => api<FindResult>('/api/search/boards/find', { method: 'POST', json: { name } }) })
}

