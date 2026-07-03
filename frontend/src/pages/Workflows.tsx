import { FormEvent, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { shortDateTime, timeAgo } from '../lib/format'
import { Chat, Fire, Workflow } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  CardSkeleton,
  EmptyState,
  ErrorNote,
  Field,
  PageHead,
  StatusPill,
  TableSkeleton,
} from '../components/ui'

function parseSlots(text: string) {
  return text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const [name, ...rest] = l.split(':')
      return { name: name.trim(), description: rest.join(':').trim() }
    })
}

export default function Workflows() {
  const workflows = useQuery<Workflow[]>(() => api.get('/api/workflows'), [], { pollMs: 10000 })
  const chats = useQuery<Chat[]>(() => api.get('/api/chats'), [])
  const toast = useToast()
  const confirm = useConfirm()

  const [showForm, setShowForm] = useState(false)

  async function toggle(wf: Workflow) {
    try {
      await api.put(`/api/workflows/${wf.id}`, { ...wf, enabled: !wf.enabled })
      toast('ok', `${wf.name} ${wf.enabled ? 'disabled' : 'enabled'}`)
      void workflows.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t update the workflow')
    }
  }

  async function remove(wf: Workflow) {
    const ok = await confirm({
      title: `Delete “${wf.name}”?`,
      body: 'Its trigger state and pending actions are removed with it.',
      actionLabel: 'Delete workflow',
      danger: true,
    })
    if (!ok) return
    try {
      await api.delete(`/api/workflows/${wf.id}`)
      toast('ok', `Deleted ${wf.name}`)
      void workflows.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t delete the workflow')
    }
  }

  return (
    <>
      <PageHead
        title="Workflows"
        lede="Standing orders for your assistants: act on a schedule, or when the chat converges on an intent."
        actions={
          <button className="btn btn--primary" onClick={() => setShowForm((v) => !v)}>
            {showForm ? 'Close' : 'New workflow'}
          </button>
        }
      />
      <div className="stack">
        {showForm && (
          <WorkflowForm
            chats={chats.data ?? []}
            onCreated={() => {
              setShowForm(false)
              void workflows.refetch()
            }}
          />
        )}

        {workflows.loading ? (
          <>
            <CardSkeleton lines={4} />
            <CardSkeleton lines={4} />
          </>
        ) : workflows.error ? (
          <ErrorNote message={workflows.error} onRetry={() => void workflows.refetch()} />
        ) : (workflows.data ?? []).length === 0 ? (
          <Card pad={false}>
            <EmptyState
              title="No workflows yet"
              hint="Try an intent workflow: “when the chat agrees on an event date, create it via the calendar tools.”"
              action={
                !showForm ? (
                  <button className="btn btn--primary" onClick={() => setShowForm(true)}>
                    New workflow
                  </button>
                ) : undefined
              }
            />
          </Card>
        ) : (
          workflows.data!.map((wf) => (
            <WorkflowCard
              key={wf.id}
              wf={wf}
              chats={chats.data ?? []}
              onToggle={() => void toggle(wf)}
              onDelete={() => void remove(wf)}
            />
          ))
        )}
      </div>
    </>
  )
}

