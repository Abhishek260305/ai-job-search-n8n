import { describe, expect, it } from 'vitest'
import { loginPath, safeNext } from '@/lib/safeNext'

const ORIGIN = 'http://localhost:8080'

describe('safeNext', () => {
  it.each([
    ['/', '/'],
    ['/jobs', '/jobs'],
    ['/jobs?tab=applied&page=2', '/jobs?tab=applied&page=2'],
    ['/jobs#details', '/jobs#details'],
  ])('keeps the path %s', (next, expected) => {
    expect(safeNext(next, ORIGIN)).toBe(expected)
  })

  it.each([
    null, undefined, '', 'jobs', 'https://evil.example/', 'http://localhost:8080.evil.example/', '//evil.example',
    '/\\evil.example', '\\\\evil.example', '/\t/evil.example', '/\t/evil.example/jobs', '/\n/evil.example/jobs',
    'javascript:alert(1)', ' /jobs', '/login',
    '/login?next=/jobs', 'data:text/html,hi',
  ])('refuses %j and goes to the overview', (next) => {
    expect(safeNext(next, ORIGIN)).toBe('/')
  })
})

describe('loginPath', () => {
  it('carries the page to come back to', () => {
    expect(loginPath('/jobs?tab=applied')).toBe('/login?next=%2Fjobs%3Ftab%3Dapplied')
  })

  it('leaves next out for the overview', () => {
    expect(loginPath('/')).toBe('/login')
  })
})
