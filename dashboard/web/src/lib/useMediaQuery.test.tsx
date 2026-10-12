import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useMediaQuery } from './useMediaQuery'

function Probe() {
  return <p>{useMediaQuery('(max-width: 767px)') ? 'narrow' : 'wide'}</p>
}

function fakeMatchMedia(initial: boolean) {
  let matches = initial
  const listeners = new Set<() => void>()
  const list = {
    get matches() { return matches },
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  }
  vi.stubGlobal('matchMedia', () => list)
  return { set(value: boolean) { matches = value; listeners.forEach((fn) => fn()) } }
}

afterEach(() => vi.unstubAllGlobals())

describe('useMediaQuery', () => {
  it('is false where the browser cannot answer, which is the desktop layout', () => {
    vi.stubGlobal('matchMedia', undefined)
    render(<Probe />)
    expect(screen.getByText('wide')).toBeInTheDocument()
  })

  it('follows the query as the window changes', () => {
    const media = fakeMatchMedia(true)
    render(<Probe />)
    expect(screen.getByText('narrow')).toBeInTheDocument()
    act(() => media.set(false))
    expect(screen.getByText('wide')).toBeInTheDocument()
  })
})
