import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { AppProviders } from '@/AppProviders'
import { createQueryClient } from '@/lib/queryClient'
import { connectUnauthorized } from '@/lib/router'
import { applyTheme, currentTheme } from '@/lib/theme'
import { routes } from '@/routes'
import './index.css'

// public/theme.js normally did this before the first paint; this covers it failing to load
applyTheme(currentTheme())

const router = createBrowserRouter(routes)
connectUnauthorized(router)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders client={createQueryClient()}>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
)
