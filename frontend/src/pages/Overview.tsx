import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { stripTags, timeAgo, truncate } from '../lib/format'
import { Bot, Chat, GlobalRun, RoleAssignment, Workflow } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { Card, CardSkeleton, EmptyState, ErrorNote, PageHead, StatusPill, TableSkeleton } from '../components/ui'

export default function Overview() {
  const bots = useQuery<Bot[]>(() => api.get('/api/bots'), [], { pollMs: 15000 })
  const chats = useQuery<Chat[]>(() => api.get('/api/chats'), [], { pollMs: 15000 })
  const workflows = useQuery<Workflow[]>(() => api.get('/api/workflows'), [], { pollMs: 15000 })
  const roles = useQuery<RoleAssignment[]>(() => api.get('/api/model-roles'), [])
  const runs = useQuery<GlobalRun[]>(() => api.get('/api/runs?limit=12'), [], { pollMs: 10000 })

  const loading =
    bots.loading || chats.loading || roles.loading || workflows.loading || runs.loading
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
      {loading ? (
        <div className="stack">
          <CardSkeleton lines={2} />
          <section className="card card-pad">
            <TableSkeleton rows={3} />
          </section>
        </div>
      ) : (
        <div className="stack">
          {!setupDone && (
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
              <div className="stat">
                <b>{bots.data?.length ?? 0}</b>
                <span>bots</span>
              </div>
              <div className="stat">
                <b>{authorized.length}</b>
                <span>chats live</span>
              </div>
              <div className="stat">
                <b>{(chats.data ?? []).filter((c) => c.status === 'pending_auth').length}</b>
                <span>awaiting admin</span>
              </div>
              <div className="stat">
                <b>{(workflows.data ?? []).filter((w) => w.enabled).length}</b>
                <span>workflows on</span>
              </div>
            </div>
          </Card>

          <Card title="Recent agent activity" pad={false}>
            {runs.error ? (
              <ErrorNote message={runs.error} onRetry={() => void runs.refetch()} />
            ) : runs.data && runs.data.length > 0 ? (
              <table className="data">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Chat</th>
                    <th>Trigger</th>
                    <th>Status</th>
                    <th>What happened</th>
                  </tr>
                </thead>
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
                      <td className="muted">{truncate(stripTags(r.error ?? r.response_text ?? r.request_text), 90)}</td>
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
      )}
    </>
  )
}
