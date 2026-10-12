import { safeUrl } from '@/lib/format'

export const APP_VERSION = __APP_VERSION__

/** docs/dashboard.md in the repository at `repo` (https://github.com/<you>/<repo>); null when there is no usable address. */
export function docsUrl(repo: string | undefined): string | null {
  const base = repo?.trim().replace(/\/+$/, '')
  return base ? safeUrl(`${base}/blob/main/docs/dashboard.md`) : null
}

/** Whether a key press happens while typing, where "/" is a character and not a shortcut. */
export function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return Boolean(target.closest('input, textarea, select, [contenteditable="true"]'))
}
