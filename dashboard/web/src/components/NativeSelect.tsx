import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

/** A plain <select>: keyboard, screen readers and phones handle it natively. */
export default function NativeSelect({ className, ...props }: ComponentProps<'select'>) {
  return (
    <select
      className={cn(
        'h-8 rounded-lg border border-input bg-background px-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50',
        className,
      )}
      {...props}
    />
  )
}
