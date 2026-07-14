import { Link, useNavigate } from 'react-router-dom'
import { api } from '../lib/api'
import { timeAgo } from '../lib/format'
import { Bot, Chat } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { Card, EmptyState, ErrorNote, PageHead, SkeletonCol, StatusPill, TableHead, TableSkeleton } from '../components/ui'

/* One column spec for skeleton AND loaded table (table.data is layout: fixed,
   so these widths are the columns — matched to what auto layout used to solve
   for typical data, to keep the rendered look unchanged). */
const COLS: SkeletonCol[] = [
  { header: 'Chat', w: '22%', kind: 'twoline', bar: 130 },
  { header: 'Bot', w: '28%', kind: 'mono', bar: 100 },
  { header: 'Status', w: '25%', kind: 'pill' },
  { header: 'Authorized by', w: '25%', bar: 150 },
]

export default function Chats() {
  const chats = useQuery<Chat[]>(() => api.get('/api/chats'), [], { pollMs: 10000 })
  const bots = useQuery<Bot[]>(() => api.get('/api/bots'), [])
  const navigate = useNavigate()

  const botName = (id: number) => {
    const bot = bots.data?.find((b) => b.id === id)
    return bot ? `@${bot.username}` : `bot ${id}`
  }

  return (
    <>
      <PageHead
        title="Chats"
        lede="Every group a bot has been added to. A chat goes live once one of its admins taps “Authorize Convoke” in Telegram."
      />
      <Card pad={false}>
        {chats.loading ? (
          <TableSkeleton rows={8} cols={COLS} />
        ) : chats.error ? (
          <ErrorNote message={chats.error} onRetry={() => void chats.refetch()} />
        ) : (chats.data ?? []).length === 0 ? (
          <EmptyState
            title="No chats yet"
            hint="Add a connected bot to a Telegram group and it will show up here within seconds."
          />
        ) : (
          <table className="data">
            <TableHead cols={COLS} />
            <tbody>
              {chats.data!.map((c) => (
                <tr
                  key={c.id}
                  className="rowlink"
                  onClick={() => navigate(`/chats/${c.id}`)}
                  style={{ cursor: 'pointer' }}
                >
                  <td>
                    {/* Link kept for keyboard/middle-click; stops the row
                        handler from double-navigating on a direct click. */}
                    <Link
                      to={`/chats/${c.id}`}
                      style={{ color: 'inherit' }}
                      onClick={(e) => e.stopPropagation()}
                    >
                      <b>{c.title || c.tg_chat_id}</b>
                    </Link>
                    <div className="muted mono" style={{ fontSize: 11.5 }}>
                      {c.type}
                      {c.is_forum ? ' · forum' : ''}
                    </div>
                  </td>
                  <td className="mono">{botName(c.bot_id)}</td>
                  <td>
                    <StatusPill status={c.status} live={c.status === 'authorized'} />
                  </td>
                  <td className="muted">
                    {c.authorized_by_name
                      ? `${c.authorized_by_name}${c.authorized_at ? ` · ${timeAgo(c.authorized_at)}` : ''}`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </>
  )
}
