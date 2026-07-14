import { FormEvent, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { shortDateTime } from '../lib/format'
import { Chat, Workflow } from '../lib/types'
import { dedupLabel } from '../lib/intent'
import SettingsEditor from '../components/SettingsEditor'
import { Select } from '../components/Select'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  Check,
  ChoiceChips,
  EmptyState,
  ErrorNote,
  Field,
  KVSkeleton,
  PageHead,
  SkeletonButton,
  SkeletonPill,
  SkeletonText,
  StatusPill,
  TabBar,
} from '../components/ui'
import { useUrlTab } from '../hooks/useUrlTab'

const SUBTABS = ['Workflows', 'Settings'] as const

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

  const [subtab, setSubtab] = useUrlTab(SUBTABS, 'Workflows')
  // "New" opens one form at the top; "Edit" happens inline in that card.
  const [showForm, setShowForm] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)

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
          subtab === 'Workflows' ? (
            <button
              className="btn btn--primary"
              onClick={() => {
                setEditingId(null)
                setShowForm((v) => !v)
              }}
            >
              {showForm ? 'Close' : 'New workflow'}
            </button>
          ) : undefined
        }
      />
      <TabBar tabs={SUBTABS} active={subtab} onSelect={setSubtab} />
      {subtab === 'Settings' && (
        <SettingsEditor endpoint="/api/settings?page=workflows" />
      )}
      {subtab === 'Workflows' && (
      <div className="stack">
        {showForm && (
          <WorkflowForm
            key="new"
            chats={chats.data ?? []}
            initial={null}
            onCancel={() => setShowForm(false)}
            onDone={() => {
              setShowForm(false)
              void workflows.refetch()
            }}
          />
        )}

        {workflows.loading ? (
          <>
            <WorkflowCardSkeleton />
            <WorkflowCardSkeleton />
            <WorkflowCardSkeleton />
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
          workflows.data!.map((wf) =>
            editingId === wf.id ? (
              <WorkflowForm
                key={wf.id}
                chats={chats.data ?? []}
                initial={wf}
                onCancel={() => setEditingId(null)}
                onDone={() => {
                  setEditingId(null)
                  void workflows.refetch()
                }}
              />
            ) : (
              <WorkflowCard
                key={wf.id}
                wf={wf}
                chats={chats.data ?? []}
                onEdit={() => {
                  setShowForm(false)
                  setEditingId(wf.id)
                }}
                onToggle={() => void toggle(wf)}
                onDelete={() => void remove(wf)}
              />
            ),
          )
        )}
      </div>
      )}
    </>
  )
}

/** The WorkflowCard's geometry while the list loads: same header row (name,
    type + status pills, three small actions) over the same kv grid. */
