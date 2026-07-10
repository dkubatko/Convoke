import { FormEvent, ReactNode, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { shortDateTime, stripTags, timeAgo } from '../lib/format'
import {
  Chat,
  ChatThread,
  ChatWorkflow,
  Gap,
  ImportJob,
  McpServer,
  MediaStatus,
  Member,
  Message,
  MessageAttachment,
  Run,
  SearchHit,
  ToolCall,
} from '../lib/types'
import { dedupLabel, funnel, stageStory, statusChip } from '../lib/intent'
import { EpisodeList } from '../components/Episodes'
import SettingsEditor from '../components/SettingsEditor'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  CardSkeleton,
  Check,
  Chip,
  EmptyState,
  ErrorNote,
  Funnel,
  HoverCard,
  HoverText,
  PageHead,
  StatusPill,
  TabBar,
  TableSkeleton,
  ToolCalls,
} from '../components/ui'
import { useUrlTab } from '../hooks/useUrlTab'

const TABS = ['Memory', 'Members', 'Workflows', 'Threads', 'Import history', 'Tools', 'Agent runs', 'Settings'] as const

export default function ChatDetail() {
  const { chatId } = useParams()
  const id = Number(chatId)
  const [tab, setTab] = useUrlTab(TABS, 'Memory')

  // Polled so the header follows status changes — a chat waiting on admin
  // authorization flips to live without a manual reload.
  const chat = useQuery<Chat>(() => api.get(`/api/chats/${id}`), [id], { pollMs: 5000 })

  if (chat.loading) return <CardSkeleton lines={5} />
  if (chat.error === 'Chat not found' || (!chat.error && !chat.data)) {
    return (
      <EmptyState
        title="Chat not found"
        hint="It may have been removed along with its bot."
        action={<Link className="btn btn--quiet" to="/chats">Back to chats</Link>}
      />
    )
  }
  if (chat.error || !chat.data) {
    return <ErrorNote message={chat.error!} onRetry={() => void chat.refetch()} />
  }

  const c = chat.data
  return (
    <>
      <PageHead
        title={c.title || String(c.tg_chat_id)}
        actions={<StatusPill status={c.status} live={c.status === 'authorized'} />}
        lede={
          c.status === 'authorized'
            ? `Live since ${c.authorized_at ? shortDateTime(c.authorized_at) : 'authorization'}${c.authorized_by_name ? `, authorized by ${c.authorized_by_name}` : ''}.`
            : 'Waiting for a chat admin to tap “Authorize Convoke” in Telegram. Nothing is stored until then.'
        }
      />
      <TabBar tabs={TABS} active={tab} onSelect={setTab} />
      {tab === 'Memory' && <MemoryTab chatId={id} />}
      {tab === 'Members' && <MembersTab chatId={id} />}
      {tab === 'Workflows' && <WorkflowsTab chatId={id} />}
      {tab === 'Threads' && <ThreadsTab chatId={id} />}
      {tab === 'Import history' && <ImportTab chatId={id} />}
      {tab === 'Tools' && <ToolsTab chatId={id} />}
      {tab === 'Agent runs' && <RunsTab chatId={id} />}
      {tab === 'Settings' && (
        <SettingsEditor endpoint={`/api/chats/${id}/settings`} />
      )}
    </>
  )
}

/* ---------------- Memory ---------------- */

