import { useEffect, useState } from 'react'
import { api } from '../lib/api'

interface Health {
  status: string
  db: string
  pgvector: boolean
}

export default function Dashboard({ onLogout }: { onLogout: () => void }) {
  const [health, setHealth] = useState<Health | null>(null)

  useEffect(() => {
    api.get<Health>('/api/health').then(setHealth).catch(() => setHealth(null))
  }, [])

  async function logout() {
    await api.post('/api/auth/logout')
    onLogout()
  }

  return (
    <>
      <header>
        <strong>Convoke</strong>
        <button onClick={logout}>Sign out</button>
      </header>
      <main>
        <h2>System</h2>
        {health ? (
          <p>
            API: {health.status} · Database: {health.db} · pgvector:{' '}
            {health.pgvector ? 'enabled' : 'missing'}
          </p>
        ) : (
          <p>Backend unreachable.</p>
        )}
        <p>Bots, chats, workflows and MCP connections will appear here as they are built.</p>
      </main>
    </>
  )
}
