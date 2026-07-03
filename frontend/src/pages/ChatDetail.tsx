import { FormEvent, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { shortDateTime, timeAgo, truncate } from '../lib/format'
import {
  Chat,
  ChatWorkflow,
  Gap,
  ImportJob,
  McpServer,
  Message,
  Run,
  SearchHit,
  TriggerStateInfo,
} from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  CardSkeleton,
  EmptyState,
  ErrorNote,
  PageHead,
  StatusPill,
  TableSkeleton,
} from '../components/ui'

const TABS = ['Memory', 'Workflows', 'Import history', 'Tools', 'Agent runs'] as const
type Tab = (typeof TABS)[number]

export default function ChatDetail() {
  const { chatId } = useParams()
  const id = Number(chatId)
  const [tab, setTab] = useState<Tab>('Memory')

  const chat = useQuery<Chat | undefined>(
    async () => (await api.get<Chat[]>('/api/chats')).find((c) => c.id === id),
    [id],
  )

  if (chat.loading) return <CardSkeleton lines={5} />
  if (chat.error) return <ErrorNote message={chat.error} onRetry={() => void chat.refetch()} />
  if (!chat.data) {
    return (
      <EmptyState
        title="Chat not found"
        hint="It may have been removed along with its bot."
        action={<Link className="btn btn--quiet" to="/chats">Back to chats</Link>}
      />
    )
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
      <div className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            className={tab === t ? 'active' : ''}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === 'Memory' && <MemoryTab chatId={id} />}
      {tab === 'Workflows' && <WorkflowsTab chatId={id} />}
      {tab === 'Import history' && <ImportTab chatId={id} />}
      {tab === 'Tools' && <ToolsTab chatId={id} />}
      {tab === 'Agent runs' && <RunsTab chatId={id} />}
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
    const result = await api.post<{ deleted_messages: number }>(`/api/chats/${chatId}/forget`, body)
    toast('ok', `Forgot ${result.deleted_messages} message${result.deleted_messages === 1 ? '' : 's'}`)
    setForgetSender('')
    void messages.refetch()
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
              hits.map((h) => (
                <pre key={h.chunk_id} className="transcript">
                  <span className="muted">match {(1 - h.distance).toFixed(2)}</span>
                  {'\n'}
                  {h.rendered}
                </pre>
              ))
            )}
          </div>
        )}
      </Card>

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
                  <td>{m.text}</td>
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
            placeholder="Sender id"
            value={forgetSender}
            onChange={(e) => setForgetSender(e.target.value)}
          />
          <button
            className="btn btn--quiet"
            disabled={!forgetSender}
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