function MemoryTab({ chatId }: { chatId: number }) {
  const toast = useToast()
  const confirm = useConfirm()
  const messages = useQuery<Message[]>(
    () => api.get(`/api/chats/${chatId}/messages?limit=25`),
    [chatId],
    { pollMs: 10000 },
  )
  const mediaStatus = useQuery<MediaStatus>(
    () => api.get(`/api/chats/${chatId}/media-status`),
    [chatId],
    { pollMs: 10000 },
  )
  const gaps = useQuery<Gap[]>(() => api.get(`/api/chats/${chatId}/gaps`), [chatId])

  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [forgetSender, setForgetSender] = useState('')

  async function search(e: FormEvent) {
    e.preventDefault()
    setSearching(true)
    try {
      setHits(await api.get(`/api/chats/${chatId}/search?q=${encodeURIComponent(query)}&k=4`))
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Search failed')
    } finally {
      setSearching(false)
    }
  }

  async function forget(body: Record<string, unknown>, what: string) {
    const ok = await confirm({
      title: `Forget ${what}?`,
      body: 'Matching stored messages are deleted and the chat’s memory is rebuilt without them. Telegram itself is not touched.',
      actionLabel: 'Forget',
      danger: true,
    })
    if (!ok) return
    try {
      const result = await api.post<{ deleted_messages: number }>(`/api/chats/${chatId}/forget`, body)
      toast('ok', `Forgot ${result.deleted_messages} message${result.deleted_messages === 1 ? '' : 's'}`)
      setForgetSender('')
      void messages.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t forget those messages')
    }
  }

  return (
    <div className="stack">
      {gaps.data && gaps.data.length > 0 && (
        <Card title="Gaps in memory">
          <p className="muted" style={{ marginBottom: 10 }}>
            Convoke was offline longer than Telegram keeps updates (24 hours). Messages in these
            ranges were never received and can only be recovered with a history import.
          </p>
          <dl className="kv">
            {gaps.data.map((g) => (
              <div key={g.id} style={{ display: 'contents' }}>
                <dt>gap</dt>
                <dd>
                  {shortDateTime(g.gap_start)} → {shortDateTime(g.gap_end)}
                </dd>
              </div>
            ))}
          </dl>
        </Card>
      )}

      <Card title="Search this chat's memory">
        <form className="row" onSubmit={search}>
          <input
            style={{ flex: '1 1 280px' }}
            placeholder="What was decided about…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="btn btn--primary" disabled={searching || !query}>
            {searching ? 'Searching…' : 'Search'}
          </button>
        </form>
        {hits !== null && (
          <div className="stack" style={{ marginTop: 14, gap: 10 }}>
            {hits.length === 0 ? (
              <p className="muted">
                Nothing indexed matches. Conversations are indexed shortly after they go quiet, so
                very recent messages may not be searchable yet.
              </p>
            ) : (
              hits.map((h, i) => (
                <pre key={h.chunk_id} className="transcript">
                  {/* RRF scores rank hits but aren't a human-meaningful 0–1
                      similarity — show the rank, not a fake percentage. */}
                  <span className="muted">match #{i + 1}</span>
                  {'\n'}
                  {h.rendered}
                </pre>
              ))
            )}
          </div>
        )}
      </Card>

      {mediaStatus.data &&
        mediaStatus.data.pending +
          mediaStatus.data.described +
          mediaStatus.data.failed +
          mediaStatus.data.skipped >
          0 && (
          <Card>
            <div className="row" style={{ alignItems: 'center', gap: 8 }}>
              <b style={{ fontSize: 13 }}>Media memory</b>
              <span className="pill pill--accent">{mediaStatus.data.described} described</span>
              {mediaStatus.data.pending > 0 && (
                <span className="pill pill--idle pill--live">
                  <span className="lamp" aria-hidden />
                  {mediaStatus.data.pending} pending
                </span>
              )}
              {mediaStatus.data.failed > 0 && (
                <span className="pill pill--err">{mediaStatus.data.failed} failed</span>
              )}
              {mediaStatus.data.skipped > 0 && (
                <span
                  className="pill pill--warn"
                  title="Assign vision/transcription models on the Models page to process these"
                >
                  {mediaStatus.data.skipped} skipped
                </span>
              )}
            </div>
          </Card>
        )}

      <Card title="Latest messages" pad={false}>
        {messages.loading ? (
          <TableSkeleton rows={5} />
        ) : (messages.data ?? []).length === 0 ? (
          <EmptyState
            title="Nothing stored yet"
            hint="Messages appear here once the chat is authorized and people start talking."
          />
        ) : (
          <table className="data">
            <tbody>
              {messages.data!.map((m) => (
                <tr key={m.id}>
                  <td style={{ width: 160 }}>
                    <b>{m.sender_name || '—'}</b>
                    <div className="muted mono" style={{ fontSize: 11 }}>
                      {timeAgo(m.sent_at)}
                      {m.source !== 'live' ? ` · ${m.source}` : ''}
                    </div>
                  </td>
                  <td>
                    {m.reply_to && (
                      <div
                        className="muted"
                        style={{
                          fontSize: 12,
                          borderLeft: '2px solid var(--line-strong)',
                          padding: '1px 8px',
                          marginBottom: 4,
                        }}
                      >
                        ↪ <b>{m.reply_to.sender_name}</b>:{' '}
                        <HoverText text={m.reply_to.text} max={90} />
                      </div>
                    )}
                    {m.attachment && <AttachmentLine att={m.attachment} />}
                    {m.text && <HoverText text={m.text} max={180} />}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Forget">
        <p className="muted" style={{ marginBottom: 12 }}>
          Telegram never tells bots when messages are deleted — this is how you remove stored
          content on someone's behalf.
        </p>
        <div className="row">
          <input
            style={{ width: 160 }}
            type="number"
            placeholder="Sender id"
            value={forgetSender}
            onChange={(e) => setForgetSender(e.target.value)}
          />
          <button
            className="btn btn--quiet"
            disabled={!forgetSender || !Number.isFinite(Number(forgetSender))}
            onClick={() => void forget({ sender_id: Number(forgetSender) }, `everything from sender ${forgetSender}`)}
          >
            Forget sender
          </button>
          <button
            className="btn btn--danger"
            onClick={() => void forget({ everything: true }, 'this entire chat’s stored history, memory, and notes')}
          >
            Forget entire chat
          </button>
        </div>
      </Card>
    </div>
  )
}

/* ---------------- Workflows ---------------- */

function AttachmentLine({ att }: { att: MessageAttachment }) {
  const dur = att.duration_s ? ` ${Math.floor(att.duration_s / 60)}:${String(att.duration_s % 60).padStart(2, '0')}` : ''
  const label = `${att.kind.replace('_', ' ')}${dur}`
  const tone =
    att.status === 'described' ? 'accent' : att.status === 'failed' ? 'err' : att.status === 'skipped' ? 'warn' : 'idle'
  const body =
    att.status === 'described'
      ? [att.description, att.transcript && `“${att.transcript}”`].filter(Boolean).join(' — ')
      : att.status === 'pending'
        ? 'description pending…'
        : att.error ?? att.status
  return (
    <div style={{ marginBottom: 4 }}>
      <span className={`pill pill--${tone}`}>{label}</span>{' '}
      <span className="muted" style={{ fontSize: 12.5 }}>{body}</span>
    </div>
  )
}

function WorkflowsTab({ chatId }: { chatId: number }) {
  const toast = useToast()
  const workflows = useQuery<ChatWorkflow[]>(
    () => api.get(`/api/chats/${chatId}/workflows`),
    [chatId],
    { pollMs: 3000 },
  )
  // Optimistic assignment state so the checkbox responds instantly;
  // reconciled from the server on every (re)fetch. A ref mirrors it so the
  // PUT body is computed synchronously \u2014 a setState updater's side-effect is
  // NOT available before the next line (React batches), which previously sent
  // an empty list and un-assigned everything.
  const [assignedIds, setAssignedIds] = useState<number[]>([])
  const assignedRef = useRef<number[]>([])
  const setAssigned = (next: number[]) => {
    assignedRef.current = next
    setAssignedIds(next)
  }
  const [expandedIds, setExpandedIds] = useState<number[]>([])
  useEffect(() => {
    if (workflows.data) {
      setAssigned(workflows.data.filter((w) => w.assigned).map((w) => w.id))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflows.data])

  async function toggle(wf: ChatWorkflow, on: boolean) {
    // Compute the full next set from the ref (latest committed state) so
    // concurrent toggles compose (the PUT replaces the whole list) and the
    // request body is correct without waiting on a state flush.
    const next = on
      ? [...assignedRef.current, wf.id]
      : assignedRef.current.filter((id) => id !== wf.id)
    setAssigned(next)
    try {
      await api.put(`/api/chats/${chatId}/workflows`, next)
      toast('ok', on ? `${wf.name} now watches this chat` : `${wf.name} no longer watches this chat`)
      void workflows.refetch()
    } catch (err) {
      setAssigned(
        on ? assignedRef.current.filter((id) => id !== wf.id) : [...assignedRef.current, wf.id],
      )
      toast('err', err instanceof ApiError ? err.message : 'Couldn\u2019t update the assignment')
    }
  }

  if (workflows.loading) return <CardSkeleton lines={4} />
  if (workflows.error)
    return <ErrorNote message={workflows.error} onRetry={() => void workflows.refetch()} />
  if ((workflows.data ?? []).length === 0) {
    return (
      <Card pad={false}>
        <EmptyState
          title="No workflows exist yet"
          hint="Create one on the Workflows page, then enable it for this chat here."
          action={<Link className="btn btn--quiet" to="/workflows">Open Workflows</Link>}
        />
      </Card>
    )
  }

  const active = workflows.data!.filter((wf) => assignedIds.includes(wf.id))
  const available = workflows.data!.filter((wf) => !assignedIds.includes(wf.id))

  const renderCard = (wf: ChatWorkflow) => {
    const assigned = assignedIds.includes(wf.id)
    const expanded = expandedIds.includes(wf.id)
    const toggleExpand = () =>
      setExpandedIds((ids) =>
        ids.includes(wf.id) ? ids.filter((i) => i !== wf.id) : [...ids, wf.id],
      )
    return (
      <Card key={wf.id}>
        {/* The Details button is the expand affordance — the card surface
            itself is inert, so no misleading pointer cursor. */}
        <div className="page-head-row">
          {/* Checkbox standalone — never wrapped in a <label> with other
              content, or a click bubbles to the label and re-fires on the
              input, toggling twice (spurious PUT, box won't stay checked). */}
          <div className="row" style={{ gap: 10 }}>
            <input
              type="checkbox"
              aria-label={`Enable ${wf.name} for this chat`}
              checked={assigned}
              onChange={(e) => void toggle(wf, e.target.checked)}
            />
            <h3 style={{ fontSize: 15, margin: 0 }}>
              <HoverCard content={<WorkflowPreview wf={wf} />}>
                <Link to={`/workflows/${wf.id}`} style={{ color: 'inherit' }}>
                  {wf.name}
                </Link>
              </HoverCard>
            </h3>
            <span className="pill pill--accent">
              <span className="lamp" aria-hidden />
              {wf.type}
            </span>
            {!wf.enabled && <StatusPill status="disabled" />}
          </div>
          <span className="row" style={{ gap: 8 }}>
            {assigned && wf.type === 'intent' && <IntentStatus wf={wf} />}
            {assigned && wf.type === 'intent' && wf.pending_messages > 0 && (
              <Chip label={`${wf.pending_messages} new`} tone="idle" />
            )}
            {assigned && (
              <button
                className="btn btn--quiet btn--sm"
                aria-expanded={expanded}
                onClick={toggleExpand}
              >
                {expanded ? 'Hide details \u25be' : 'Details \u25b8'}
              </button>
            )}
          </span>
        </div>
        {assigned && !expanded && (
          <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
            {wf.type === 'scheduled'
              ? `${wf.cron ?? ''} \u00b7 next ${wf.next_fire_at ? shortDateTime(wf.next_fire_at) : '\u2014'}`
              : stageStory(wf.cursors[0], wf.episodes, wf.threshold, wf.examples_status)}
            {wf.type === 'intent' && wf.pending_messages > 0 && (
              <span className="muted">
                {' \u00b7 '}
                {wf.pending_messages} new message{wf.pending_messages === 1 ? '' : 's'} waiting for
                the next check
              </span>
            )}
          </p>
        )}
        {assigned && expanded && <ExpandedWorkflow wf={wf} chatId={chatId} />}
      </Card>
    )
  }

  return (
    <div className="stack">
      <Blind title="Active" count={active.length} defaultOpen>
        {active.length === 0 ? (
          <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
            No workflows watch this chat yet. Expand “Available” below and tick one to activate it.
          </p>
        ) : (
          active.map(renderCard)
        )}
      </Blind>
      {available.length > 0 && (
        <Blind title="Available" count={available.length}>
          {available.map(renderCard)}
        </Blind>
      )}
    </div>
  )
}

/** Collapsible section; open state is local so toggling one blind never
    re-mounts the other's cards (their Details expansion must survive). */
function Blind({ title, count, defaultOpen = false, children }: {
  title: string
  count: number
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{
          background: 'none', border: 'none', padding: 0, cursor: 'pointer',
          font: 'inherit', color: 'inherit', display: 'flex', alignItems: 'center', gap: 8,
        }}
      >
        <span className="card-title" style={{ margin: 0 }}>{title}</span>
        <span className="muted" style={{ fontSize: 12 }}>{count}</span>
        <span className="muted">{open ? '▾' : '▸'}</span>
      </button>
      {open && <div className="stack" style={{ marginTop: 10 }}>{children}</div>}
    </section>
  )
}

function IntentStatus({ wf }: { wf: ChatWorkflow }) {
  // The agent running (post-fire) is the other live LLM call — surface it.
  if (wf.recent_runs[0]?.status === 'running') {
    return <Chip label="acting now" tone="accent" live />
  }
  return <Chip {...statusChip(wf.cursors, wf.episodes, wf.examples_status)} />
}

/** Glanceable config shown on hover over a workflow name. */
function WorkflowPreview({ wf }: { wf: ChatWorkflow }) {
  return (
    <span className="stack" style={{ gap: 7, fontSize: 12.5, display: 'flex' }}>
      <b style={{ fontSize: 13 }}>{wf.name}</b>
      <span>
        <span className="muted">watches for</span>{' '}
        {wf.type === 'scheduled' ? (
          <span className="mono">{wf.cron}</span>
        ) : (
          wf.trigger_prompt
        )}
      </span>
      <span>
        <span className="muted">then</span> {wf.action_prompt}
      </span>
      <span className="muted" style={{ fontSize: 12 }}>
        {wf.type === 'intent' ? `${dedupLabel(wf)}; ` : ''}
        {wf.confirm ? 'asks in the chat before acting' : 'acts without asking'}
      </span>
      <span className="accent" style={{ fontSize: 12 }}>Click to configure →</span>
    </span>
  )
}

interface ActivityEntry {
  key: string
  when: string
  status: string
  error: boolean
  // Full slots + outcome, one string; HoverText truncates it and reveals the
  // rest on hover.
  detail: string
  tools: ToolCall[] | null
}

/** A fire and the agent run it queued are ONE event — merge them. */
function mergeActivity(wf: ChatWorkflow): ActivityEntry[] {
  const runsById = new Map(wf.recent_runs.map((r) => [r.id, r]))
  const used = new Set<number>()
  const entries: ActivityEntry[] = wf.recent_fires.map((f) => {
    const run = f.agent_run_id != null ? runsById.get(f.agent_run_id) : undefined
    if (run) used.add(run.id)
    const parts: string[] = []
    const slots = Object.entries(f.slots)
      .map(([k, v]) => `${k}: ${v.value}`)
      .join(' \u00b7 ')
    if (slots) parts.push(slots)
    // Strip the Telegram HTML markup the agent's reply carries (<b>, <pre>, …)
    // so the activity preview reads as plain text — same as the Runs tab and the
    // run-only branch below.
    const outcome = run ? (run.error ?? run.response_text) : f.error
    if (outcome) parts.push(stripTags(outcome))
    return {
      key: `f${f.id}`,
      when: f.created_at,
      status: run && f.status === 'done' ? run.status : f.status,
      error: !!(run?.error ?? f.error),
      detail: parts.join(' · '),
      tools: run?.tool_calls ?? null,
    }
  })
  for (const r of wf.recent_runs) {
    if (used.has(r.id)) continue
    entries.push({
      key: `r${r.id}`,
      when: r.created_at,
      status: r.status,
      error: !!r.error,
      detail: stripTags(r.error ?? r.response_text ?? ''),
      tools: r.tool_calls ?? null,
    })
  }
  return entries.sort((a, b) => b.when.localeCompare(a.when))
}

/** The chat's live view of a workflow: what it's doing right now and what it's
    done here. Configuration lives on the workflow page (linked), not inline. */
function ExpandedWorkflow({ wf, chatId }: { wf: ChatWorkflow; chatId: number }) {
  const activity = mergeActivity(wf)
  const forum = wf.cursors.length > 1
  return (
    <div className="stack" style={{ gap: 14, marginTop: 12 }}>
      {wf.type === 'intent' &&
        (wf.cursors.length === 0 ? (
          <p className="muted" style={{ fontSize: 12.5 }}>
            {stageStory(undefined, wf.episodes, wf.threshold, wf.examples_status)}
          </p>
        ) : (
          wf.cursors.map((c) => (
            <div className="stack" key={c.thread_key} style={{ gap: 8 }}>
              {forum && (
                <div className="muted mono" style={{ fontSize: 11.5 }}>
                  thread {c.thread_key || 'main'}
                </div>
              )}
              <p style={{ fontSize: 13, margin: 0 }}>
                {stageStory(c, wf.episodes, wf.threshold, wf.examples_status)}
              </p>
              <Funnel
                steps={funnel(c, wf.episodes, wf.threshold, wf.required_slots, {
                  pending: wf.pending_messages > 0,
                  awaitingConfirm: wf.recent_fires[0]?.status === 'confirm_wait',
                })}
              />
              <dl className="kv">
                <dt>last check</dt>
                <dd>
                  {c.last_evaluated_at ? timeAgo(c.last_evaluated_at) : 'never'}
                  {wf.pending_messages > 0 && ` · ${wf.pending_messages} new waiting`}
                </dd>
              </dl>
            </div>
          ))
        ))}

      {wf.type === 'intent' && (
        <EpisodeList episodes={wf.episodes} requiredSlots={wf.required_slots} />
      )}

      {wf.type === 'scheduled' && (
        <dl className="kv">
          <dt>schedule</dt>
          <dd>
            {wf.cron} · next {wf.next_fire_at ? shortDateTime(wf.next_fire_at) : '—'}
          </dd>
        </dl>
      )}

      {activity.length > 0 && (
        <div>
          <div className="card-title">Activity in this chat</div>
          <table className="data">
            <tbody>
              {activity.map((a) => (
                <tr key={a.key}>
                  <td style={{ width: 100 }} className="mono muted">{timeAgo(a.when)}</td>
                  <td style={{ width: 170 }}>
                    <StatusPill status={a.status} />
                  </td>
                  <td className={a.error ? 'field-error' : 'muted'} style={{ fontSize: 12.5 }}>
                    <HoverText text={a.detail} max={110} />
                  </td>
                  <td className="toolcall-cell" style={{ width: 160 }}>
                    <ToolCalls calls={a.tools} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="muted" style={{ fontSize: 12 }}>
        {wf.type === 'intent' && (
          <>
            Timing follows this chat's <Link to={`/chats/${chatId}?tab=Settings`}>Settings</Link>.{' '}
          </>
        )}
        <Link to={`/workflows/${wf.id}`}>Configure this workflow →</Link>
      </p>
    </div>
  )
}

/* ---------------- Threads ---------------- */

function ThreadsTab({ chatId }: { chatId: number }) {
  const threads = useQuery<ChatThread[]>(() => api.get(`/api/chats/${chatId}/threads`), [chatId])
  const toast = useToast()
  const [expandedKeys, setExpandedKeys] = useState<number[]>([])
  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState('')

  async function save(tk: number, body: { monitored?: boolean; title?: string }) {
    try {
      await api.put(`/api/chats/${chatId}/threads/${tk}`, body)
      await threads.refetch()
    } catch (e) {
      toast('err', e instanceof ApiError ? e.message : 'Couldn’t save')
    }
  }

  if (threads.loading) return <CardSkeleton lines={4} />
  if (threads.error)
    return <ErrorNote message={threads.error} onRetry={() => void threads.refetch()} />
  const list = threads.data ?? []

  return (
    <div className="stack">
      <p className="muted tab-desc">
        Turn a thread off and Convoke ignores it completely — no workflows, nothing added to memory,
        and no replies, even to a direct mention. Names are yours to set, for display only.
      </p>
      {list.length === 0 ? (
        <Card>
          <EmptyState title="No threads yet" hint="Threads appear here as messages arrive." />
        </Card>
      ) : (
        list.map((t) => {
          const expanded = expandedKeys.includes(t.thread_key)
          const editingThis = editing === t.thread_key
          return (
            <Card key={t.thread_key}>
              <div className="page-head-row">
                <div className="row" style={{ gap: 10 }}>
                  <input
                    type="checkbox"
                    aria-label={`Monitor ${t.name}`}
                    checked={t.monitored}
                    onChange={(e) => void save(t.thread_key, { monitored: e.target.checked })}
                  />
                  <span className="thread-name-wrap">
                    {editingThis ? (
                      <input
                        className="thread-rename"
                        autoFocus
                        value={draft}
                        size={Math.max(4, (draft.length || t.default_name.length))}
                        placeholder={t.default_name}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            void save(t.thread_key, { title: draft })
                            setEditing(null)
                          }
                          if (e.key === 'Escape') setEditing(null)
                        }}
                        onBlur={() => {
                          void save(t.thread_key, { title: draft })
                          setEditing(null)
                        }}
                      />
                    ) : (
                      <h3 className="thread-name-h3">
                        <button
                          type="button"
                          className="name-edit"
                          title="Rename (display only)"
                          onClick={() => {
                            setEditing(t.thread_key)
                            setDraft(t.title ?? '')
                          }}
                        >
                          {t.name}
                        </button>
                      </h3>
                    )}
                  </span>
                  {!t.title && !editingThis && (
                    <span className="muted" style={{ fontSize: 11.5 }}>default name</span>
                  )}
                  {!t.monitored && <Chip label="ignored" tone="idle" />}
                </div>
                <span className="row" style={{ gap: 10 }}>
                  <span className="muted mono" style={{ fontSize: 11 }}>
                    {t.message_count} msg{t.message_count === 1 ? '' : 's'}
                    {t.last_activity ? ` · ${timeAgo(t.last_activity)}` : ''}
                  </span>
                  <button
                    className="btn btn--quiet btn--sm"
                    aria-expanded={expanded}
                    onClick={() =>
                      setExpandedKeys((ks) =>
                        ks.includes(t.thread_key)
                          ? ks.filter((k) => k !== t.thread_key)
                          : [...ks, t.thread_key],
                      )
                    }
                  >
                    {expanded ? 'Hide ▾' : 'Preview ▸'}
                  </button>
                </span>
              </div>
              {expanded && (
                <div className="thread-preview">
                  {t.preview.length === 0 ? (
                    <p className="muted" style={{ margin: 0, fontSize: 12.5 }}>No recent messages.</p>
                  ) : (
                    t.preview.map((m, i) => (
                      <div className="thread-preview-line" key={i}>
                        <b>{m.sender_name || 'unknown'}</b>{' '}
                        <span className="muted">
                          <HoverText text={m.text || '—'} max={110} />
                        </span>
                      </div>
                    ))
                  )}
                </div>
              )}
            </Card>
          )
        })
      )}
    </div>
  )
}

/* ---------------- Members ---------------- */

function MembersTab({ chatId }: { chatId: number }) {
  const members = useQuery<Member[]>(() => api.get(`/api/chats/${chatId}/members`), [chatId])
  const toast = useToast()
  // Staged overrides keyed by sender_id; nothing is written until Save ('' =
  // clear back to the auto name). `editing` is the one row whose input is open.
  const [edits, setEdits] = useState<Record<number, string>>({})
  const [botEdits, setBotEdits] = useState<Record<number, boolean>>({})
  const [editing, setEditing] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  const list = members.data ?? []
  const savedOverride = (m: Member) => m.override_name ?? ''
  const valueOf = (m: Member) => (m.sender_id in edits ? edits[m.sender_id] : savedOverride(m))
  const botOf = (m: Member) => (m.sender_id in botEdits ? botEdits[m.sender_id] : m.is_bot)
  const isNameChanged = (m: Member) =>
    m.sender_id in edits && edits[m.sender_id].trim() !== savedOverride(m)
  const isBotChanged = (m: Member) =>
    m.sender_id in botEdits && botEdits[m.sender_id] !== m.is_bot
  const isChanged = (m: Member) => isNameChanged(m) || isBotChanged(m)
  const dirty = list.filter(isChanged)
  const stage = (id: number, v: string) => setEdits((p) => ({ ...p, [id]: v }))
  const revert = (id: number) =>
    setEdits((p) => {
      const n = { ...p }
      delete n[id]
      return n
    })

  async function save() {
    if (!dirty.length) return
    setBusy(true)
    try {
      await api.put(
        `/api/chats/${chatId}/members`,
        dirty.map((m) => ({
          sender_id: m.sender_id,
          display_name: isNameChanged(m) ? edits[m.sender_id].trim() || null : m.override_name,
          ...(isBotChanged(m) ? { is_bot: botEdits[m.sender_id] } : {}),
        })),
      )
      toast('ok', `Saved ${dirty.length} — rebuilding memory under the new name${dirty.length > 1 ? 's' : ''}`)
      // Refetch first, THEN drop the local edits, so nothing flashes old values.
      await members.refetch()
      setEdits({})
      setBotEdits({})
    } catch (e) {
      toast('err', e instanceof ApiError ? e.message : 'Couldn’t save')
    } finally {
      setBusy(false)
    }
  }
  function cancel() {
    setEdits({})
    setBotEdits({})
    setEditing(null)
  }

  if (members.loading) return <TableSkeleton rows={5} />
  if (members.error)
    return <ErrorNote message={members.error} onRetry={() => void members.refetch()} />

  return (
    <div className="stack">
      <p className="muted tab-desc">
        How the bot refers to each person — in the conversation it reads, in its memory, and in its
        replies. The user id is fixed and the handle is filled in from Telegram when available. Click
        a name to rename it; edits apply only when you click Save — this chat’s memory then
        refreshes under the new names in the background, staying searchable throughout. Mark other
        bots (e.g. game bots from imported history) with the Bot checkbox: their messages get a
        [bot] tag and stop being scored by memory search, though they stay readable in results.
      </p>
      <Card pad={false}>
        {list.length === 0 ? (
          <div className="card-pad">
            <EmptyState title="No members yet" hint="Members appear here as messages arrive." />
          </div>
        ) : (
          <>
            <table className="data members-table">
              <thead>
                <tr>
                  <th>User ID</th>
                  <th>Handle</th>
                  <th>Display name</th>
                  <th title="Messages render tagged [bot] and are excluded from memory scoring">Bot</th>
                </tr>
              </thead>
              <tbody>
                {list.map((m) => {
                  const val = valueOf(m)
                  const overridden = val.trim().length > 0
                  const shownName = val.trim() || m.auto_name || 'Unknown'
                  return (
                    <tr
                      key={m.sender_id}
                      className={isChanged(m) ? 'member-row--changed' : undefined}
                    >
                      <td className="mono muted">{m.sender_id}</td>
                      <td className="mono">
                        {m.handle ? `@${m.handle}` : <span className="muted">—</span>}
                      </td>
                      <td>
                        {editing === m.sender_id ? (
                          // Auto-sizing input (hugs the text, resizes as you type),
                          // pulled left so the text sits where the name was.
                          <label className="name-field" data-value={val || m.auto_name || 'name'}>
                            <input
                              size={1}
                              autoFocus
                              value={val}
                              placeholder={m.auto_name || 'name'}
                              aria-label={`Display name for user ${m.sender_id}`}
                              onChange={(e) => stage(m.sender_id, e.target.value)}
                              onBlur={() => setEditing(null)}
                              onKeyDown={(e) => {
                                if (e.key === 'Enter') setEditing(null)
                                if (e.key === 'Escape') {
                                  revert(m.sender_id)
                                  setEditing(null)
                                }
                              }}
                            />
                          </label>
                        ) : (
                          <div className="member-edit">
                            <button
                              type="button"
                              className="member-name"
                              title="Click to rename"
                              onClick={() => setEditing(m.sender_id)}
                            >
                              {shownName}
                            </button>
                            {overridden && (
                              <button
                                type="button"
                                className="reset-btn"
                                title="Reset to the Telegram name"
                                aria-label="Reset to the Telegram name"
                                onClick={() => stage(m.sender_id, '')}
                              >
                                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                                  <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                                  <path d="M3 3v5h5" />
                                </svg>
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          checked={botOf(m)}
                          aria-label={`Treat user ${m.sender_id} as a bot`}
                          onChange={(e) =>
                            setBotEdits((p) => ({ ...p, [m.sender_id]: e.target.checked }))
                          }
                        />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <div className="settings-actions">
              <button
                className="btn btn--primary"
                disabled={busy || dirty.length === 0}
                onClick={() => void save()}
              >
                {busy ? 'Saving…' : dirty.length ? `Save ${dirty.length}` : 'Saved'}
              </button>
              {dirty.length > 0 && (
                <button type="button" className="btn btn--quiet" disabled={busy} onClick={cancel}>
                  Cancel
                </button>
              )}
            </div>
          </>
        )}
      </Card>
    </div>
  )
}

/* ---------------- Import ---------------- */

const ACTIVE_IMPORT = ['pending', 'validating', 'ingesting']

function ImportTab({ chatId }: { chatId: number }) {
  const toast = useToast()
  const confirm = useConfirm()
  const fileRef = useRef<HTMLInputElement>(null)
  const [uploading, setUploading] = useState(false)
  const jobs = useQuery<ImportJob[]>(() => api.get(`/api/chats/${chatId}/imports`), [chatId], {
    pollMs: 3000,
  })

  async function upload() {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      await api.upload(`/api/chats/${chatId}/import`, file)
      toast('ok', 'Upload received — validating the export now')
      if (fileRef.current) fileRef.current.value = ''
      void jobs.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  async function removeImport(job: ImportJob) {
    const ok = await confirm({
      title: 'Delete this import?',
      body: `Every message that ${job.filename} brought in is removed and memory is rebuilt without them.`,
      actionLabel: 'Delete import',
      danger: true,
    })
    if (!ok) return
    try {
      const result = await api.delete<{ deleted_messages: number }>(`/api/imports/${job.id}/messages`)
      toast('ok', `Deleted ${result.deleted_messages} imported messages`)
      void jobs.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t delete the import')
    }
  }

  return (
    <div className="stack">
      <Card title="Upload a Telegram export">
        <p className="muted" style={{ marginBottom: 12 }}>
          Bots can't read messages from before they joined — that history has to come from a chat
          export. Ask a chat admin to open the chat in <b>Telegram Desktop</b> → ⋯ →{' '}
          <b>Export chat history</b> → format <b>JSON</b>, then upload the <code>result.json</code>{' '}
          — or the whole export ZIP to include media — here. The file is checked against live
          history before anything is trusted.
        </p>
        <div className="row">
          <input ref={fileRef} type="file" accept=".json,.zip,application/json,application/zip" style={{ flex: '1 1 280px' }} />
          <button className="btn btn--primary" onClick={() => void upload()} disabled={uploading}>
            {uploading ? 'Uploading…' : 'Upload export'}
          </button>
        </div>
      </Card>

      <Card title="Imports" pad={false}>
        {jobs.loading ? (
          <TableSkeleton rows={2} />
        ) : (jobs.data ?? []).length === 0 ? (
          <EmptyState title="No imports yet" hint="Uploads and their validation results appear here." />
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>File</th>
                <th>Status</th>
                <th>Messages</th>
                <th>Notes</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.data!.map((j) => (
                <tr key={j.id}>
                  <td className="mono">{j.filename}</td>
                  <td>
                    <StatusPill status={j.status} live={ACTIVE_IMPORT.includes(j.status)} />
                  </td>
                  <td className="mono">
                    {j.messages_ingested}/{j.messages_total}
                  </td>
                  <td className="muted" style={{ maxWidth: 340 }}>{j.detail}</td>
                  <td style={{ textAlign: 'right' }}>
                    {/* A failed job can still hold committed partial rows — offer the same cleanup. */}
                    {(j.status === 'done' || (j.status === 'failed' && j.messages_ingested > 0)) && (
                      <button className="btn btn--danger btn--sm" onClick={() => void removeImport(j)}>
                        Delete import
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  )
}

/* ---------------- Tools ---------------- */

function ToolsTab({ chatId }: { chatId: number }) {
  const toast = useToast()
  const servers = useQuery<McpServer[]>(() => api.get('/api/mcp-servers'), [])
  const enabled = useQuery<number[]>(() => api.get(`/api/chats/${chatId}/mcp`), [chatId])
  // Local source of truth for the assignment set, reconciled from the server.
  // The PUT replaces the whole list, so we must never build it from a stale or
  // failed GET — that would wipe every other assignment.
  const [assigned, setAssigned] = useState<number[]>([])
  useEffect(() => {
    if (enabled.data) setAssigned(enabled.data)
  }, [enabled.data])

  async function toggle(serverId: number, on: boolean) {
    const previous = assigned
    const next = on ? [...previous, serverId] : previous.filter((i) => i !== serverId)
    setAssigned(next)
    try {
      await api.put(`/api/chats/${chatId}/mcp`, next)
    } catch (err) {
      setAssigned(previous)
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t update tools')
    }
  }

  if (servers.loading || enabled.loading) return <CardSkeleton lines={3} />
  if (servers.error || enabled.error)
    return (
      <ErrorNote
        message={servers.error ?? enabled.error!}
        onRetry={() => {
          void servers.refetch()
          void enabled.refetch()
        }}
      />
    )

  return (
    <Card title="Tools available to this chat's agent">
      {(servers.data ?? []).length === 0 ? (
        <EmptyState
          title="No MCP servers registered"
          hint="Register tool servers first, then choose which chats may use them."
          action={<Link className="btn btn--quiet" to="/tools">Open Tools</Link>}
        />
      ) : (
        <div className="stack" style={{ gap: 10 }}>
          {servers.data!.map((s) => (
            <Check
              key={s.id}
              checked={assigned.includes(s.id)}
              disabled={!s.enabled}
              onChange={(on) => void toggle(s.id, on)}
            >
              <b>{s.name}</b>{' '}
              <span className="muted mono" style={{ fontSize: 12 }}>
                {s.url ?? `${s.command} ${s.args.join(' ')}`}
              </span>
              {!s.enabled && <span className="muted"> (disabled globally)</span>}
            </Check>
          ))}
        </div>
      )}
    </Card>
  )
}

/* ---------------- Runs ---------------- */

function RunsTab({ chatId }: { chatId: number }) {
  const runs = useQuery<Run[]>(() => api.get(`/api/chats/${chatId}/runs?limit=25`), [chatId], {
    pollMs: 5000,
  })

  if (runs.loading)
    return (
      <Card pad={false}>
        <TableSkeleton rows={4} />
      </Card>
    )
  if (runs.error) return <ErrorNote message={runs.error} onRetry={() => void runs.refetch()} />

  return (
    <Card pad={false}>
      {(runs.data ?? []).length === 0 ? (
        <EmptyState
          title="The agent hasn't run here yet"
          hint="Mention the bot, reply to it, or let a workflow trigger."
        />
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>When</th>
              <th>Trigger</th>
              <th>Status</th>
              <th>Request</th>
              <th>Outcome</th>
              <th>Tool calls</th>
            </tr>
          </thead>
          <tbody>
            {runs.data!.map((r) => (
              <tr key={r.id}>
                <td className="mono muted">{timeAgo(r.created_at)}</td>
                <td className="mono">{r.trigger}</td>
                <td>
                  <StatusPill status={r.status} />
                </td>
                <td className="muted">
                  <HoverText text={stripTags(r.request_text)} max={70} />
                </td>
                <td className={r.error ? 'field-error' : 'muted'}>
                  <HoverText text={stripTags(r.error ?? r.response_text ?? '')} max={90} />
                </td>
                <td className="toolcall-cell">
                  <ToolCalls calls={r.tool_calls} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
