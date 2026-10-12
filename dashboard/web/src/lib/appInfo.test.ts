import { describe, expect, it } from 'vitest'
import { APP_VERSION, docsUrl, isTyping } from '@/lib/appInfo'

it('the version comes from package.json', () => {
  expect(APP_VERSION).toMatch(/^\d+\.\d+\.\d+/)
})

describe('docsUrl', () => {
  it('points at docs/dashboard.md in the repository', () => {
    expect(docsUrl('https://github.com/me/ai-job-search-n8n')).toBe('https://github.com/me/ai-job-search-n8n/blob/main/docs/dashboard.md')
    expect(docsUrl(' https://github.com/me/repo// ')).toBe('https://github.com/me/repo/blob/main/docs/dashboard.md')
  })

  it('has no link without a usable address', () => {
    expect(docsUrl(undefined)).toBeNull()
    expect(docsUrl('   ')).toBeNull()
    expect(docsUrl('javascript:alert(1)//')).toBeNull()
    expect(docsUrl('github.com/me/repo')).toBeNull()
  })
})

describe('isTyping', () => {
  it('is true in fields', () => {
    const editable = document.createElement('div')
    editable.setAttribute('contenteditable', 'true')
    const inner = document.createElement('span')
    editable.append(inner)
    for (const el of [document.createElement('input'), document.createElement('textarea'), document.createElement('select'), editable, inner]) {
      expect(isTyping(el)).toBe(true)
    }
  })

  it('is false elsewhere', () => {
    expect(isTyping(document.createElement('button'))).toBe(false)
    expect(isTyping(document.body)).toBe(false)
    expect(isTyping(null)).toBe(false)
  })
})
