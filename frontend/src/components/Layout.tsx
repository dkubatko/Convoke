import { useSyncExternalStore } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { api } from '../lib/api'
import { debugForcedLoading, useQuery } from '../hooks/useQuery'
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
  // Debug: the health pill doubles as a skeleton-preview toggle (see useQuery).
  // While previewing, health.loading is forced too, so label from the flag.
  const previewing = useSyncExternalStore(debugForcedLoading.subscribe, debugForcedLoading.read)

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="wordmark">
          <div className="lockup">
            <img className="mark" src="/favicon.svg" alt="" width={26} height={26} />
            <h1>Convoke</h1>
          </div>
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
          <button
            type="button"
            className={`pill ${previewing ? 'pill--warn' : healthy ? 'pill--ok pill--live' : 'pill--err'}`}
            style={{ cursor: 'pointer', border: 'none' }}
            role="switch"
            aria-checked={previewing}
            title="Debug: preview loading skeletons — forces every query's loading state (already-loaded, data-gated content stays visible)"
            onClick={() => debugForcedLoading.toggle()}
          >
            <span className="lamp" aria-hidden />
            {previewing
              ? 'skeleton preview'
              : health.loading
                ? 'checking…'
                : healthy
                  ? 'all systems live'
                  : 'backend unreachable'}
          </button>
          <button
            className="btn btn--quiet btn--sm"
            onClick={async () => {
              // Clear the local session regardless — the cookie is httpOnly and
              // a failed logout POST shouldn't strand the user "signed in".
              try {
                await api.post('/api/auth/logout')
              } catch {
                // ignore; sign the user out locally anyway
              }
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
