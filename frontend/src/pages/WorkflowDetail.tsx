import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api'
import { shortDateTime, timeAgo, truncate } from '../lib/format'
import { Chat, WorkflowDetail as WorkflowDetailT, WorkflowChat } from '../lib/types'
import { dedupLabel, funnel, stageStory, statusChip } from '../lib/intent'
import { useQuery } from '../hooks/useQuery'
import { Card, CardSkeleton, Chip, EmptyState, ErrorNote, Funnel, PageHead, StatusPill } from '../components/ui'
import { EpisodeList } from '../components/Episodes'
import { WorkflowForm } from './Workflows'

export default function WorkflowDetail() {
  const { workflowId } = useParams()
  const id = Number(workflowId)
  const wf = useQuery<WorkflowDetailT>(() => api.get(`/api/workflows/${id}/detail`), [id], {
    pollMs: 5000,
  })
  const chats = useQuery<Chat[]>(() => api.get('/api/chats'), [])
  const [editing, setEditing] = useState(false)

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
        actions={
          !editing ? (
            <button className="btn btn--quiet" onClick={() => setEditing(true)}>
              Edit
            </button>
          ) : undefined
        }
      />

      {editing ? (
        <WorkflowForm
          chats={chats.data ?? []}
          initial={w}
          onCancel={() => setEditing(false)}
          onDone={() => {
            setEditing(false)
            void wf.refetch()
          }}
        />
      ) : (
      <Card title="Definition">
        <dl className="kv">
          {w.type === 'intent' ? (
            <>
              <dt>trigger</dt>
              <dd style={{ fontFamily: 'var(--font-body)' }}>{w.trigger_prompt}</dd>
              <dt>waits for</dt>
              <dd>{w.required_slots.map((s) => s.name).join(', ') || 'any match (fires on first match)'}</dd>
              <dt>dedup</dt>
              <dd>{dedupLabel(w)}</dd>
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
          <dt>chats</dt>
          <dd>
            {w.chats.length === 0
              ? 'none assigned — this workflow can’t fire'
              : w.chats.map((c) => c.chat_title).join(', ')}
          </dd>
        </dl>
      </Card>
      )}

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
  const forum = c.cursors.length > 1
  return (
    <Card>
      <div className="page-head-row">
        <h3 style={{ fontSize: 15, margin: 0 }}>
          <Link to={`/chats/${c.chat_id}`} style={{ color: 'inherit' }} title="Open this chat">
            {c.chat_title}
          </Link>
        </h3>
        <span className="row" style={{ gap: 8 }}>
          {wf.type === 'intent' && <Chip {...statusChip(c.cursors, c.episodes, wf.examples_status)} />}
          {wf.type === 'intent' && c.pending_messages > 0 && <Chip label={`${c.pending_messages} new`} tone="idle" />}
          <StatusPill status={c.chat_status} live={c.chat_status === 'authorized'} />
        </span>
      </div>

      {wf.type === 'intent' && (
        <div className="stack" style={{ gap: 8, marginTop: 10 }}>
          {c.cursors.length === 0 ? (
            <p className="muted" style={{ fontSize: 12.5 }}>
              {stageStory(undefined, c.episodes, wf.threshold, wf.examples_status)}
            </p>
          ) : (
            c.cursors.map((cur) => (
              <div className="stack" key={cur.thread_key} style={{ gap: 8 }}>
                {forum && (
                  <div className="muted mono" style={{ fontSize: 11.5 }}>
                    thread {cur.thread_key || 'main'}
                  </div>
                )}
                <p style={{ fontSize: 12.5, margin: 0 }}>
                  {stageStory(cur, c.episodes, wf.threshold, wf.examples_status)}
                </p>
                <Funnel
                  steps={funnel(cur, c.episodes, wf.threshold, wf.required_slots, {
                    pending: c.pending_messages > 0,
                    awaitingConfirm: c.recent_fires[0]?.status === 'confirm_wait',
                    minFireConfidence: wf.min_fire_confidence,
                  })}
                />
                <dl className="kv">
                  <dt>last check</dt>
                  <dd>{cur.last_evaluated_at ? timeAgo(cur.last_evaluated_at) : 'never'}</dd>
                </dl>
              </div>
            ))
          )}
          <EpisodeList
            episodes={c.episodes}
            requiredSlots={wf.required_slots}
            minFireConfidence={wf.min_fire_confidence}
          />
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