function WorkflowCard({ wf, chats, onToggle, onDelete }: {
  wf: Workflow
  chats: Chat[]
  onToggle: () => void
  onDelete: () => void
}) {
  const [showFires, setShowFires] = useState(false)
  const fires = useQuery<Fire[]>(() => api.get(`/api/workflows/${wf.id}/fires`), [wf.id], {
    enabled: showFires,
    pollMs: showFires ? 5000 : undefined,
  })
  const chatNames = wf.chat_ids
    .map((id) => chats.find((c) => c.id === id)?.title || `chat ${id}`)
    .join(', ')

  return (
    <Card>
      <div className="page-head-row" style={{ marginBottom: 10 }}>
        <div className="row" style={{ gap: 10 }}>
          <h3 style={{ fontSize: 16 }}>{wf.name}</h3>
          <span className="pill pill--accent">
            <span className="lamp" aria-hidden />
            {wf.type}
          </span>
          <StatusPill status={wf.enabled ? 'active' : 'disabled'} live={wf.enabled} />
        </div>
        <span className="row">
          <button className="btn btn--quiet btn--sm" onClick={onToggle}>
            {wf.enabled ? 'Disable' : 'Enable'}
          </button>
          {wf.type === 'intent' && (
            <button className="btn btn--quiet btn--sm" onClick={() => setShowFires((v) => !v)}>
              {showFires ? 'Hide activity' : 'Activity'}
            </button>
          )}
          <button className="btn btn--danger btn--sm" onClick={onDelete}>
            Delete
          </button>
        </span>
      </div>

      <dl className="kv">
        {wf.type === 'scheduled' ? (
          <>
            <dt>schedule</dt>
            <dd>
              {wf.cron}
              {wf.next_fire_at ? ` · next ${shortDateTime(wf.next_fire_at)}` : ''}
            </dd>
          </>
        ) : (
          <>
            <dt>trigger</dt>
            <dd style={{ fontFamily: 'var(--font-body)' }}>{wf.trigger_prompt}</dd>
            {wf.required_slots.length > 0 && (
              <>
                <dt>needs</dt>
                <dd>{wf.required_slots.map((s) => s.name).join(', ')}</dd>
              </>
            )}
            <dt>detector</dt>
            <dd>
              {wf.examples_status === 'ready'
                ? `calibrated (threshold ${wf.threshold?.toFixed(2) ?? '—'})`
                : wf.examples_status === 'pending'
                  ? 'generating example phrases…'
                  : 'fallback — configure an agent model and edit the workflow to calibrate'}
              {wf.confirm ? ' · asks before acting' : ' · acts without asking'}
            </dd>
          </>
        )}
        <dt>action</dt>
        <dd style={{ fontFamily: 'var(--font-body)' }}>{wf.action_prompt}</dd>
        <dt>chats</dt>
        <dd style={{ fontFamily: 'var(--font-body)' }}>{chatNames || 'none assigned — this workflow can’t fire'}</dd>
      </dl>

      {showFires && (
        <div style={{ marginTop: 14 }}>
          {fires.loading ? (
            <TableSkeleton rows={2} />
          ) : fires.error ? (
            <ErrorNote message={fires.error} onRetry={() => void fires.refetch()} />
          ) : (fires.data ?? []).length === 0 ? (
            <p className="muted">Hasn't fired yet — per-chat detection state lives in each chat's Workflows tab.</p>
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Chat</th>
                  <th>Status</th>
                  <th>Gathered</th>
                </tr>
              </thead>
              <tbody>
                {fires.data!.map((f) => (
                  <tr key={f.id}>
                    <td className="mono muted">{timeAgo(f.created_at)}</td>
                    <td>
                      <Link to={`/chats/${f.chat_id}`}>{f.chat_title || `chat ${f.chat_id}`}</Link>
                    </td>
                    <td>
                      <StatusPill status={f.status} />
                      {f.error && <div className="field-error">{f.error}</div>}
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {Object.entries(f.slots)
                        .map(([k, v]) => `${k}: ${v.value}`)
                        .join(' · ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </Card>
  )
}

function WorkflowForm({ chats, onCreated }: { chats: Chat[]; onCreated: () => void }) {
  const toast = useToast()
  const [type, setType] = useState<'intent' | 'scheduled'>('intent')
  const [name, setName] = useState('')
  const [cron, setCron] = useState('0 9 * * *')
  const [trigger, setTrigger] = useState('')
  const [slots, setSlots] = useState('')
  const [action, setAction] = useState('')
  const [confirmFirst, setConfirmFirst] = useState(true)
  const [chatIds, setChatIds] = useState<number[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.post('/api/workflows', {
        name,
        type,
        action_prompt: action,
        cron: type === 'scheduled' ? cron : null,
        trigger_prompt: type === 'intent' ? trigger : null,
        required_slots: type === 'intent' ? parseSlots(slots) : [],
        confirm: confirmFirst,
        chat_ids: chatIds,
      })
      toast(
        'ok',
        type === 'intent'
          ? `Created ${name} — generating its detector phrases in the background`
          : `Created ${name}`,
      )
      onCreated()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Couldn’t create the workflow')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="New workflow">
      <form className="stack" style={{ gap: 14 }} onSubmit={submit}>
        <div className="grid-2">
          <Field label="Name">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Event scheduler" />
          </Field>
          <Field label="Kind">
            <select value={type} onChange={(e) => setType(e.target.value as 'intent' | 'scheduled')}>
              <option value="intent">Intent — fires when the chat converges on something</option>
              <option value="scheduled">Scheduled — fires on a cron schedule</option>
            </select>
          </Field>
        </div>

        {type === 'scheduled' ? (
          <Field label="Schedule (cron, UTC)" error={error?.includes('cron') ? error : null}>
            <input className="mono" value={cron} onChange={(e) => setCron(e.target.value)} />
          </Field>
        ) : (
          <>
            <Field
              label="Trigger — describe the moment to watch for, in plain words"
              hint="Convoke generates example phrases from this and watches the chat cheaply until they appear."
            >
              <textarea
                rows={2}
                value={trigger}
                onChange={(e) => setTrigger(e.target.value)}
                placeholder="The group agrees to schedule an event, with a specific date and time settled"
              />
            </Field>
            <Field
              label="Information to wait for (one per line: name: description)"
              hint="Values are collected across messages as the conversation unfolds — they never need to appear in a single message. The workflow fires once every line has a confident value."
            >
              <textarea
                rows={3}
                className="mono"
                value={slots}
                onChange={(e) => setSlots(e.target.value)}
                placeholder={'date: the agreed date and time\ntitle: what the event is'}
              />
            </Field>
            <label className="row" style={{ gap: 8 }}>
              <input
                type="checkbox"
                style={{ width: 'auto' }}
                checked={confirmFirst}
                onChange={(e) => setConfirmFirst(e.target.checked)}
              />
              Ask in the chat before acting
            </label>
          </>
        )}

        <Field label="Action — what the agent should do when this fires">
          <textarea
            rows={2}
            value={action}
            onChange={(e) => setAction(e.target.value)}
            placeholder="Create the event via the calendar tools, then post a one-line confirmation"
          />
        </Field>

        <Field label="Chats this applies to" error={chats.length === 0 ? 'No chats yet — add a bot to a group first' : null}>
          <div className="row" style={{ gap: 14 }}>
            {chats.map((c) => (
              <label key={c.id} className="row" style={{ gap: 6 }}>
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  checked={chatIds.includes(c.id)}
                  onChange={(e) =>
                    setChatIds(e.target.checked ? [...chatIds, c.id] : chatIds.filter((i) => i !== c.id))
                  }
                />
                {c.title || c.tg_chat_id}
              </label>
            ))}
          </div>
        </Field>

        {error && <p className="field-error">{error}</p>}
        <div className="row">
          <button
            className="btn btn--primary"
            disabled={busy || !name || !action || (type === 'intent' && !trigger)}
          >
            {busy ? 'Creating…' : 'Create workflow'}
          </button>
        </div>
      </form>
    </Card>
  )
}
