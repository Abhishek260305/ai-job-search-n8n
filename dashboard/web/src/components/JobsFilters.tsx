import { Search, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import MultiSelect from '@/components/MultiSelect'
import NativeSelect from '@/components/NativeSelect'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { isTyping } from '@/lib/appInfo'
import { SOURCES, useJobsParams } from '@/lib/jobsParams'
import { clearFilters, hasFilters, sourceLabel, statusOptions } from '@/lib/jobsTable'
import { statusLabel } from '@/lib/status'

const SEARCH_DELAY_MS = 300
const MIN_SCORES = [40, 50, 60, 70, 80, 90]
const POSTED_DAYS = [1, 3, 7, 14, 30]

/** The search box: the URL follows the typing after a pause, and the box follows the URL (a card click clears it). */
function SearchBox() {
  const { view, update } = useJobsParams()
  const [text, setText] = useState(view.q ?? '')
  const [seen, setSeen] = useState(view.q)

  if (view.q !== seen) {
    setSeen(view.q)
    if ((view.q ?? '') !== text.trim()) setText(view.q ?? '')
  }

  useEffect(() => {
    const wanted = text.trim().slice(0, 100)
    if (wanted === (view.q ?? '')) return
    const timer = setTimeout(() => update({ q: wanted || undefined }), SEARCH_DELAY_MS)
    return () => clearTimeout(timer)
  }, [text, view.q, update])

  // "/" jumps to the search, unless it is typed into a field or a dialog, panel or menu has the keyboard
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return
      if (isTyping(event.target) || document.querySelector('[role="dialog"], [role="menu"], [role="listbox"]')) return
      event.preventDefault()
      input.current?.focus()
      input.current?.select()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="relative w-full sm:w-64">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <Input
        ref={input}
        type="search"
        aria-label="Search title or company"
        aria-keyshortcuts="/"
        placeholder="Search title or company"
        maxLength={100}
        className="pr-8 pl-8"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      {!text && (
        <kbd aria-hidden="true" className="pointer-events-none absolute top-1/2 right-2 hidden -translate-y-1/2 rounded border px-1.5 font-sans text-xs text-muted-foreground sm:inline">
          /
        </kbd>
      )}
    </div>
  )
}

export default function JobsFilters() {
  const { view, update, setView, jobId } = useJobsParams()
  const statuses = statusOptions(view.tab)

  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
      <SearchBox />
      {statuses.length > 0 && (
        <MultiSelect
          label="Status"
          options={statuses.map((s) => ({ value: s, label: statusLabel(s) }))}
          selected={view.status}
          onChange={(status) => update({ status })}
        />
      )}
      <MultiSelect
        label="Source"
        options={SOURCES.map((s) => ({ value: s, label: sourceLabel(s) }))}
        selected={view.source}
        onChange={(source) => update({ source })}
      />
      <NativeSelect
        aria-label="Minimum score"
        value={view.min_score ?? ''}
        onChange={(e) => update({ min_score: e.target.value ? Number(e.target.value) : undefined })}
      >
        <option value="">Any score</option>
        {MIN_SCORES.map((n) => <option key={n} value={n}>Score ≥ {n}</option>)}
      </NativeSelect>
      <NativeSelect
        aria-label="Posted within"
        value={view.posted_days ?? ''}
        onChange={(e) => update({ posted_days: e.target.value ? Number(e.target.value) : undefined })}
      >
        <option value="">Posted any time</option>
        {POSTED_DAYS.map((n) => <option key={n} value={n}>Posted in {n === 1 ? '24 hours' : `${n} days`}</option>)}
      </NativeSelect>
      <div className="flex items-center gap-2 px-1">
        <Checkbox id="filter-remote" checked={view.remote} onCheckedChange={(on) => update({ remote: on === true })} />
        <Label htmlFor="filter-remote" className="font-normal">Remote only</Label>
      </div>
      {hasFilters(view) && (
        <Button variant="ghost" onClick={() => setView(clearFilters(view), jobId)}>
          <X aria-hidden="true" />
          Clear filters
        </Button>
      )}
    </div>
  )
}
