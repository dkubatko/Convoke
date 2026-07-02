import { FormEvent, useState } from 'react'
import { api, ApiError } from '../lib/api'

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
      setError(err instanceof ApiError ? err.message : 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="centered">
      <h1>Convoke</h1>
      <form className="card" onSubmit={submit}>
        <label htmlFor="password">Operator password</label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
        />
        <button type="submit" disabled={busy || password.length === 0}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        {error && <div className="error">{error}</div>}
      </form>
    </div>
  )
}
