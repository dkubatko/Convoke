import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { McpServer } from './McpSection'

interface Msg {
  id: number
  sender_name: string
  text: string
  sent_at: string
  source: string
}

interface Hit {
  chunk_id: number
  distance: number
  rendered: string
}

interface ImportJob {
  id: number
  filename: string
  status: string
  detail: string | null
  messages_total: number
  messages_ingested: number
}

interface Gap {
  id: number
  gap_start: string
  gap_end: string
}

interface Run {
  id: number
  trigger: string
  status: string
  request_text: string
  response_text: string | null
  error: string | null
  created_at: string
}

const ACTIVE_STATUSES = ['pending', 'validating', 'ingesting']

export default function ChatPanel({ chatId, title }: { chatId: number; title: string }) {
  const [messages, setMessages] = useState<Msg[]>([])
  const [jobs, setJobs] = useState<ImportJob[]>([])
  const [runs, setRuns] = useState<Run[]>([])
  const [allServers, setAllServers] = useState<McpServer[]>([])
  const [enabledServers, setEnabledServers] = useState<number[]>([])
  const [gaps, setGaps] = useState<Gap[]>([])
  const [forgetSender, setForgetSender] = useState('')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const refresh = useCallback(() => {
    api.get<Msg[]>(`/api/chats/${chatId}/messages?limit=20`).then(setMessages).catch(() => {})
    api.get<ImportJob[]>(`/api/chats/${chatId}/imports`).then(setJobs).catch(() => {})
    api.get<Run[]>(`/api/chats/${chatId}/runs?limit=10`).then(setRuns).catch(() => {})
    api.get<McpServer[]>('/api/mcp-servers').then(setAllServers).catch(() => {})
    api.get<number[]>(`/api/chats/${chatId}/mcp`).then(setEnabledServers).catch(() => {})
    api.get<Gap[]>(`/api/chats/${chatId}/gaps`).then(setGaps).catch(() => {})
  }, [chatId])

  useEffect(() => {
    setHits(null)
    setError(null)
    refresh()
  }, [refresh])

  // poll while an import is running
  useEffect(() => {
    if (!jobs.some((j) => ACTIVE_STATUSES.includes(j.status))) return
    const t = setInterval(refresh, 2000)
    return () => clearInterval(t)
  }, [jobs, refresh])

  async function search() {
    setSearching(true)
    setError(null)
    try {
      setHits(await api.get<Hit[]>(`/api/chats/${chatId}/search?q=${encodeURIComponent(query)}`))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Search failed')
    } finally {
      setSearching(false)
    }
  }

  async function upload() {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setError(null)
    try {
      await api.upload(`/api/chats/${chatId}/import`, file)
      if (fileRef.current) fileRef.current.value = ''
      refresh()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Upload failed')
    }
  }

  async function removeImport(jobId: number) {
    if (!confirm('Delete all messages this import brought in?')) return
    await api.delete(`/api/imports/${jobId}/messages`)
    refresh()
  }

  return (
    <div className="chat-panel">
      <h3>{title}</h3>
      {error && <div className="error">{error}</div>}

      <h4>Semantic search</h4>
      <div className="row">
        <input
          placeholder="What was said about…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && query && search()}
          style={{ minWidth: '300px' }}
        />
        <button disabled={searching || !query} onClick={search}>
          {searching ? 'Searching…' : 'Search'}
        </button>
      </div>
      {hits !== null &&
        (hits.length === 0 ? (
          <p>No indexed history yet (chunks embed shortly after conversations go quiet).</p>
        ) : (
          hits.map((h) => (
            <pre key={h.chunk_id} className="hit">
              <span className="dist">relevance {(1 - h.distance).toFixed(2)}</span>
              {'\n'}
              {h.rendered}
            </pre>
          ))
        ))}

      <h4>History import</h4>
      <p className="hint">
        Ask a chat admin for a Telegram Desktop export (⋯ → Export chat history → Format:
        <b> JSON</b>) and upload the <code>result.json</code> here.
      </p>
      <div className="row">
        <input ref={fileRef} type="file" accept=".json,application/json" />
        <button onClick={upload}>Upload</button>
      </div>
      {jobs.length > 0 && (
        <table>
          <thead>
            <tr><th>File</th><th>Status</th><th>Progress</th><th>Detail</th><th /></tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.id}>
                <td>{j.filename}</td>
                <td>{j.status}</td>
                <td>{j.messages_ingested}/{j.messages_total}</td>
                <td className="detail">{j.detail}</td>
                <td>
                  {j.status === 'done' && (
                    <button className="danger" onClick={() => removeImport(j.id)}>
                      Delete import
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {allServers.length > 0 && (
        <>
          <h4>MCP tools for this chat</h4>
          <div className="row" style={{ flexWrap: 'wrap' }}>
            {allServers.map((s) => (
              <label key={s.id} style={{ marginRight: '1rem' }}>
                <input
                  type="checkbox"
                  checked={enabledServers.includes(s.id)}
                  onChange={async (e) => {
                    const next = e.target.checked
                      ? [...enabledServers, s.id]
                      : enabledServers.filter((id) => id !== s.id)
                    setEnabledServers(next)
                    await api.put(`/api/chats/${chatId}/mcp`, next)
                  }}
                />{' '}
                {s.name}
              </label>
            ))}
          </div>
        </>
      )}

      {runs.length > 0 && (
        <>
          <h4>Agent runs</h4>
          <table>
            <thead>
              <tr><th>When</th><th>Trigger</th><th>Status</th><th>Request</th><th>Response / error</th></tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td>{new Date(r.created_at).toLocaleTimeString()}</td>
                  <td>{r.trigger}</td>
                  <td>{r.status}</td>
                  <td className="detail">{r.request_text.slice(0, 80)}</td>
                  <td className="detail">{(r.error ?? r.response_text ?? '').slice(0, 120)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {gaps.length > 0 && (
        <>
          <h4>Memory gaps</h4>
          <p className="warn">
            Convoke was offline longer than Telegram keeps updates (24h) — messages in these
            ranges were never captured:
          </p>
          <ul>
            {gaps.map((g) => (
              <li key={g.id}>
                {new Date(g.gap_start).toLocaleString()} → {new Date(g.gap_end).toLocaleString()}
              </li>
            ))}
          </ul>
        </>
      )}

      <h4>Forget</h4>
      <p className="hint">
        Telegram never tells bots about deleted messages — remove stored content here.
      </p>
      <div className="row">
        <input
          placeholder="Sender id"
          value={forgetSender}
          onChange={(e) => setForgetSender(e.target.value)}
          style={{ width: '140px' }}
        />
        <button
          disabled={!forgetSender}
          onClick={async () => {
            if (!confirm(`Forget all stored messages from sender ${forgetSender}?`)) return
            await api.post(`/api/chats/${chatId}/forget`, { sender_id: Number(forgetSender) })
            setForgetSender('')
            refresh()
          }}
        >
          Forget sender
        </button>
        <button
          className="danger"
          onClick={async () => {
            if (!confirm('Forget EVERYTHING stored for this chat (messages, memory, notes)?')) return
            await api.post(`/api/chats/${chatId}/forget`, { everything: true })
            refresh()
          }}
        >
          Forget entire chat
        </button>
      </div>

      <h4>Recent messages</h4>
      {messages.length === 0 ? (
        <p>Nothing stored yet.</p>
      ) : (
        <ul className="messages">
          {messages.map((m) => (
            <li key={m.id}>
              <b>{m.sender_name || '—'}</b>
              <span className="ts"> {new Date(m.sent_at).toLocaleString()} ({m.source})</span>
              <br />
              {m.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
