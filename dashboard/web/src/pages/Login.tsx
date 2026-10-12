import { type FormEvent, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { ApiError } from '@/api/client'
import { useLogin } from '@/api/hooks'
import ThemeToggle from '@/components/ThemeToggle'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { safeNext } from '@/lib/safeNext'

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Something went wrong. Try again.'
  if (error.status === 401) return 'Wrong password.'
  if (error.status === 429) {
    const minutes = Math.max(1, Math.ceil((error.retryAfter ?? 900) / 60))
    return `Too many attempts. Try again in ${minutes} minute${minutes === 1 ? '' : 's'}.`
  }
  return error.detail
}

export default function Login() {
  const [password, setPassword] = useState('')
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const login = useLogin()

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!password || login.isPending) return
    login.mutate(password, {
      onSuccess: () => navigate(safeNext(params.get('next')), { replace: true }),
      onError: () => setPassword(''),
    })
  }

  return (
    <main className="relative flex min-h-svh items-center justify-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle className="font-heading text-xl">Job search dashboard</CardTitle>
          <CardDescription>Enter the dashboard password from the server's .env file.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="grid gap-4" noValidate>
            <div className="grid gap-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                autoFocus
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={login.isError || undefined}
                aria-describedby={login.isError ? 'login-error' : undefined}
              />
            </div>
            {login.isError && (
              <p id="login-error" role="alert" className="text-sm text-destructive">
                {errorMessage(login.error)}
              </p>
            )}
            <Button type="submit" disabled={!password || login.isPending}>
              {login.isPending ? 'Signing in…' : 'Sign in'}
            </Button>
          </form>
        </CardContent>
      </Card>
      {/* after the form, so the password field stays the first stop for Tab */}
      <ThemeToggle className="absolute top-3 right-3" />
    </main>
  )
}
