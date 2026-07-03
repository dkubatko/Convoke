import { NavLink, Outlet } from 'react-router-dom'
import { api } from '../lib/api'
import { useQuery } from '../hooks/useQuery'
import {
  IconBolt,
  IconBubbles,
  IconChip,
  IconGauge,
  IconPlane,
  IconPlug,
  IconSignOut,
} from './icons'

interface Health {
  status: string
  db: string
  pgvector: boolean
}

const NAV = [
  { to: '/', label: 'Overview', icon: <IconGauge />, end: true },
  { to: '/bots', label: 'Bots', icon: <IconPlane /> },
  { to: '/chats', label: 'Chats', icon: <IconBubbles /> },
  { to: '/workflows', label: 'Workflows', icon: <IconBolt /> },
  { to: '/tools', label: 'Tools', icon: <IconPlug /> },
  { to: '/models', label: 'Models', icon: <IconChip /> },
]

export default function Layout({ onSignOut }: { onSignOut: () => void }) {
  const health = useQuery<Health>(() => api.get('/api/health'), [], { pollMs: 30000 })
  const healthy = health.data?.status === 'ok' && health.data.pgvector

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="wordmark">
          <h1>Convoke</h1>
          <div className={`wire${healthy ? ' wire--live' : ''}`} aria-hidden />
          <div className="tagline">chat agent dispatch</div>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end}>
              {item.icon}
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className={`pill ${healthy ? 'pill--ok pill--live' : 'pill--err'}`}>
            <span className="lamp" aria-hidden />
            {health.loading ? 'checking…' : healthy ? 'all systems live' : 'backend unreachable'}
          </span>
          <button
            className="btn btn--quiet btn--sm"
            onClick={async () => {
              await api.post('/api/auth/logout')
              onSignOut()
            }}
          >
            <IconSignOut size={13} /> Sign out
          </button>
        </div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
