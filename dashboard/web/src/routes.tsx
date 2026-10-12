import type { RouteObject } from 'react-router-dom'
import RequireAuth from '@/components/RequireAuth'
import Login from '@/pages/Login'
import NotFound from '@/pages/NotFound'
import HealthRoute from '@/pages/HealthRoute'
import OverviewRoute from '@/pages/OverviewRoute'
import SearchRoute from '@/pages/SearchRoute'
import SettingsRoute from '@/pages/SettingsRoute'
import SpendRoute from '@/pages/SpendRoute'

// Shared by main.tsx (browser history) and the tests (memory history).
export const routes: RouteObject[] = [
  { path: '/login', element: <Login /> },
  {
    element: <RequireAuth />,
    children: [
      { path: '/', element: <OverviewRoute /> },
      { path: '/health', element: <HealthRoute /> },
      { path: '/spend', element: <SpendRoute /> },
      { path: '/search', element: <SearchRoute /> },
      { path: '/settings', element: <SettingsRoute /> },
      { path: '*', element: <NotFound /> },
    ],
  },
]
