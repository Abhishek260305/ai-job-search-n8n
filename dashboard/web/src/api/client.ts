// The one way the app talks to the API. Same-origin only: the page and the API come from the same server, through
// the SSH tunnel, and the session is an HttpOnly cookie the browser sends by itself.

const WRITE_HEADER = { 'X-Requested-With': 'jobsearch-dashboard' }
const READ_METHODS = new Set(['GET', 'HEAD'])
// A wrong password is a 401 too, and must not send anyone to the login page.
const NO_REDIRECT = new Set(['/api/login', '/api/logout'])

export class ApiError extends Error {
  readonly status: number
  readonly detail: string
  readonly currentStatus?: string
  readonly retryAfter?: number

  constructor(status: number, detail: string, extra: { currentStatus?: string; retryAfter?: number } = {}) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.currentStatus = extra.currentStatus
    this.retryAfter = extra.retryAfter
  }
}

let onUnauthorized: () => void = () => {}

/** Called when the session is missing or expired (any 401 except from login/logout). */
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler
}

function messageOf(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    // FastAPI's validation errors: [{loc: [...], msg: "..."}]
    const msgs = detail.map((d) => (d as { msg?: unknown })?.msg).filter((m): m is string => typeof m === 'string')
    if (msgs.length) return msgs.join('; ')
  }
  return status >= 500 ? 'The dashboard API had a problem.' : `Request failed (${status}).`
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init
  const method = (rest.method ?? 'GET').toUpperCase()
  const finalHeaders = new Headers(headers)
  if (!READ_METHODS.has(method)) {
    for (const [name, value] of Object.entries(WRITE_HEADER)) finalHeaders.set(name, value)
  }
  if (json !== undefined) finalHeaders.set('Content-Type', 'application/json')

  let response: Response
  try {
    response = await fetch(path, {
      ...rest,
      method,
      headers: finalHeaders,
      body: json !== undefined ? JSON.stringify(json) : rest.body,
      credentials: 'same-origin',
    })
  } catch {
    throw new ApiError(0, 'Cannot reach the dashboard. Is the SSH tunnel open, or Tailscale connected?')
  }

  if (response.status === 204) return undefined as T
  const isJson = response.headers.get('content-type')?.includes('application/json')
  const body: unknown = isJson ? await response.json().catch(() => null) : null

  if (!response.ok) {
    if (response.status === 401 && !NO_REDIRECT.has(path)) onUnauthorized()
    const retryAfter = Number(response.headers.get('Retry-After')) || undefined
    const currentStatus = (body as { current_status?: unknown } | null)?.current_status
    throw new ApiError(response.status, messageOf(body, response.status), {
      currentStatus: typeof currentStatus === 'string' ? currentStatus : undefined,
      retryAfter,
    })
  }
  return body as T
}

/** The query string for list parameters as FastAPI reads them: repeated keys for arrays, empty values left out. */
export function toQuery(params: Record<string, unknown>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)))
    else search.append(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}
