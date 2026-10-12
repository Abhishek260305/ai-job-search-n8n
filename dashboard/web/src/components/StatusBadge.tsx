import { cn } from '@/lib/utils'
import { statusLabel, statusTone, TONE_CLASSES } from '@/lib/status'

/** A status by its label (never by colour alone). */
export default function StatusBadge({ status, className }: { status: string; className?: string }) {
  return (
    <span className={cn('inline-flex h-6 items-center rounded-md px-2 text-xs font-medium whitespace-nowrap', TONE_CLASSES[statusTone(status)], className)}>
      {statusLabel(status)}
    </span>
  )
}
