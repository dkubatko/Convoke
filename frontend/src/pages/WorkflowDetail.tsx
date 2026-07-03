import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { shortDateTime, timeAgo, truncate } from '../lib/format'
import { WorkflowDetail as WorkflowDetailT, WorkflowChat } from '../lib/types'
import { coolingDown, funnel, stageStory, statusChip } from '../lib/intent'
import { useQuery } from '../hooks/useQuery'
import { Card, CardSkeleton, Chip, EmptyState, ErrorNote, Funnel, PageHead, StatusPill } from '../components/ui'
import { cooldownLabel } from './ChatDetail'

export default function WorkflowDetail() {
  const { workflowId } = useParams()
  const id = Number(workflowId)
  const wf = useQuery<WorkflowDetailT>(() => api.get(`/api/workflows/${id}/detail`), [id], {
    pollMs: 5000,
  })

  if (wf.loading) return <CardSkeleton lines={5} />
  if (wf.error) return <ErrorNote message={wf.error} onRetry={() => void wf.refetch()} />
  const w = wf.data!

  return (
    <div className="stack">
      <PageHead
        title={w.name}
        lede={
          <span>
            <Link to="/workflows">Workflows</Link> · {w.type} workflow
          </span>
        }
      />

      <Card title="Definition">
        <dl className="kv">
          {w.type === 'intent' ? (
            <>
              <dt>trigger</dt>
              <dd style={{ fontFamily: 'var(--font-body)' }}>{w.trigger_prompt}</dd>
              <dt>waits for</dt>
              <dd>{w.required_slots.map((s) => s.name).join(', ') || 'any match (fires on first match)'}</dd>
              <dt>cooldown</dt>
              <dd>{cooldownLabel(w.cooldown_seconds)}</dd>
              <dt>detector</dt>
              <dd>
                {w.examples_status}
                {w.threshold != null && ` · match threshold ${w.threshold.toFixed(2)}`}
              </dd>
            </>
          ) : (
            <>
              <dt>schedule</dt>
              <dd className="mono">
                {w.cron} · next {w.next_fire_at ? shortDateTime(w.next_fire_at) : '—'}
              </dd>
            </>
          )}
          <dt>on fire</dt>
          <dd>{w.confirm ? 'asks in the chat before acting' : 'acts without asking'}</dd>
          <dt>action</dt>
          <dd style={{ fontFamily: 'var(--font-body)' }}>{w.action_prompt}</dd>
          <dt>enabled</dt>
          <dd>{w.enabled ? 'yes' : 'no (paused globally)'}</dd>
        </dl>
      </Card>

      <div>
        <PageHead title="In these chats" />
        {w.chats.length === 0 ? (
          <Card>
            <EmptyState
              title="Not assigned to any chat"
              hint="Enable this workflow on a chat from that chat's Workflows tab."
            />
          </Card>
        ) : (
          <div className="stack" style={{ gap: 12 }}>
            {w.chats.map((c) => (
              <ChatStateCard key={c.chat_id} c={c} wf={w} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function ChatStateCard({ c, wf }: { c: WorkflowChat; wf: WorkflowDetailT }) {
  const s = c.states[0]
  const forum = c.states.length > 1
  return (
    <Card>
      <div className="page-head-row">
        <h3 style={{ fontSize: 15, margin: 0 }}>
          <Link to={`/chats/${c.chat_id}`} style={{ color: 'inherit' }} title="Open this chat">
            {c.chat_title}
          </Link>
        </h3>
        <span className="row" style={{ gap: 8 }}>
          {wf.type === 'intent' && <Chip {...statusChip(s, wf.examples_status)} live={s?.last_stage === 'accumulating'} />}
          {wf.type === 'intent' && c.pending_messages > 0 && <Chip label={`${c.pending_messages} new`} tone="idle" />}
          <StatusPill status={c.chat_status} live={c.chat_status === 'authorized'} />
        </span>
      </div>

      {wf.type === 'intent' && (
        <div className="stack" style={{ gap: 8, marginTop: 10 }}>
          {c.states.length === 0 ? (
            <p className="muted" style={{ fontSize: 12.5 }}>
              {stageStory(undefined, wf.threshold, wf.examples_status)}
            </p>
          ) : (
            c.states.map((st) => (
              <div className="stack" key={st.thread_key} style={{ gap: 8 }}>
                {forum && (
                  <div className="muted mono" style={{ fontSize: 11.5 }}>
                    thread {st.thread_key || 'main'}
                  </div>
                )}
                <p style={{ fontSize: 12.5, margin: 0 }}>{stageStory(st, wf.threshold, wf.examples_status)}</p>
                <Funnel steps={funnel(st, wf.threshold, wf.required_slots)} />
                <dl className="kv">
                  <dt>last check</dt>
                  <dd>{st.last_evaluated_at ? timeAgo(st.last_evaluated_at) : 'never'}</dd>
                  <dt>gathered</dt>
                  <dd>
                    {Object.keys(st.slots).length === 0
                      ? `nothing yet (needs: ${wf.required_slots.map((r) => r.name).join(', ') || 'any match'})`
                      : Object.entries(st.slots).map(([k, v]) => `${k}: ${v.value}`).join('  ·  ')}
                  </dd>
                  {coolingDown(st) && (
                    <>
                      <dt>cooldown</dt>
                      <dd>until {shortDateTime(st.cooldown_until!)}</dd>
                    </>
                  )}
                </dl>
              </div>
            ))
          )}
        </div>
      )}

      {c.recent_fires.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="card-title">Recent fires</div>
          <table className="data">
            <tbody>
              {c.recent_fires.map((f) => (
                <tr key={f.id}>
                  <td style={{ width: 100 }} className="mono muted">{timeAgo(f.created_at)}</td>
                  <td style={{ width: 150 }}>
                    <StatusPill status={f.status} />
                  </td>
                  <td className={f.error ? 'field-error' : 'muted'} style={{ fontSize: 12.5 }}>
                    {truncate(
                      f.error ??
                        Object.entries(f.slots).map(([k, v]) => `${k}: ${v.value}`).join(' · ') ??
                        '',
                      110,
                    ) || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