function WorkflowsTab({ chatId }: { chatId: number }) {
  const toast = useToast()
  const workflows = useQuery<ChatWorkflow[]>(
    () => api.get(`/api/chats/${chatId}/workflows`),
    [chatId],
    { pollMs: 5000 },
  )
  // Optimistic assignment state so the checkbox responds instantly;
  // reconciled from the server on every (re)fetch.
  const [assignedIds, setAssignedIds] = useState<number[]>([])
  const [expandedIds, setExpandedIds] = useState<number[]>([])
  useEffect(() => {
    if (workflows.data) {
      setAssignedIds(workflows.data.filter((w) => w.assigned).map((w) => w.id))
    }
  }, [workflows.data])

  async function toggle(wf: ChatWorkflow, on: boolean) {
    const previous = assignedIds
    const next = on ? [...previous, wf.id] : previous.filter((id) => id !== wf.id)
    setAssignedIds(next)
    try {
      await api.put(`/api/chats/${chatId}/workflows`, next)
      toast('ok', on ? `${wf.name} now watches this chat` : `${wf.name} no longer watches this chat`)
      void workflows.refetch()
    } catch (err) {
      setAssignedIds(previous)
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

  const pending = workflows.data![0]?.pending_messages ?? 0
  return (
    <div className="stack">
      {pending > 0 && (
        <p className="row" style={{ gap: 8 }}>
          <span className="pill pill--accent pill--live">
            <span className="lamp" aria-hidden />
            {pending} message{pending === 1 ? '' : 's'} waiting
          </span>
          <span className="muted" style={{ fontSize: 12.5 }}>
            evaluated ~1 minute after the chat goes quiet
          </span>
        </p>
      )}
      {workflows.data!.map((wf) => {
        const assigned = assignedIds.includes(wf.id)
        const expanded = expandedIds.includes(wf.id)
        return (
          <Card key={wf.id}>
            <div className="page-head-row">
              <label className="row" style={{ gap: 10 }}>
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  checked={assigned}
                  onChange={(e) => void toggle(wf, e.target.checked)}
                />
                <h3 style={{ fontSize: 15 }}>{wf.name}</h3>
                <span className="pill pill--accent">
                  <span className="lamp" aria-hidden />
                  {wf.type}
                </span>
                {!wf.enabled && <StatusPill status="disabled" />}
              </label>
              <span className="row" style={{ gap: 10 }}>
                {assigned && wf.type === 'intent' && <StagePill wf={wf} />}
                {assigned && (
                  <button
                    className="btn btn--quiet btn--sm"
                    aria-expanded={expanded}
                    onClick={() =>
                      setExpandedIds((ids) =>
                        expanded ? ids.filter((i) => i !== wf.id) : [...ids, wf.id],
                      )
                    }
                  >
                    {expanded ? 'Hide details \u25be' : 'Details \u25b8'}
                  </button>
                )}
              </span>
            </div>
            {assigned && !expanded && (
              <p className="muted mono" style={{ fontSize: 12, marginTop: 8 }}>
                {compactSummary(wf)}
              </p>
            )}
            {assigned && expanded && <ExpandedWorkflow wf={wf} />}
          </Card>
        )
      })}
    </div>
  )
}

function cooldownActive(s: TriggerStateInfo | undefined): boolean {
  return !!s?.cooldown_until && new Date(s.cooldown_until) > new Date()
}

/** One quiet line for the collapsed card — the essentials, no sections. */
function compactSummary(wf: ChatWorkflow): string {
  if (wf.type === 'scheduled') {
    return `${wf.cron ?? ''} \u00b7 next ${wf.next_fire_at ? shortDateTime(wf.next_fire_at) : '\u2014'}`
  }
  const s = wf.states[0]
  if (!s || !s.last_evaluated_at) {
    return wf.examples_status === 'pending'
      ? 'calibrating detector\u2026'
      : 'watching \u2014 nothing evaluated yet'
  }
  const parts = [`last check ${timeAgo(s.last_evaluated_at)}`]
  const gathered = Object.entries(s.slots)
    .map(([k, v]) => `${k}: ${v.value}`)
    .join(', ')
  if (gathered) parts.push(`gathered ${gathered}`)
  if (cooldownActive(s)) parts.push(`cooldown until ${shortDateTime(s.cooldown_until!)}`)
  const fires = wf.recent_fires.length
  if (fires) parts.push(`${fires} fire${fires === 1 ? '' : 's'}`)
  return parts.join(' \u00b7 ')
}

interface ActivityEntry {
  key: string
  when: string
  status: string
  error: boolean
  detail: string[]
}

/** A fire and the agent run it queued are ONE event — merge them. */
function mergeActivity(wf: ChatWorkflow): ActivityEntry[] {
  const runsById = new Map(wf.recent_runs.map((r) => [r.id, r]))
  const used = new Set<number>()
  const entries: ActivityEntry[] = wf.recent_fires.map((f) => {
    const run = f.agent_run_id != null ? runsById.get(f.agent_run_id) : undefined
    if (run) used.add(run.id)
    const detail: string[] = []
    const slots = Object.entries(f.slots)
      .map(([k, v]) => `${k}: ${v.value}`)
      .join(' \u00b7 ')
    if (slots) detail.push(slots)
    const outcome = run ? (run.error ?? run.response_text) : f.error
    if (outcome) detail.push(truncate(outcome, 120))
    return {
      key: `f${f.id}`,
      when: f.created_at,
      status: run && f.status === 'done' ? run.status : f.status,
      error: !!(run?.error ?? f.error),
      detail,
    }
  })
  for (const r of wf.recent_runs) {
    if (used.has(r.id)) continue
    entries.push({
      key: `r${r.id}`,
      when: r.created_at,
      status: r.status,
      error: !!r.error,
      detail: [truncate(r.error ?? r.response_text ?? '', 120)].filter(Boolean),
    })
  }
  return entries.sort((a, b) => b.when.localeCompare(a.when))
}

function ExpandedWorkflow({ wf }: { wf: ChatWorkflow }) {
  const activity = mergeActivity(wf)
  const forum = wf.states.length > 1
  return (
    <div className="stack" style={{ gap: 12, marginTop: 12 }}>
      {wf.type === 'intent' && wf.states.length === 0 && (
        <p className="muted" style={{ fontSize: 12.5 }}>
          Watching — no conversation window has been evaluated yet.
        </p>
      )}
      {wf.type === 'intent' &&
        wf.states.map((s) => (
          <dl className="kv" key={s.thread_key}>
            {forum && (
              <>
                <dt>thread</dt>
                <dd>{s.thread_key || 'main'}</dd>
              </>
            )}
            <dt>last check</dt>
            <dd>
              {s.last_evaluated_at ? timeAgo(s.last_evaluated_at) : 'never'}
              {s.last_score != null &&
                ` \u00b7 match ${s.last_score.toFixed(2)}${wf.threshold ? ` / needs ${wf.threshold.toFixed(2)}` : ''}`}
              {s.last_confidence != null && ` \u00b7 classifier confidence ${s.last_confidence.toFixed(2)}`}
            </dd>
            <dt>gathered</dt>
            <dd>
              {Object.keys(s.slots).length === 0
                ? `nothing yet (waiting for: ${wf.required_slots.map((r) => r.name).join(', ') || 'any match'})`
                : Object.entries(s.slots)
                    .map(([k, v]) => `${k}: ${v.value}`)
                    .join(' \u00b7 ')}
            </dd>
            {cooldownActive(s) && (
              <>
                <dt>cooldown</dt>
                <dd>until {shortDateTime(s.cooldown_until!)}</dd>
              </>
            )}
          </dl>
        ))}
      {wf.type === 'scheduled' && (
        <dl className="kv">
          <dt>schedule</dt>
          <dd>
            {wf.cron} · next {wf.next_fire_at ? shortDateTime(wf.next_fire_at) : '\u2014'}
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
                    {a.detail.join('\u2002\u00b7\u2002') || '\u2014'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {wf.type === 'intent' && (
        <p className="muted" style={{ fontSize: 12 }}>
          Windows are evaluated ~1 minute after new messages stop (or every 30 messages);
          “last check” moves only when there was something new to evaluate.
        </p>
      )}
    </div>
  )
}

function StagePill({ wf }: { wf: ChatWorkflow }) {
  const state = wf.states[0]
  if (!state || !state.last_stage) {
    return (
      <span className="pill pill--idle">
        <span className="lamp" aria-hidden />
        {wf.examples_status === 'pending' ? 'calibrating detector' : 'no activity yet'}
      </span>
    )
  }
  return <StatusPill status={state.last_stage} live={state.last_stage === 'accumulating'} />
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
    const result = await api.delete<{ deleted_messages: number }>(`/api/imports/${job.id}/messages`)
    toast('ok', `Deleted ${result.deleted_messages} imported messages`)
    void jobs.refetch()
  }

  return (
    <div className="stack">
      <Card title="Upload a Telegram export">
        <p className="muted" style={{ marginBottom: 12 }}>
          Bots can't read messages from before they joined — that history has to come from a chat
          export. Ask a chat admin to open the chat in <b>Telegram Desktop</b> → ⋯ →{' '}
          <b>Export chat history</b> → format <b>JSON</b>, then upload the <code>result.json</code>{' '}
          here. The file is checked against live history before anything is trusted.
        </p>
        <div className="row">
          <input ref={fileRef} type="file" accept=".json,application/json" style={{ flex: '1 1 280px' }} />
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
                    {j.status === 'done' && (
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

  async function toggle(serverId: number, on: boolean) {
    const current = enabled.data ?? []
    const next = on ? [...current, serverId] : current.filter((i) => i !== serverId)
    try {
      await api.put(`/api/chats/${chatId}/mcp`, next)
      void enabled.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t update tools')
    }
  }

  if (servers.loading || enabled.loading) return <CardSkeleton lines={3} />

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
            <label key={s.id} className="row" style={{ gap: 10 }}>
              <input
                type="checkbox"
                style={{ width: 'auto' }}
                checked={(enabled.data ?? []).includes(s.id)}
                onChange={(e) => void toggle(s.id, e.target.checked)}
                disabled={!s.enabled}
              />
              <span>
                <b>{s.name}</b>{' '}
                <span className="muted mono" style={{ fontSize: 12 }}>
                  {s.url ?? `${s.command} ${s.args.join(' ')}`}
                </span>
                {!s.enabled && <span className="muted"> (disabled globally)</span>}
              </span>
            </label>
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
                <td className="muted">{truncate(r.request_text, 70)}</td>
                <td className={r.error ? 'field-error' : 'muted'}>
                  {truncate(r.error ?? r.response_text ?? '', 90)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
