/**
 * Where to go after logging in. Only a path on this site is accepted, so a crafted link such as
 * /login?next=https://evil.example cannot send you to another site after you type your password.
 */
export function safeNext(next: string | null | undefined, origin = window.location.origin): string {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return '/'
  try {
    const url = new URL(next, origin)
    if (url.origin !== origin || url.pathname === '/login') return '/'
    return url.pathname + url.search + url.hash
  } catch {
    return '/'
  }
}

/** The login page's address for coming back to `path` afterwards. */
export function loginPath(path: string): string {
  const back = safeNext(path)
  return back === '/' ? '/login' : `/login?next=${encodeURIComponent(back)}`
}
