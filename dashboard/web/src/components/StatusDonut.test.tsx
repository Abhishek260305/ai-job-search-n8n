import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { Summary } from '@/api/types'
import { SUMMARY } from '@/test/server'
import StatusDonut from './StatusDonut'

const NONE: Summary['by_status'] = { applied: 0, in_progress: 0, interview: 0, offer: 0, rejected: 0, withdrawn: 0, not_applied: 0 }
const EMPTY_TEXT = 'No applications yet. Mark a job as applied from the table below.'

const sectors = (container: HTMLElement) => [...container.querySelectorAll('.recharts-pie-sector path')]
const legendRows = () => within(screen.getByRole('list', { name: 'Legend' })).getAllByRole('listitem')
const toggle = () => screen.getByRole('checkbox', { name: 'Include not applied' })

describe('StatusDonut', () => {
  it('draws one slice per non-zero status, in their colours, with the total in the centre', () => {
    const { container } = render(<StatusDonut byStatus={SUMMARY.by_status} />)
    // in progress 2, interview 1, rejected 2
    expect(sectors(container).map((p) => p.getAttribute('fill'))).toEqual(['#56B4E9', '#E69F00', '#D55E00'])
    expect(screen.getByRole('img')).toHaveTextContent(/^5submitted$/)
    expect(screen.queryByText(EMPTY_TEXT)).not.toBeInTheDocument()
  })

  it('lists every submitted status with its count in the legend, zeros too', () => {
    render(<StatusDonut byStatus={SUMMARY.by_status} />)
    expect(legendRows().map((li) => li.textContent)).toEqual([
      'Applied0', 'In progress2', 'Interview1', 'Offer0', 'Rejected2', 'Withdrawn0',
    ])
  })

  it('states the numbers for a screen reader', () => {
    render(<StatusDonut byStatus={SUMMARY.by_status} />)
    expect(screen.getByRole('img', {
      name: 'Applications by status: Applied 0, In progress 2, Interview 1, Offer 0, Rejected 2, Withdrawn 0. 5 in total.',
    })).toBeInTheDocument()
  })

  it('adds the shortlist slice with the toggle, and takes it away again', async () => {
    const { container } = render(<StatusDonut byStatus={SUMMARY.by_status} />)
    expect(toggle()).not.toBeChecked()
    await userEvent.click(toggle())
    expect(sectors(container)).toHaveLength(4)
    expect(sectors(container).at(-1)).toHaveAttribute('fill', '#CC79A7')
    expect(legendRows().at(-1)).toHaveTextContent('Not applied43')
    expect(screen.getByRole('img')).toHaveTextContent(/^48in total$/)
    expect(screen.getByRole('img').getAttribute('aria-label')).toMatch(/Not applied 43\. 48 in total\.$/)
    await userEvent.click(toggle())
    expect(sectors(container)).toHaveLength(3)
    expect(legendRows()).toHaveLength(6)
  })

  it('says so instead of drawing an empty ring', () => {
    const { container } = render(<StatusDonut byStatus={{ ...NONE, not_applied: 43 }} />)
    expect(screen.getByText(EMPTY_TEXT)).toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(sectors(container)).toHaveLength(0)
  })

  it('shows the shortlist alone when the toggle is on and nothing was submitted', async () => {
    const { container } = render(<StatusDonut byStatus={{ ...NONE, not_applied: 43 }} />)
    await userEvent.click(toggle())
    expect(screen.queryByText(EMPTY_TEXT)).not.toBeInTheDocument()
    expect(sectors(container)).toHaveLength(1)
    expect(screen.getByRole('img')).toHaveTextContent(/^43in total$/)
  })

  it('keeps the empty message when there is nothing at all', async () => {
    render(<StatusDonut byStatus={NONE} />)
    await userEvent.click(toggle())
    expect(screen.getByText(EMPTY_TEXT)).toBeInTheDocument()
  })
})
