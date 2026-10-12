import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { createQueryClient } from '@/lib/queryClient'
import { renderApp } from '@/test/render'

describe('the app shell', () => {
  it('shows the overview at /', async () => {
    renderApp('/')
    expect(await screen.findByRole('heading', { name: 'Job search' })).toBeInTheDocument()
  })

  it('shows a not-found page for an unknown path, with a way back', async () => {
    const { router } = renderApp('/no/such/page')
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('link', { name: 'Back to the overview' }))
    expect(router.state.location.pathname).toBe('/')
    expect(await screen.findByRole('heading', { name: 'Job search' })).toBeInTheDocument()
  })
})

describe('the query client', () => {
  it('keeps data fresh for 30 seconds, retries once and refetches on focus', () => {
    const queries = createQueryClient().getDefaultOptions().queries
    expect(queries).toMatchObject({ staleTime: 30_000, retry: 1, refetchOnWindowFocus: true })
  })

  it('never retries a change', () => {
    expect(createQueryClient().getDefaultOptions().mutations?.retry).toBe(false)
  })
})
