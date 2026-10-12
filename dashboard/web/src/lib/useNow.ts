import { useEffect, useState } from 'react'

/** The current time, re-read every `everyMs`, so "3m ago" keeps counting while the page stays open. */
export function useNow(everyMs = 30_000): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = setInterval(() => setNow(new Date()), everyMs)
    return () => clearInterval(timer)
  }, [everyMs])
  return now
}
