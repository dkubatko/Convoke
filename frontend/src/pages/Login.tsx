import { FormEvent, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { Field } from '../components/ui'

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/auth/login', { password })
      onLogin()
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? 'That password doesn’t match. It’s the CONVOKE_OPERATOR_PASSWORD from your .env file.'
          : 'The backend didn’t respond. Check that the stack is running.',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 20 }}>
      <div style={{ width: 'min(360px, 100%)' }}>
        <div className="wordmark" style={{ padding: 0, marginBottom: 22 }}>
          <div className="lockup">
            <img
              className="mark"
              src="/favicon.svg"
              alt=""
              width={38}
              height={38}
              style={{ width: 38, height: 38, borderRadius: 9 }}
            />
            <h1 style={{ fontSize: 28 }}>Convoke</h1>
          </div>
          <div className="wire" style={{ width: 130 }} aria-hidden />
          <div className="tagline">chat agent dispatch</div>
        </div>
        <form className="card card-pad stack" onSubmit={submit} style={{ gap: 14 }}>
          <Field label="Operator password" error={error}>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
              autoComplete="current-password"
            />
          </Field>
          <button className="btn btn--primary" type="submit" disabled={busy || !password}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}
