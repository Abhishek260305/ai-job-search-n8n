import { Link } from 'react-router-dom'
import { useState } from 'react'
import { useEmailInbox } from '@/api/hooks'
import type { EmailInbox, EmailItem, EmailState } from '@/api/types'
import AppFooter from '@/components/AppFooter'
import AppHeader from '@/components/AppHeader'
import EmailCard from '@/components/email/EmailCard'
import JobDrawer from '@/components/JobDrawer'
import { PanelError, PanelSkeleton } from '@/components/ops/PanelState'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useJobsParams } from '@/lib/jobsParams'

const TABS: readonly { state: EmailState; label: string }[] = [
  { state: 'pending', label: 'To review' },
  { state: 'accepted', label: 'Accepted' },
  { state: 'dismissed', label: 'Dismissed' },
]

const PANEL_ID = 'email-panel'
/** The key includes what the person reads: if a later email replaces this one in place (same id), a tick given for the old one does not carry over. */
const cardKey = (item: EmailItem) => `${item.id}:${item.from_domain}:${item.subject}:${item.confidence}:${item.flags.join(',')}`
const tabId = (state: EmailState) => `email-tab-${state}`

const EMPTY: Record<EmailState, string> = {
  pending: 'Nothing to review. New replies to your applications appear here after the mail reader runs (every 30 minutes by default).',
  accepted: 'Nothing accepted yet.',
  dismissed: 'Nothing dismissed.',
}

/** What to say when the Gmail part is not doing its job, so an empty list is never a mystery. */
function Banner({ inbox }: { inbox: EmailInbox }) {
  if (!inbox.enabled) {
    return (
      <p role="note" className="rounded-xl border bg-muted/50 p-4 text-sm">
        The Gmail part is <strong>off</strong>, so no new replies are being read. Switch it on under <Link to="/settings" className="underline">Settings</Link> (it also needs the
        Google login: <code>scripts/jobsearch gmail login</code>). Suggestions already made stay here.
      </p>
    )
  }
  if (inbox.login === 'expired') {
    return (
      <p role="alert" className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
        The Google login expired or was revoked, so no replies are being read. Sign in again with <code>scripts/jobsearch gmail login</code>.
      </p>
    )
  }
  return null
}

export default function EmailPage() {
  const [state, setState] = useState<EmailState>('pending')
  const inbox = useEmailInbox(state)
  const { view, setView } = useJobsParams()
  const counts = inbox.data?.counts
  return (
    <>
      <AppHeader page="email" />
      <main className="mx-auto max-w-[56rem] space-y-4 p-4 md:p-6">
        <div>
          <h2 className="font-heading text-lg font-semibold">Email replies</h2>
          <p className="text-sm text-muted-foreground">
            Replies to your applications that look like a confirmation, an interview, an offer or a rejection. Nothing changes until you accept one: you decide,
            and accepting is the same change as moving the job on the board (it is in the job&apos;s history and can be undone).
          </p>
        </div>
        {inbox.data && <Banner inbox={inbox.data} />}
        <Tabs value={state} onValueChange={(v) => setState(v as EmailState)}>
          <TabsList aria-label="Suggestions">
            {TABS.map((t) => (
              <TabsTrigger key={t.state} value={t.state} id={tabId(t.state)} aria-controls={PANEL_ID}>
                {t.label}{counts ? ` (${counts[t.state]})` : ''}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <div id={PANEL_ID} role="tabpanel" aria-labelledby={tabId(state)} className="grid gap-3">
          {counts && inbox.data && counts[state] > inbox.data.items.length && (
            <p className="text-sm text-muted-foreground">Showing the newest {inbox.data.items.length} of {counts[state]}. Deal with these and the rest appear.</p>
          )}
          {inbox.data ? (
            inbox.data.items.length === 0 ? (
              <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">{EMPTY[state]}</p>
            ) : (
              <ul className="grid gap-3" aria-label={TABS.find((t) => t.state === state)?.label}>
                {inbox.data.items.map((item) => (
                  <li key={cardKey(item)}><EmailCard item={item} onOpen={(id) => setView(view, id)} /></li>
                ))}
              </ul>
            )
          ) : inbox.isError ? (
            <PanelError what="The email replies" error={inbox.error} retry={() => inbox.refetch()} />
          ) : (
            <PanelSkeleton label="Loading the email replies" />
          )}
        </div>
      </main>
      <AppFooter />
      <JobDrawer neighbours={false} />
    </>
  )
}
