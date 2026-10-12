import { APP_VERSION, docsUrl } from '@/lib/appInfo'

export default function AppFooter() {
  const docs = docsUrl(import.meta.env.VITE_REPO_URL)
  return (
    <footer className="mx-auto max-w-[96rem] px-4 pb-6 text-xs text-muted-foreground md:px-6">
      Dashboard v{APP_VERSION} ·{' '}
      {docs ? (
        <a href={docs} target="_blank" rel="noopener noreferrer" className="underline underline-offset-4 outline-none hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50">
          How it works (docs/dashboard.md)
        </a>
      ) : (
        <>How it works: <code>docs/dashboard.md</code> in the repository</>
      )}
    </footer>
  )
}
