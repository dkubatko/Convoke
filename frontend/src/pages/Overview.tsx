import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { stripTags, timeAgo } from '../lib/format'
import { Bot, Chat, GlobalRun, RoleAssignment, Workflow } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { Card, EmptyState, ErrorNote, HoverText, PageHead, SkeletonCol, SkeletonText, StatusPill, TableHead, TableSkeleton, ToolCalls } from '../components/ui'

/* Shared column spec for skeleton and loaded table (fixed layout) — widths
   match what auto layout solved for typical data, keeping the look unchanged. */
const RUN_COLS: SkeletonCol[] = [
  { header: 'When', w: '8%', kind: 'mono', bar: 64 },
  { header: 'Chat', w: '10%', bar: 110 },
  { header: 'Trigger', w: '8%', kind: 'mono', bar: 70 },
  { header: 'Status', w: '14%', kind: 'pill' },
  { header: 'What happened', w: '44%', kind: 'para' },
  { header: 'Tool calls', w: '16%', kind: 'pill' },
]

export default function Overview() {
  const bots = useQuery<Bot[]>(() => api.get('/api/bots'), [], { pollMs: 15000 })
  const chats = useQuery<Chat[]>(() => api.get('/api/chats'), [], { pollMs: 15000 })
  const workflows = useQuery<Workflow[]>(() => api.get('/api/workflows'), [], { pollMs: 15000 })
  const roles = useQuery<RoleAssignment[]>(() => api.get('/api/model-roles'), [])
  const runs = useQuery<GlobalRun[]>(() => api.get('/api/runs?limit=12'), [], { pollMs: 10000 })

  // Each card resolves on its own; the setup checklist additionally waits for
  // every query it reads, so it never flashes half-done steps.
  const setupLoading =
    bots.loading || chats.loading || roles.loading || workflows.loading
  const authorized = chats.data?.filter((c) => c.status === 'authorized') ?? []
  const steps = [
    {
      done: (roles.data ?? []).some((r) => r.role === 'agent' && r.model_id != null),
      label: 'Point the agent role at a model',
      hint: 'Any OpenAI-compatible endpoint works — Ollama, LM Studio, OpenRouter.',
      to: '/models',
    },
    {
      done: (bots.data ?? []).length > 0,
      label: 'Connect a Telegram bot',
      hint: 'Create one with @BotFather and disable privacy mode so it can hear the chat.',
      to: '/bots',
    },
    {
      done: authorized.length > 0,
      label: 'Get a chat authorized',
      hint: 'Add the bot to a group; a chat admin taps “Authorize Convoke”.',
      to: '/chats',
    },
    {
      done: (workflows.data ?? []).length > 0,
      label: 'Create a workflow',
      hint: 'Scheduled or intent-based — this is where the bot starts acting on its own.',
      to: '/workflows',
    },
  ]
  const setupDone = steps.every((s) => s.done)

  return (
    <>
      <PageHead
        title="Overview"
        lede="What your assistants are hearing, remembering, and doing right now."
      />
      <div className="stack">
        {!setupLoading && !setupDone && (
          <Card title="Getting on the air">
            <div className="checklist">
              {steps.map((s) => (
                <div key={s.label} className={`checklist-item${s.done ? ' done' : ''}`}>
                  <span className="step" aria-hidden />
                  <div className="what">
                    <b>{s.label}</b>
                    <p>{s.hint}</p>
                  </div>
                  {!s.done && (
                    <Link className="btn btn--quiet btn--sm" to={s.to}>
                      Open
                    </Link>
                  )}
                </div>
              ))}
            </div>
          </Card>
        )}

        <Card>
          <div className="stat-row">
            {/* Labels are static — only the numbers load. */}
            <Stat n={bots.data?.length} loading={bots.loading} label="bots" />
            <Stat n={authorized.length} loading={chats.loading} label="chats live" />
            <Stat
              n={(chats.data ?? []).filter((c) => c.status === 'pending_auth').length}
              loading={chats.loading}
              label="awaiting admin"
            />
            <Stat
              n={(workflows.data ?? []).filter((w) => w.enabled).length}
              loading={workflows.loading}
              label="workflows on"
            />
          </div>
        </Card>

        <Card title="Recent agent activity" pad={false}>
          {runs.loading ? (
            <TableSkeleton rows={8} cols={RUN_COLS} className="data--rows2" />
          ) : runs.error ? (
            <ErrorNote message={runs.error} onRetry={() => void runs.refetch()} />
          ) : runs.data && runs.data.length > 0 ? (
            <table className="data data--rows2">
              <TableHead cols={RUN_COLS} />
              <tbody>
                {runs.data.map((r) => (
                  <tr key={r.id}>
                    <td className="mono muted">{timeAgo(r.created_at)}</td>
                    <td>
                      <Link to={`/chats/${r.chat_id}`}>{r.chat_title || `chat ${r.chat_id}`}</Link>
                    </td>
                    <td className="mono">{r.trigger}</td>
                    <td>
                      <StatusPill status={r.status} />
                    </td>
                    <td className="muted">
                      <div className="clamp2">
                        <HoverText text={stripTags(r.error ?? r.response_text ?? r.request_text)} max={90} />
                      </div>
                    </td>
                    <td className="toolcall-cell">
                      <ToolCalls calls={r.tool_calls} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title="No agent runs yet"
              hint="Mention your bot in an authorized chat, or wait for a workflow to trigger."
            />
          )}
        </Card>
      </div>
    </>
  )
}

function Stat({ n, loading, label }: { n: number | undefined; loading: boolean; label: string }) {
  return (
    <div className="stat">
      <b>{loading ? <SkeletonText w={26} /> : n ?? 0}</b>
      <span>{label}</span>
    </div>
  )
}
