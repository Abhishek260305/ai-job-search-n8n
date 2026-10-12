import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useNow } from './useNow'

afterEach(() => vi.useRealTimers())

describe('useNow', () => {
  it('moves on every interval and stops when unmounted', () => {
    vi.useFakeTimers({ now: new Date('2026-10-12T09:00:00Z') })
    const { result, unmount } = renderHook(() => useNow(30_000))
    expect(result.current.toISOString()).toBe('2026-10-12T09:00:00.000Z')
    act(() => { vi.advanceTimersByTime(30_000) })
    expect(result.current.toISOString()).toBe('2026-10-12T09:00:30.000Z')
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})
