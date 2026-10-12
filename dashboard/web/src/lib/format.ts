// Formatting shared by every screen. Posting data comes from the internet: links go through safeUrl().

export const DASH = '—'

const numberCache = new Map<string, Intl.NumberFormat>()

/**
 * One compact amount: ₹12.5L, ₹1.2Cr, $120K, €55K, SGD 3K. Indian grouping (lakh, crore) for rupees. The currency
 * symbol when there is one, its code otherwise (Intl takes any three letters as a code), and the text as stored after
 * the number when it is not shaped like a code at all.
 */
function compact(value: number, currency: string | null): string {
  const code = currency?.trim().toUpperCase() || null
  const key = code ?? ''
  let format = numberCache.get(key)
  if (!format) {
    try {
      format = new Intl.NumberFormat(code === 'INR' ? 'en-IN' : 'en-US', {
        notation: 'compact',
        // with a currency, Intl defaults to two minimum decimals ("₹12.0L"); a whole amount should read "₹12L"
        minimumFractionDigits: 0,
        maximumFractionDigits: 1,
        ...(code ? { style: 'currency', currency: code } : {}),
      })
    } catch {
      // not three letters (for example "₹" or "Rs."): the number, then the text as stored
      return `${compact(value, null)} ${currency!.trim()}`
    }
    numberCache.set(key, format)
  }
  return format.format(value)
}

const countFormat = new Intl.NumberFormat('en-US')

/** "1,234"; 0 stays "0" (a card never shows a blank). */
export function formatCount(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? countFormat.format(value) : DASH
}

function amount(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

/**
 * The salary range of a posting, or "—". Never adds "/year": postings say per year, per month or per hour, and the
 * period is not stored (the live data has both USD 60 and USD 500,000).
 */
export function formatSalary(min: number | null | undefined, max: number | null | undefined, currency?: string | null): string {
  let low = amount(min)
  let high = amount(max)
  if (low === null && high === null) return DASH
  if (low !== null && high !== null && low > high) [low, high] = [high, low]
  const cur = currency ?? null
  if (low !== null && high !== null) {
    return low === high ? compact(low, cur) : `${compact(low, cur)} – ${compact(high, cur)}`
  }
  return low !== null ? `from ${compact(low, cur)}` : `up to ${compact(high!, cur)}`
}

function toDate(value: string | Date | null | undefined): Date | null {
  if (!value) return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

const UNITS: [limit: number, size: number, suffix: string][] = [
  [60, 1, 's'],
  [3600, 60, 'm'],
  [86_400, 3600, 'h'],
  [30 * 86_400, 86_400, 'd'],
  [365 * 86_400, 30 * 86_400, 'mo'],
  [Infinity, 365 * 86_400, 'y'],
]

/** "3d ago", "5h ago", "in 2d" (deadlines), "just now"; "—" for no date. Pair it with formatDateTime in a tooltip. */
export function formatRelative(value: string | Date | null | undefined, now: Date = new Date()): string {
  const date = toDate(value)
  if (!date) return DASH
  const seconds = Math.round((now.getTime() - date.getTime()) / 1000)
  const abs = Math.abs(seconds)
  if (abs < 60) return 'just now'
  const [, size, suffix] = UNITS.find(([limit]) => abs < limit)!
  const count = Math.floor(abs / size)
  return seconds >= 0 ? `${count}${suffix} ago` : `in ${count}${suffix}`
}

const dateTime = new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'short' })
const dateOnly = new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium' })

/** "12 Oct 2026, 09:30" in the browser's time zone. */
export function formatDateTime(value: string | Date | null | undefined): string {
  const date = toDate(value)
  return date ? dateTime.format(date) : DASH
}

/** "12 Oct 2026". A plain `YYYY-MM-DD` (a deadline) is that calendar day, not midnight UTC shifted. */
export function formatDate(value: string | Date | null | undefined): string {
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [y, m, d] = value.split('-').map(Number)
    return dateOnly.format(new Date(y, m - 1, d))
  }
  const date = toDate(value)
  return date ? dateOnly.format(date) : DASH
}

/** The link itself when it is an absolute http(s) URL, otherwise null. Never put a posting link in href without it. */
export function safeUrl(url: string | null | undefined): string | null {
  if (!url || typeof url !== 'string') return null
  try {
    const parsed = new URL(url.trim())
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

/** "boards.greenhouse.io" for a link, without "www."; null when the link is not safe. */
export function domainOf(url: string | null | undefined): string | null {
  const safe = safeUrl(url)
  return safe ? new URL(safe).hostname.replace(/^www\./, '') : null
}
