import { ChevronDown } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

type Option<T extends string> = { value: T; label: string }

/** A button that opens a list of checkboxes; the menu stays open while ticking several. */
export default function MultiSelect<T extends string>({ label, options, selected, onChange }: {
  label: string
  options: readonly Option<T>[]
  selected: readonly T[]
  onChange: (next: T[]) => void
}) {
  const toggle = (value: T, on: boolean) =>
    onChange(options.map((o) => o.value).filter((v) => (v === value ? on : selected.includes(v))))

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" aria-label={selected.length ? `${label} filter: ${selected.length} selected` : `${label} filter`}>
          {label}
          {selected.length > 0 && <span className="rounded bg-muted px-1 tabular-nums">{selected.length}</span>}
          <ChevronDown aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-80 min-w-48 overflow-y-auto">
        {options.map((o) => (
          <DropdownMenuCheckboxItem
            key={o.value}
            checked={selected.includes(o.value)}
            onCheckedChange={(on) => toggle(o.value, on === true)}
            onSelect={(e) => e.preventDefault()}
          >
            {o.label}
          </DropdownMenuCheckboxItem>
        ))}
        {selected.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onChange([])}>Clear {label.toLowerCase()}</DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
