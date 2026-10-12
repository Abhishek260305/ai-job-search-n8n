import { describe, expect, it } from 'vitest'
import openapi from '@/api/openapi.json'
import { DEFAULT_VIEW, exportHref, PAGE_SIZES, parseView, SORT_KEYS, SOURCES, TABS, viewSearch } from './jobsParams'

const params = openapi.paths['/api/jobs'].get.parameters
const enumOf = (name: string): unknown[] => {
  const schema = params.find((p) => p.name === name)!.schema as { enum?: unknown[]; items?: { enum?: unknown[] }; default?: unknown }
  return schema.enum ?? schema.items?.enum ?? []
}
const defaultOf = (name: string): unknown => (params.find((p) => p.name === name)!.schema as { default?: unknown }).default

const parse = (query: string) => parseView(new URLSearchParams(query))

describe('the allowed values', () => {
  it('match what the API accepts', () => {
    expect([...TABS].sort()).toEqual([...enumOf('tab')].sort())
    expect([...SORT_KEYS].sort()).toEqual([...enumOf('sort')].sort())
    expect([...SOURCES].sort()).toEqual([...enumOf('source')].sort())
  })

  it('use the API defaults', () => {
    expect(DEFAULT_VIEW.tab).toBe(defaultOf('tab'))
    expect(DEFAULT_VIEW.sort).toBe(defaultOf('sort'))
    expect(DEFAULT_VIEW.order).toBe(defaultOf('order'))
    expect(DEFAULT_VIEW.page).toBe(defaultOf('page'))
    expect(DEFAULT_VIEW.page_size).toBe(defaultOf('page_size'))
    expect(PAGE_SIZES).toContain(DEFAULT_VIEW.page_size)
  })
})

describe('parseView', () => {
  it('gives the defaults for an empty query', () => {
    expect(parse('')).toEqual(DEFAULT_VIEW)
  })

  it('reads every parameter', () => {
    expect(parse('tab=applied&q=acme&status=interview&status=offer&source=lever&source=ashby&min_score=70&remote=true&posted_days=14&sort=company&order=asc&page=3&page_size=100'))
      .toEqual({
        tab: 'applied', q: 'acme', status: ['interview', 'offer'], source: ['lever', 'ashby'], min_score: 70,
        remote: true, posted_days: 14, sort: 'company', order: 'asc', page: 3, page_size: 100,
      })
  })

  it('drops what the API would refuse instead of sending it', () => {
    expect(parse('tab=nope&status=bogus&status=offer&source=monster&min_score=101&posted_days=0&sort=salary&order=up&page=0&page_size=30&remote=1'))
      .toEqual({ ...DEFAULT_VIEW, status: ['offer'] })
    expect(parse('min_score=-1&page=1e3&posted_days=7.5')).toEqual(DEFAULT_VIEW)
    expect(parse('min_score=0').min_score).toBe(0)
  })

  it('trims the search and caps it at the API limit', () => {
    expect(parse('q=%20%20').q).toBeUndefined()
    expect(parse(`q=${'x'.repeat(150)}`).q).toHaveLength(100)
  })

  it('keeps each list value once', () => {
    expect(parse('status=offer&status=offer').status).toEqual(['offer'])
  })
})

describe('viewSearch', () => {
  it('leaves the defaults out', () => {
    expect(viewSearch(DEFAULT_VIEW).toString()).toBe('')
  })

  it('repeats keys for lists and adds the open job last', () => {
    const view = { ...DEFAULT_VIEW, tab: 'applied' as const, status: ['interview' as const, 'offer' as const], page: 2 }
    expect(viewSearch(view, 42).toString()).toBe('tab=applied&status=interview&status=offer&page=2&job=42')
  })

  it('survives a round trip', () => {
    const query = 'tab=all&q=data+engineer&status=rejected&source=arbeitnow&min_score=0&remote=true&posted_days=30&sort=title&order=asc&page=9&page_size=25'
    expect(viewSearch(parse(query)).toString()).toBe(query)
  })
})

describe('exportHref', () => {
  it('carries the filters and sort but not the page', () => {
    const view = parse('tab=all&q=acme&source=lever&sort=company&order=asc&page=3&page_size=100')
    expect(exportHref(view)).toBe('/api/export.csv?tab=all&q=acme&source=lever&sort=company&order=asc')
  })

  it('is the bare export for the default view', () => {
    expect(exportHref(DEFAULT_VIEW)).toBe('/api/export.csv')
  })

  it('encodes the search text', () => {
    expect(exportHref(parse('q=R%26D%20%2B%20ML'))).toBe('/api/export.csv?q=R%26D+%2B+ML')
  })
})
