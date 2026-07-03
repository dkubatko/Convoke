import { FormEvent, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { shortDateTime } from '../lib/format'
import { Chat, Workflow } from '../lib/types'
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
  const [editing, setEditing] = useState<Workflow | null>(null)
  const formOpen = showForm || editing !== null

  function openEdit(wf: Workflow) {
    setEditing(wf)
    setShowForm(false)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }
  function closeForm() {
    setShowForm(false)
    setEditing(null)
  }

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
          <button
            className="btn btn--primary"
            onClick={() => (formOpen ? closeForm() : setShowForm(true))}
          >
            {formOpen ? 'Close' : 'New workflow'}
          </button>
        }
      />
      <div className="stack">
        {formOpen && (
          <WorkflowForm
            key={editing?.id ?? 'new'}
            chats={chats.data ?? []}
            initial={editing}
            onCancel={closeForm}
            onDone={() => {
              closeForm()
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
              onEdit={() => openEdit(wf)}
              onToggle={() => void toggle(wf)}
              onDelete={() => void remove(wf)}
            />
          ))
        )}
      </div>
    </>
  )
}

function WorkflowCard({ wf, chats, onEdit, onToggle, onDelete }: {
  wf: Workflow
  chats: Chat[]
  onEdit: () => void
  onToggle: () => void
  onDelete: () => void
}) {
  return (
    <Card>
      <div className="page-head-row" style={{ marginBottom: 10 }}>
        <div className="row" style={{ gap: 10 }}>
          <h3 style={{ fontSize: 16, margin: 0 }}>
            <Link to={`/workflows/${wf.id}`} style={{ color: 'inherit' }} title="Open this workflow">
              {wf.name}
            </Link>
          </h3>
          <span className="pill pill--accent">
            <span className="lamp" aria-hidden />
            {wf.type}
          </span>
          <StatusPill status={wf.enabled ? 'active' : 'disabled'} live={wf.enabled} />
        </div>
        <span className="row">
          <button className="btn btn--quiet btn--sm" onClick={onEdit}>
            Edit
          </button>
          <button className="btn btn--quiet btn--sm" onClick={onToggle}>
            {wf.enabled ? 'Disable' : 'Enable'}
          </button>
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
            </dd>
            <dt>cooldown</dt>
            <dd>
              {wf.cooldown_seconds === 0
                ? 'none — fires on every match'
                : wf.cooldown_seconds % 3600 === 0
                  ? `${wf.cooldown_seconds / 3600} h`
                  : `${Math.round(wf.cooldown_seconds / 60)} min`}
              {wf.confirm ? ' · asks before acting' : ' · acts without asking'}
            </dd>
          </>
        )}
        <dt>action</dt>
        <dd style={{ fontFamily: 'var(--font-body)' }}>{wf.action_prompt}</dd>
        <dt>chats</dt>
        <dd style={{ fontFamily: 'var(--font-body)' }}>
          {wf.chat_ids.length === 0 ? (
            'none assigned — this workflow can’t fire'
          ) : (
            wf.chat_ids.map((cid, i) => (
              <span key={cid}>
                {i > 0 && ', '}
                <Link to={`/chats/${cid}`}>{chats.find((c) => c.id === cid)?.title || `chat ${cid}`}</Link>
              </span>
            ))
          )}
        </dd>
      </dl>
    </Card>
  )
}

function WorkflowForm({ chats, initial, onDone, onCancel }: {
  chats: Chat[]
  initial?: Workflow | null
  onDone: () => void
  onCancel: () => void
}) {
  const toast = useToast()
  const editing = !!initial
  const [type, setType] = useState<'intent' | 'scheduled'>(
    (initial?.type as 'intent' | 'scheduled') ?? 'intent',
  )
  const [name, setName] = useState(initial?.name ?? '')
  const [cron, setCron] = useState(initial?.cron ?? '0 9 * * *')
  const [trigger, setTrigger] = useState(initial?.trigger_prompt ?? '')
  const [slots, setSlots] = useState(
    initial ? initial.required_slots.map((s) => `${s.name}: ${s.description ?? ''}`.trim()).join('\n') : '',
  )
  const [action, setAction] = useState(initial?.action_prompt ?? '')
  const [confirmFirst, setConfirmFirst] = useState(initial?.confirm ?? true)
  const [cooldownMin, setCooldownMin] = useState(
    initial ? Math.round(initial.cooldown_seconds / 60) : 60,
  )
  const [chatIds, setChatIds] = useState<number[]>(initial?.chat_ids ?? [])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    const body = {
      name,
      type,
      action_prompt: action,
      enabled: initial?.enabled ?? true,
      cron: type === 'scheduled' ? cron : null,
      trigger_prompt: type === 'intent' ? trigger : null,
      required_slots: type === 'intent' ? parseSlots(slots) : [],
      confirm: confirmFirst,
      cooldown_seconds: type === 'intent' ? Math.max(0, Math.round(cooldownMin * 60)) : 3600,
      chat_ids: chatIds,
    }
    try {
      if (editing) {
        await api.put(`/api/workflows/${initial!.id}`, body)
        toast('ok', `Saved changes to ${name}`)
      } else {
        await api.post('/api/workflows', body)
        toast(
          'ok',
          type === 'intent'
            ? `Created ${name} — generating its detector phrases in the background`
            : `Created ${name}`,
        )
      }
      onDone()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Couldn’t save the workflow')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title={editing ? `Edit “${initial!.name}”` : 'New workflow'}>
      <form className="stack" style={{ gap: 14 }} onSubmit={submit}>
        <div className="grid-2">
          <Field label="Name">
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Event scheduler" />
          </Field>
          <Field label="Kind" hint={editing ? 'The kind of a workflow can’t be changed after creation.' : undefined}>
            <select
              value={type}
              disabled={editing}
              onChange={(e) => setType(e.target.value as 'intent' | 'scheduled')}
            >
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
            <Field
              label="Cooldown (minutes)"
              hint="After it fires, the workflow won't fire again in the same chat for this long — stops one ongoing conversation from re-triggering it. 0 = fire on every match. Slot-based workflows also self-dedupe by resetting their gathered values."
            >
              <input
                type="number"
                min={0}
                step={1}
                className="mono"
                style={{ maxWidth: 120 }}
                value={cooldownMin}
                onChange={(e) => setCooldownMin(Number(e.target.value))}
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
            {busy ? 'Saving…' : editing ? 'Save changes' : 'Create workflow'}
          </button>
          <button type="button" className="btn btn--quiet" onClick={onCancel}>
            Cancel
          </button>
          {editing && type === 'intent' && trigger !== initial!.trigger_prompt && (
            <span className="muted" style={{ fontSize: 12 }}>
              Changing the trigger re-generates the detector phrases.
            </span>
          )}
        </div>
      </form>
    </Card>
  )
}
