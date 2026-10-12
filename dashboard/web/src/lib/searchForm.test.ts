import { describe, expect, it } from 'vitest'
import type { SearchProfile } from '@/api/types'
import {
  addCompany, companyCount, hints, joinLines, optionalInt, problemsOf, removeCompany, sameProfile, sourceGroups, splitLines, withSource,
  type Registry,
} from './searchForm'

const registry: Registry = {
  sources: ['indeed', 'naukri', 'linkedin', 'linkedin_apify', 'glassdoor', 'ziprecruiter', 'google', 'greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters', 'remotive', 'remoteok'],
  groups: { jobspy: ['indeed', 'naukri', 'linkedin'], apify: ['linkedin_apify'], boards: ['greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters'], feeds: ['remotive', 'remoteok'] },
  board_systems: ['greenhouse', 'lever', 'ashby', 'workable', 'smartrecruiters'], paid: ['linkedin_apify'], descriptions: {},
  limits: { max_board_slugs: 300, max_title_words: 50, max_board_name: 100 },
}

const profile = (over: Partial<SearchProfile> = {}): SearchProfile => ({
  search_terms: ['backend engineer'], locations: ['Pune'], sources: ['indeed'], allow_remote: true, min_yoe: 0, max_yoe: 4, include_keywords: [],
  exclude_keywords: [], exclude_companies: [], max_age_days: 7, score_threshold: 60, boards: {}, board_title_words: [], ...over,
})

describe('lines', () => {
  it('trims, drops empty lines and repeats, and keeps the first spelling', () => {
    expect(splitLines('  backend engineer \n\nSRE\r\nsre\n  python developer')).toEqual(['backend engineer', 'SRE', 'python developer'])
    expect(splitLines('')).toEqual([])
    expect(joinLines(['a', 'b'])).toBe('a\nb')
  })

  it('reads an optional whole number', () => {
    expect(optionalInt('')).toBeNull()
    expect(optionalInt(' 4 ')).toBe(4)
    for (const bad of ['x', '-1', '2.5', '12345']) expect(optionalInt(bad)).toBeNaN()
  })
})

describe('sources', () => {
  it('groups them from the registry, with the ones in no group among the job sites and the paid one apart', () => {
    const groups = sourceGroups(registry)
    expect(groups.map((g) => g.label)).toEqual(['Job sites', 'Paid', 'Company board systems', 'Remote job feeds'])
    expect(groups[0].sources).toEqual(['indeed', 'naukri', 'linkedin', 'glassdoor', 'ziprecruiter', 'google'])
    expect(groups[1].sources).toEqual(['linkedin_apify'])
  })

  it('switches one on or off and keeps the registry order', () => {
    expect(withSource(['indeed', 'remotive'], 'greenhouse', true, registry)).toEqual(['indeed', 'greenhouse', 'remotive'])
    expect(withSource(['indeed', 'remotive'], 'indeed', false, registry)).toEqual(['remotive'])
    expect(withSource(['indeed'], 'indeed', true, registry)).toEqual(['indeed'])
  })
})

describe('companies', () => {
  it('adds a company with or without a display name', () => {
    const a = addCompany({}, 'greenhouse', ' stripe ', '', registry)
    expect(a).toEqual({ boards: { greenhouse: [{ slug: 'stripe', name: null }] }, error: null })
    const b = addCompany(a.boards, 'greenhouse', 'notion', ' Notion ', registry)
    expect(b.boards.greenhouse).toEqual([{ slug: 'stripe', name: null }, { slug: 'notion', name: 'Notion' }])
    expect(companyCount(b.boards)).toBe(2)
  })

  it('refuses a slug that is not one, with the reason', () => {
    for (const bad of ['', '../x', 'a b', 'a/b', '-x', 'x'.repeat(65), 'é']) {
      const r = addCompany({}, 'lever', bad, '', registry)
      expect(r.error).toMatch(/letters, digits/)
      expect(r.boards).toEqual({})
    }
  })

  it('refuses a repeat, but SmartRecruiters slugs are case-sensitive and the others are not', () => {
    const have = { greenhouse: [{ slug: 'Stripe', name: null }], smartrecruiters: [{ slug: 'Bosch', name: null }] }
    expect(addCompany(have, 'greenhouse', 'stripe', '', registry).error).toMatch(/already listed/)
    expect(addCompany(have, 'smartrecruiters', 'bosch', '', registry).error).toBeNull()
    expect(addCompany(have, 'smartrecruiters', 'Bosch', '', registry).error).toMatch(/already listed/)
  })

  it('refuses a long or odd display name, and too many companies', () => {
    expect(addCompany({}, 'lever', 'x', 'n'.repeat(101), registry).error).toMatch(/at most 100/)
    expect(addCompany({}, 'lever', 'x', 'bad\u0007name', registry).error).toMatch(/control/)
    const full = { greenhouse: Array.from({ length: 300 }, (_, i) => ({ slug: `c${i}`, name: null })) }
    expect(addCompany(full, 'lever', 'one-more', '', registry).error).toMatch(/At most 300/)
  })

  it('removes a company, and drops a system with none left', () => {
    const have = { greenhouse: [{ slug: 'a', name: null }, { slug: 'b', name: null }], lever: [{ slug: 'c', name: null }] }
    expect(removeCompany(have, 'greenhouse', 'a')).toEqual({ greenhouse: [{ slug: 'b', name: null }], lever: [{ slug: 'c', name: null }] })
    expect(removeCompany(have, 'lever', 'c')).toEqual({ greenhouse: have.greenhouse })
  })
})

