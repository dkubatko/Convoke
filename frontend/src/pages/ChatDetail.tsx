import { FormEvent, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api, ApiError } from '../lib/api'
import { shortDateTime, timeAgo, truncate } from '../lib/format'
import { Chat, Gap, ImportJob, McpServer, Message, Run, SearchHit } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  EmptyState,
  ErrorNote,
  LoadingWire,
  PageHead,
  StatusPill,
} from '../components/ui'

const TABS = ['Memory', 'Import history', 'Tools', 'Agent runs'] as const
type Tab = (typeof TABS)[number]

export default function ChatDetail() {
  const { chatId } = useParams()
  const id = Number(chatId)
  const [tab, setTab] = useState<Tab>('Memory')

  const chat = useQuery<Chat | undefined>(
    async () => (await api.get<Chat[]>('/api/chats')).find((c) => c.id === id),
    [id],
  )

  if (chat.loading) return <LoadingWire />
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
          <LoadingWire />
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
          <LoadingWire />
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

  if (servers.loading || enabled.loading) return <LoadingWire />

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

  if (runs.loading) return <LoadingWire />
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
