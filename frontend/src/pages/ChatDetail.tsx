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
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t update the assignment')
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

  return (
    <div className="stack">
      {workflows.data!.map((wf) => {
        const assigned = assignedIds.includes(wf.id)
        return (
        <Card key={wf.id}>
          <div className="page-head-row" style={{ marginBottom: assigned ? 12 : 0 }}>
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
            {assigned && wf.type === 'intent' && <StagePill wf={wf} />}
            {assigned && wf.type === 'scheduled' && (
              <span className="muted mono" style={{ fontSize: 12 }}>
                next {wf.next_fire_at ? shortDateTime(wf.next_fire_at) : '—'}
              </span>
            )}
          </div>
          {assigned && wf.type === 'intent' && <IntentStatePanel wf={wf} />}
          {assigned && wf.recent_runs.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div className="card-title">Recent runs in this chat</div>
              <ul className="messages">
                {wf.recent_runs.map((r) => (
                  <li key={r.id}>
                    <StatusPill status={r.status} />{' '}
                    <span className="ts">{timeAgo(r.created_at)}</span>
                    <br />
                    <span className={r.error ? 'field-error' : 'muted'}>
                      {truncate(r.error ?? r.response_text ?? '', 110)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
        )
      })}
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

function IntentStatePanel({ wf }: { wf: ChatWorkflow }) {
  const forum = wf.states.length > 1
  return (
    <div className="stack" style={{ gap: 10 }}>
      {wf.states.length === 0 && (
        <p className="muted" style={{ fontSize: 12.5 }}>
          Watching — no conversation window has been evaluated yet.
        </p>
      )}
      {wf.states.map((s) => (
        <div key={s.thread_key}>
          <dl className="kv">
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
                ` · match ${s.last_score.toFixed(2)}${wf.threshold ? ` / needs ${wf.threshold.toFixed(2)}` : ''}`}
              {s.last_confidence != null && ` · classifier confidence ${s.last_confidence.toFixed(2)}`}
            </dd>
            <dt>gathered</dt>
            <dd>
              {Object.keys(s.slots).length === 0
                ? `nothing yet (waiting for: ${wf.required_slots.map((r) => r.name).join(', ') || 'any match'})`
                : Object.entries(s.slots)
                    .map(([k, v]) => `${k}: ${v.value}`)
                    .join(' · ')}
            </dd>
            {s.cooldown_until && new Date(s.cooldown_until) > new Date() && (
              <>
                <dt>cooldown</dt>
                <dd>until {shortDateTime(s.cooldown_until)}</dd>
              </>
            )}
          </dl>
        </div>
      ))}
      {wf.recent_fires.length > 0 && (
        <div>
          <div className="card-title">Fires in this chat</div>
          <table className="data">
            <tbody>
              {wf.recent_fires.map((f) => (
                <tr key={f.id}>
                  <td style={{ width: 110 }} className="mono muted">{timeAgo(f.created_at)}</td>
                  <td style={{ width: 170 }}>
                    <StatusPill status={f.status} />
                  </td>
                  <td className="mono" style={{ fontSize: 12 }}>
                    {Object.entries(f.slots).map(([k, v]) => `${k}: ${v.value}`).join(' · ') || '—'}
                    {f.error && <div className="field-error">{f.error}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
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
