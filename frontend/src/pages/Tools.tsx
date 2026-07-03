import { FormEvent, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { McpServer } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  EmptyState,
  ErrorNote,
  Field,
  PageHead,
  TableSkeleton,
} from '../components/ui'

export default function Tools() {
  const servers = useQuery<McpServer[]>(() => api.get('/api/mcp-servers'), [])
  const toast = useToast()
  const confirm = useConfirm()

  const [name, setName] = useState('')
  const [transport, setTransport] = useState<'http' | 'stdio'>('http')
  const [url, setUrl] = useState('')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [bearer, setBearer] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function add(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
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
      toast('ok', `Registered ${name} — enable it per chat from the chat's Tools tab`)
      setName(''); setUrl(''); setCommand(''); setArgs(''); setBearer('')
      void servers.refetch()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Couldn’t register the server')
    } finally {
      setBusy(false)
    }
  }

  async function remove(server: McpServer) {
    const ok = await confirm({
      title: `Remove ${server.name}?`,
      body: 'Chats using this server lose its tools on their next agent run.',
      actionLabel: 'Remove server',
      danger: true,
    })
    if (!ok) return
    await api.delete(`/api/mcp-servers/${server.id}`)
    toast('ok', `Removed ${server.name}`)
    void servers.refetch()
  }

  return (
    <>
      <PageHead
        title="Tools"
        lede="MCP servers give your agents hands: calendars, files, tickets. Register them here, then switch them on per chat."
      />
      <div className="stack">
        <Card title="Register an MCP server">
          <form className="stack" style={{ gap: 14 }} onSubmit={add}>
            <div className="grid-2">
              <Field label="Name">
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Calendar" />
              </Field>
              <Field
                label="Transport"
                hint={
                  transport === 'http'
                    ? 'Preferred — run the server as its own service.'
                    : 'The command must exist inside the backend container.'
                }
              >
                <select value={transport} onChange={(e) => setTransport(e.target.value as 'http' | 'stdio')}>
                  <option value="http">Streamable HTTP</option>
                  <option value="stdio">stdio (local command)</option>
                </select>
              </Field>
            </div>
            {transport === 'http' ? (
              <div className="grid-2">
                <Field label="URL">
                  <input
                    className="mono"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    placeholder="http://calendar:8000/mcp"
                  />
                </Field>
                <Field label="Bearer token" hint="Optional. Stored encrypted.">
                  <input type="password" value={bearer} onChange={(e) => setBearer(e.target.value)} />
                </Field>
              </div>
            ) : (
              <div className="grid-2">
                <Field label="Command">
                  <input className="mono" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="mcp-filesystem" />
                </Field>
                <Field label="Arguments">
                  <input className="mono" value={args} onChange={(e) => setArgs(e.target.value)} placeholder="--root /data" />
                </Field>
              </div>
            )}
            {error && <p className="field-error">{error}</p>}
            <div className="row">
              <button
                className="btn btn--primary"
                disabled={busy || !name || (transport === 'http' ? !url : !command)}
              >
                {busy ? 'Registering…' : 'Register server'}
              </button>
            </div>
          </form>
        </Card>

        <Card pad={false}>
          {servers.loading ? (
            <TableSkeleton rows={2} />
          ) : servers.error ? (
            <ErrorNote message={servers.error} onRetry={() => void servers.refetch()} />
          ) : (servers.data ?? []).length === 0 ? (
            <EmptyState
              title="No tool servers yet"
              hint="Without tools the agent can still talk and remember — tools let it act."
            />
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Transport</th>
                  <th>Target</th>
                  <th>Auth</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {servers.data!.map((s) => (
                  <tr key={s.id}>
                    <td>
                      <b>{s.name}</b>
                    </td>
                    <td className="mono">{s.transport}</td>
                    <td className="mono muted">{s.url ?? `${s.command} ${s.args.join(' ')}`}</td>
                    <td className="muted">{s.has_headers ? 'bearer token' : 'none'}</td>
                    <td style={{ textAlign: 'right' }}>
                      <button className="btn btn--danger btn--sm" onClick={() => void remove(s)}>
                        Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </>
  )
}
