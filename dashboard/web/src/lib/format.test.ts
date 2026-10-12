import { describe, expect, it } from 'vitest'
import { domainOf, formatCount, formatDate, formatDateTime, formatRelative, formatSalary, safeUrl } from './format'

describe('formatSalary', () => {
  it('shows a range in compact form, lakh and crore for rupees', () => {
    expect(formatSalary(1_200_000, 1_800_000, 'INR')).toBe('₹12L – ₹18L')
    expect(formatSalary(12_500_000, 20_000_000, 'INR')).toBe('₹1.3Cr – ₹2Cr')
    expect(formatSalary(120_000, 150_000, 'USD')).toBe('$120K – $150K')
  })

  it('handles one side only', () => {
    expect(formatSalary(120_000, null, 'USD')).toBe('from $120K')
    expect(formatSalary(null, 150_000, 'USD')).toBe('up to $150K')
    expect(formatSalary(undefined, 90_000, 'EUR')).toBe('up to €90K')
  })

  it('shows "—" when neither side is known', () => {
    expect(formatSalary(null, null, 'USD')).toBe('—')
    expect(formatSalary(undefined, undefined)).toBe('—')
    expect(formatSalary(Number.NaN, Number.POSITIVE_INFINITY, 'USD')).toBe('—')
    expect(formatSalary(-5, null, 'USD')).toBe('—')
  })

  it('shows one number when both sides are equal, and fixes a reversed range', () => {
    expect(formatSalary(100_000, 100_000, 'GBP')).toBe('£100K')
    expect(formatSalary(150_000, 120_000, 'USD')).toBe('$120K – $150K')
  })

  it('uses the code, glued to the number, when a currency has no symbol of its own', () => {
    expect(formatSalary(3000, 6000, 'SGD')).toBe('SGD\u00a03K – SGD\u00a06K')
    expect(formatSalary(45_000, null, 'AED')).toBe('from AED\u00a045K')
    expect(formatSalary(70_000, 90_000, 'CAD')).toBe('CA$70K – CA$90K')
    expect(formatSalary(50_000, null, 'LPA')).toBe('from LPA\u00a050K')
  })

  it('accepts a lower-case or padded code', () => {
    expect(formatSalary(100_000, null, ' inr ')).toBe('from ₹1L')
  })

  it('keeps text that is not a currency code after the number', () => {
    expect(formatSalary(5, null, '₹')).toBe('from 5 ₹')
    expect(formatSalary(50_000, 80_000, 'Rs.')).toBe('50K Rs. – 80K Rs.')
  })

  it('shows a plain number when the currency is missing', () => {
    expect(formatSalary(80_000, 200_000, null)).toBe('80K – 200K')
    expect(formatSalary(80_000, 200_000, '')).toBe('80K – 200K')
  })

  it('never claims a period', () => {
    expect(formatSalary(60, 80, 'USD')).toBe('$60 – $80')
    expect(formatSalary(60, 80, 'USD')).not.toMatch(/year|month|hour|\//i)
  })
})

describe('formatCount', () => {
  it('groups thousands and keeps zero', () => {
    expect(formatCount(0)).toBe('0')
    expect(formatCount(7)).toBe('7')
    expect(formatCount(123_456)).toBe('123,456')
    expect(formatCount(null)).toBe('—')
    expect(formatCount(Number.NaN)).toBe('—')
  })
})

describe('formatRelative', () => {
  const now = new Date('2026-10-12T12:00:00Z')

  it.each([
    ['2026-10-12T11:59:30Z', 'just now'],
    ['2026-10-12T11:55:00Z', '5m ago'],
    ['2026-10-12T09:00:00Z', '3h ago'],
    ['2026-10-12T10:30:00Z', '1h ago'],
    ['2026-10-09T12:00:00Z', '3d ago'],
    ['2026-10-08T22:00:00Z', '3d ago'],
    ['2026-08-10T12:00:00Z', '2mo ago'],
    ['2025-10-01T12:00:00Z', '1y ago'],
    ['2026-10-15T12:00:00Z', 'in 3d'],
  ])('%s -> %s', (when, expected) => {
    expect(formatRelative(when, now)).toBe(expected)
  })

  it('accepts a Date and shows "—" for nothing or garbage', () => {
    expect(formatRelative(new Date('2026-10-12T10:00:00Z'), now)).toBe('2h ago')
    expect(formatRelative(null, now)).toBe('—')
    expect(formatRelative('not a date', now)).toBe('—')
  })
})

describe('formatDateTime and formatDate', () => {
  it('shows the absolute date for tooltips', () => {
    expect(formatDateTime('2026-10-12T09:30:00')).toBe('12 Oct 2026, 09:30')
    expect(formatDateTime(null)).toBe('—')
    expect(formatDate('2026-10-12T09:30:00')).toBe('12 Oct 2026')
  })

  it('keeps a plain calendar date on its own day', () => {
    expect(formatDate('2026-10-31')).toBe('31 Oct 2026')
    expect(formatDate('garbage')).toBe('—')
    expect(formatDate(undefined)).toBe('—')
  })
})

describe('safeUrl', () => {
  it('keeps absolute http and https links', () => {
    expect(safeUrl('https://boards.greenhouse.io/acme/jobs/1')).toBe('https://boards.greenhouse.io/acme/jobs/1')
    expect(safeUrl('http://example.com')).toBe('http://example.com/')
    expect(safeUrl('  https://example.com/a?b=1  ')).toBe('https://example.com/a?b=1')
  })

  it.each([
    'javascript:alert(1)',
    'JavaScript:alert(1)',
    ' javascript:alert(1)',
    'java\tscript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    'mailto:hr@example.com',
    '/jobs/1',
    '//evil.example/x',
    'example.com/jobs',
    'not a url',
    '',
    null,
    undefined,
  ])('refuses %j', (url) => {
    expect(safeUrl(url)).toBeNull()
  })
})

describe('domainOf', () => {
  it('gives the host without www.', () => {
    expect(domainOf('https://www.linkedin.com/jobs/view/1')).toBe('linkedin.com')
    expect(domainOf('https://jobs.lever.co/acme/1')).toBe('jobs.lever.co')
  })

  it('is null for unsafe or broken links', () => {
    expect(domainOf('javascript:alert(1)')).toBeNull()
    expect(domainOf('/relative')).toBeNull()
    expect(domainOf(null)).toBeNull()
  })
})
