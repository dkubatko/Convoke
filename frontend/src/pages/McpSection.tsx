import { FormEvent, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

export interface McpServer {
  id: number
  name: string
  transport: string
  url: string | null
  command: string | null
  args: string[]
  has_headers: boolean
  enabled: boolean
}

export default function McpSection() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [name, setName] = useState('')
  const [transport, setTransport] = useState<'http' | 'stdio'>('http')
  const [url, setUrl] = useState('')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [bearer, setBearer] = useState('')
  const [error, setError] = useState<string | null>(null)

  const load = () => api.get<McpServer[]>('/api/mcp-servers').then(setServers).catch(() => {})
  useEffect(() => { load() }, [])

  async function add(e: FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      await api.post('/api/mcp-servers', {
        name,
        transport,
        url: transport === 'http' ? url : null,
        command: transport === 'stdio' ? command : null,
        args: transport === 'stdio' && args ? args.split(/\s+/) : [],
        headers: bearer ? { Authorization: `Bearer ${bearer}` } : null,
      })
      setName(''); setUrl(''); setCommand(''); setArgs(''); setBearer('')
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to add MCP server')
    }
  }

  async function remove(id: number) {
    if (!confirm('Remove this MCP server? Chats using it will lose its tools.')) return
    await api.delete(`/api/mcp-servers/${id}`)
    load()
  }

  return (
    <section>
      <h2>MCP servers</h2>
      <p className="hint">
        Tools the agent can use. Prefer streamable HTTP servers (run them as separate
        services); stdio commands must exist inside the backend container.
      </p>
      <form className="row" onSubmit={add}>
        <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} style={{ width: '120px' }} />
        <select value={transport} onChange={(e) => setTransport(e.target.value as 'http' | 'stdio')}>
          <option value="http">http</option>
          <option value="stdio">stdio</option>
        </select>
        {transport === 'http' ? (
          <>
            <input placeholder="URL (http://host:8000/mcp)" value={url} onChange={(e) => setUrl(e.target.value)} style={{ minWidth: '260px' }} />
            <input type="password" placeholder="Bearer token (optional)" value={bearer} onChange={(e) => setBearer(e.target.value)} />
          </>
        ) : (
          <>
            <input placeholder="Command" value={command} onChange={(e) => setCommand(e.target.value)} />
            <input placeholder="Args" value={args} onChange={(e) => setArgs(e.target.value)} />
          </>
        )}
        <button type="submit" disabled={!name || (transport === 'http' ? !url : !command)}>Add</button>
      </form>
      {error && <div className="error">{error}</div>}
      {servers.length > 0 && (
        <table>
          <thead><tr><th>Name</th><th>Transport</th><th>Target</th><th>Auth</th><th /></tr></thead>
          <tbody>
            {servers.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td>{s.transport}</td>
                <td className="detail">{s.url ?? `${s.command} ${s.args.join(' ')}`}</td>
                <td>{s.has_headers ? '🔑' : '—'}</td>
                <td><button className="danger" onClick={() => remove(s.id)}>Remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
