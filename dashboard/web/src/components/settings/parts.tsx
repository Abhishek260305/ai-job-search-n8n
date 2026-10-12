import type { ReactNode } from 'react'
import { ApiError } from '@/api/client'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'

/** The scraper's own refusal, or why the request failed, next to the form that sent it. */
export function FormError({ error, local }: { error: unknown; local?: string | null }) {
  const text = local ?? (error ? (error instanceof ApiError ? error.detail : 'The change could not be saved.') : null)
  if (!text) return null
  return <p role={local ? undefined : 'alert'} className="text-sm text-destructive">{text}</p>
}

/** A label, a control and an optional hint, tied together for screen readers. */
export function Field({ id, label, hint, children, className }: { id: string; label: string; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <div className={cn('grid gap-1.5', className)}>
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p id={`${id}-hint`} className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
}

export const INPUT_CLASS = 'h-10 w-full min-w-0 rounded-lg border border-input bg-background px-2.5 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:opacity-50'
