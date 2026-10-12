import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SUMMARY } from '@/test/server'
import Funnel from './Funnel'

describe('Funnel', () => {
  it('states every step with its share of the one before', () => {
    render(<Funnel funnel={SUMMARY.funnel} threshold={60} />)
    expect(screen.getByRole('img', {
      name: 'Where jobs drop out, each step as a share of the one before: Found 1,234, Passed filters 600 (49%), '
        + 'Scored 590 (98%), Shortlisted 50 (8%), Applied 5 (10%), Interview 1 (20%), Offer 0 (0%).',
    })).toBeInTheDocument()
  })

  it('shows "—" after an empty step', () => {
    const funnel = SUMMARY.funnel.map((f) => (['applied', 'interview', 'offer'].includes(f.step) ? { ...f, count: 0 } : f))
    render(<Funnel funnel={funnel} threshold={60} />)
    expect(screen.getByRole('img').getAttribute('aria-label')).toMatch(/Applied 0 \(0%\), Interview 0 \(—\), Offer 0 \(—\)\.$/)
  })

  it('says which threshold the shortlist step uses', () => {
    const { rerender } = render(<Funnel funnel={SUMMARY.funnel} threshold={60} />)
    expect(screen.getByText('Shortlisted uses your current threshold (60).')).toBeInTheDocument()
    rerender(<Funnel funnel={SUMMARY.funnel} threshold={null} />)
    expect(screen.getByText('Shortlisted uses your current score threshold.')).toBeInTheDocument()
  })
})
