import { useState } from 'react'
import { Field } from '@/components/settings/parts'
import { joinLines, splitLines } from '@/lib/searchForm'

/** A list typed one item per line. The text is kept as typed (a blank line while typing is fine); the list is what is passed on. */
export default function ListField({ id, label, hint, value, onChange, rows = 4, placeholder }: {
  id: string; label: string; hint?: string; value: readonly string[]; onChange: (items: string[]) => void; rows?: number; placeholder?: string
}) {
  const [text, setText] = useState(() => joinLines(value))
  return (
    <Field id={id} label={label} hint={hint}>
      <textarea
        id={id}
        rows={rows}
        value={text}
        placeholder={placeholder}
        spellCheck={false}
        aria-describedby={hint ? `${id}-hint` : undefined}
        onChange={(e) => { setText(e.target.value); onChange(splitLines(e.target.value)) }}
        className="min-h-20 w-full rounded-lg border border-input bg-background px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
      />
    </Field>
  )
}