function WorkflowCardSkeleton() {
  return (
    <Card>
      <div className="page-head-row" style={{ marginBottom: 10 }} role="status" aria-label="Loading">
        <div className="row" style={{ gap: 10 }}>
          <h3 style={{ fontSize: 16, margin: 0 }}>
            <SkeletonText w={150} />
          </h3>
          <SkeletonPill w={70} />
          <SkeletonPill w={78} />
        </div>
        <span className="row">
          <SkeletonButton sm w={44} />
          <SkeletonButton sm w={62} />
          <SkeletonButton sm w={56} />
        </span>
      </div>
      <KVSkeleton labels={['trigger', 'needs', 'detector', 'dedup', 'action', 'chats']} />
    </Card>
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
            <dt>dedup</dt>
            <dd>
              {dedupLabel(wf)}
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

export function WorkflowForm({ chats, initial, onDone, onCancel }: {
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
    initial ? Math.round(initial.cooldown_seconds / 60) : 0,
  )
  const [dedupHours, setDedupHours] = useState(initial?.dedup_window_hours ?? 12)
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
      cooldown_seconds: type === 'intent' ? Math.max(0, Math.round(cooldownMin * 60)) : 0,
      dedup_window_hours: type === 'intent' ? Math.max(1, Math.round(dedupHours)) : 12,
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
      <form onSubmit={submit}>
        <div className="form-section">
          <div className="grid-2">
            <Field label="Name">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Event scheduler" />
            </Field>
            <Field label="Kind" hint={editing ? 'Can’t change after creation.' : undefined}>
              <Select
                value={type}
                disabled={editing}
                ariaLabel="Workflow kind"
                onChange={(v) => setType(v as 'intent' | 'scheduled')}
                options={[
                  { value: 'intent', label: 'Intent — when the chat converges' },
                  { value: 'scheduled', label: 'Scheduled — on a cron clock' },
                ]}
              />
            </Field>
          </div>
        </div>

        {type === 'scheduled' ? (
          <div className="form-section">
            <div className="form-section-head">
              <h4>Schedule</h4>
            </div>
            <Field label="Cron expression (UTC)" error={error?.includes('cron') ? error : null}>
              <input className="mono" value={cron} onChange={(e) => setCron(e.target.value)} />
            </Field>
          </div>
        ) : (
          <>
            <div className="form-section">
              <div className="form-section-head">
                <h4>Trigger</h4>
                <span className="note">Convoke watches the chat cheaply until it matches.</span>
              </div>
              <Field label="The moment to watch for, in plain words">
                <textarea
                  rows={2}
                  value={trigger}
                  onChange={(e) => setTrigger(e.target.value)}
                  placeholder="The group agrees to schedule an event, with a specific date and time settled"
                />
              </Field>
              <Field
                label="Information to wait for"
                hint="One per line as name: description. Values are gathered across messages; it fires once each has a confident value."
              >
                <textarea
                  rows={3}
                  className="mono"
                  value={slots}
                  onChange={(e) => setSlots(e.target.value)}
                  placeholder={'date: the agreed date and time\ntitle: what the event is'}
                />
              </Field>
            </div>

            <div className="form-section">
              <div className="form-section-head">
                <h4>When it fires</h4>
                <span className="note">
                  Each occurrence is one topic; follow-ups on a handled topic don’t re-fire.
                </span>
              </div>
              <div className="grid-2">
                <Field
                  label="Follow-up window (hours)"
                  hint="How long a handled topic is remembered. After this, the same subject counts as new."
                >
                  <input
                    type="number"
                    min={1}
                    step={1}
                    className="mono"
                    value={dedupHours}
                    onChange={(e) => setDedupHours(Number(e.target.value))}
                  />
                </Field>
                <Field
                  label="Rate limit (minutes)"
                  hint="Minimum minutes between actions. A ready topic parks and re-checks when the limit lifts. 0 = off."
                >
                  <input
                    type="number"
                    min={0}
                    step={1}
                    className="mono"
                    value={cooldownMin}
                    onChange={(e) => setCooldownMin(Number(e.target.value))}
                  />
                </Field>
              </div>
              <Check checked={confirmFirst} onChange={setConfirmFirst}>
                Ask in the chat before acting
              </Check>
            </div>
          </>
        )}

        <div className="form-section">
          <div className="form-section-head">
            <h4>Action</h4>
          </div>
          <Field label="What the agent should do when this fires">
            <textarea
              rows={2}
              value={action}
              onChange={(e) => setAction(e.target.value)}
              placeholder="Create the event via the calendar tools, then post a one-line confirmation"
            />
          </Field>
        </div>

        <div className="form-section">
          <div className="form-section-head">
            <h4>Chats</h4>
            {chats.length > 0 && <span className="note">Which chats this workflow watches.</span>}
          </div>
          {chats.length === 0 ? (
            <p className="field-error">No chats yet — add a bot to a group first.</p>
          ) : (
            <ChoiceChips
              options={chats.map((c) => ({ value: c.id, label: c.title || String(c.tg_chat_id) }))}
              selected={chatIds}
              onToggle={(id, on) =>
                setChatIds(on ? [...chatIds, id] : chatIds.filter((i) => i !== id))
              }
            />
          )}
        </div>

        {error && <p className="field-error" style={{ marginTop: 14 }}>{error}</p>}
        <div className="form-actions">
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
