import { FormEvent, useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

interface Health {
  status: string
  db: string
  pgvector: boolean
}

interface Bot {
  id: number
  tg_bot_id: number
  username: string
  name: string
  can_read_all_group_messages: boolean
  status: string
  last_error: string | null
}

interface Chat {
  id: number
  bot_id: number
  tg_chat_id: number
  type: string
  title: string
  status: string
  authorized_by_name: string | null
}

export default function Dashboard({ onLogout }: { onLogout: () => void }) {
  const [health, setHealth] = useState<Health | null>(null)
  const [bots, setBots] = useState<Bot[]>([])
  const [chats, setChats] = useState<Chat[]>([])
  const [token, setToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(() => {
    api.get<Health>('/api/health').then(setHealth).catch(() => setHealth(null))
    api.get<Bot[]>('/api/bots').then(setBots).catch(() => {})
    api.get<Chat[]>('/api/chats').then(setChats).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, 10000)
    return () => clearInterval(interval)
  }, [refresh])

  async function addBot(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/bots', { token })
      setToken('')
      refresh()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to add bot')
    } finally {
      setBusy(false)
    }
  }

  async function removeBot(id: number) {
    if (!confirm('Remove this bot and all its chats and stored messages?')) return
    await api.delete(`/api/bots/${id}`)
    refresh()
  }

  async function recheckBot(id: number) {
    await api.post(`/api/bots/${id}/recheck`)
    refresh()
  }

  return (
    <>
      <header>
        <strong>Convoke</strong>
        <button onClick={async () => { await api.post('/api/auth/logout'); onLogout() }}>
          Sign out
        </button>
      </header>
      <main>
        <section>
          <h2>Bots</h2>
          <form className="row" onSubmit={addBot}>
            <input
              type="password"
              placeholder="Bot token from @BotFather"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              style={{ minWidth: '320px' }}
            />
            <button type="submit" disabled={busy || token.length === 0}>
              {busy ? 'Validating…' : 'Connect bot'}
            </button>
          </form>
          {error && <div className="error">{error}</div>}
          {bots.length === 0 ? (
            <p>No bots connected yet. Create one with @BotFather and paste its token above.</p>
          ) : (
            <table>
              <thead>
                <tr><th>Bot</th><th>Status</th><th>Group visibility</th><th /></tr>
              </thead>
              <tbody>
                {bots.map((b) => (
                  <tr key={b.id}>
                    <td>@{b.username}</td>
                    <td>{b.status}{b.last_error ? ` — ${b.last_error}` : ''}</td>
                    <td>
                      {b.can_read_all_group_messages ? (
                        <span className="ok">all messages</span>
                      ) : (
                        <span className="warn" title="Privacy mode is ON: the bot only sees mentions and replies, so chat memory will be empty. In @BotFather run /setprivacy → Disable, then REMOVE and RE-ADD the bot to each group.">
                          ⚠ privacy mode on — memory will be empty
                        </span>
                      )}
                    </td>
                    <td>
                      <button onClick={() => recheckBot(b.id)}>Re-check</button>{' '}
                      <button className="danger" onClick={() => removeBot(b.id)}>Remove</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section>
          <h2>Chats</h2>
          {chats.length === 0 ? (
            <p>No chats yet. Add a connected bot to a Telegram group to see it here.</p>
          ) : (
            <table>
              <thead>
                <tr><th>Title</th><th>Type</th><th>Status</th><th>Authorized by</th></tr>
              </thead>
              <tbody>
                {chats.map((c) => (
                  <tr key={c.id}>
                    <td>{c.title || c.tg_chat_id}</td>
                    <td>{c.type}</td>
                    <td>{c.status === 'authorized' ? '✅ authorized' : c.status === 'pending_auth' ? '⏳ waiting for admin' : c.status}</td>
                    <td>{c.authorized_by_name ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <footer>
          {health
            ? `API ${health.status} · DB ${health.db} · pgvector ${health.pgvector ? 'on' : 'MISSING'}`
            : 'Backend unreachable'}
        </footer>
      </main>
    </>
  )
}