describe('sameProfile and hints', () => {
  it('ignores an empty board list and key order', () => {
    expect(sameProfile(profile({ boards: {} }), profile({ boards: { lever: [] } }))).toBe(true)
    expect(sameProfile(profile({ boards: { lever: [{ slug: 'a', name: null }], ashby: [{ slug: 'b', name: null }] } }),
      profile({ boards: { ashby: [{ slug: 'b', name: null }], lever: [{ slug: 'a', name: null }] } }))).toBe(true)
    expect(sameProfile(profile(), profile({ score_threshold: 61 }))).toBe(false)
    expect(sameProfile(profile(), profile({ search_terms: ['x'] }))).toBe(false)
  })

  it('says what is missing', () => {
    expect(hints(profile())).toEqual([])
    expect(hints(profile({ search_terms: [], locations: [] }))).toEqual(['Add at least one job title or keyword to search for.', 'Add at least one location.'])
    expect(hints(profile({ sources: [] }))).toEqual(['Switch at least one source on.'])
    expect(hints(profile({ sources: ['greenhouse'] }))[0]).toMatch(/Only company boards are on/)
    expect(hints(profile({ sources: ['greenhouse'], boards: { greenhouse: [{ slug: 'a', name: null }] } }))).toEqual([])
    expect(hints(profile({ sources: ['greenhouse', 'remotive'] }))).toEqual([])
    expect(hints(profile({ min_yoe: 5, max_yoe: 2 }))).toEqual(['The least years of experience is above the most.'])
    expect(hints(profile({ score_threshold: 559 }))).toEqual(['The shortlist score is 0 to 100.'])
    expect(hints(profile({ score_threshold: 0 }))).toEqual([])
    expect(hints(profile({ max_age_days: 0 }))).toEqual(['Newest postings only: 1 to 90 days.'])
    expect(hints(profile({ max_age_days: 91 }))).toEqual(['Newest postings only: 1 to 90 days.'])
    expect(hints(profile({ max_yoe: 61 }))).toEqual(['Years of experience can be 0 to 60.'])
  })

  it('treats the same sources in another order as the same profile', () => {
    expect(sameProfile(profile({ sources: ['indeed', 'remotive'] }), profile({ sources: ['remotive', 'indeed'] }))).toBe(true)
  })

  it('says when a list is longer than one save may be', () => {
    expect(hints(profile({ search_terms: Array.from({ length: 101 }, (_, i) => `t${i}`) }))).toEqual(['Job titles: at most 100.'])
    expect(hints(profile({ locations: ['x'.repeat(201)] }))).toEqual(['Locations: each at most 200 characters.'])
    expect(hints(profile({ board_title_words: Array.from({ length: 51 }, (_, i) => `w${i}`) }))).toEqual(['Extra title words: at most 50.'])
    expect(hints(profile({ exclude_companies: Array.from({ length: 500 }, (_, i) => `c${i}`) }))).toEqual([])
  })

  it('splits the scraper\'s joined problems', () => {
    expect(problemsOf('search_terms: at least one is required; locations: at least one is required')).toEqual(
      ['search_terms: at least one is required', 'locations: at least one is required'])
    expect(problemsOf('one problem')).toEqual(['one problem'])
  })
})
