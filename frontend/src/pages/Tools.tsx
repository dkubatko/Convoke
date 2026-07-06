import { FormEvent, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { McpServer } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import { Select } from '../components/Select'
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
  const [auth, setAuth] = useState<'none' | 'bearer' | 'oauth'>('none')
  const [bearer, setBearer] = useState('')
  const [oauthClientId, setOauthClientId] = useState('')
  const [oauthClientSecret, setOauthClientSecret] = useState('')
  const [oauthScopes, setOauthScopes] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [test, setTest] = useState<
    { phase: 'idle' } | { phase: 'testing' } | { phase: 'ok'; detail: string } | { phase: 'failed'; detail: string }
  >({ phase: 'idle' })

  // Any edit invalidates the previous probe result.
  function edit<T>(setter: (v: T) => void) {
    return (value: T) => {
      setter(value)
      setTest({ phase: 'idle' })
    }
  }

  async function runTest() {
    setTest({ phase: 'testing' })
    try {
      const result = await api.post<{ ok: boolean; detail: string }>('/api/mcp-servers/test', {
        transport,
        url: transport === 'http' ? url : null,
        command: transport === 'stdio' ? command : null,
        args: transport === 'stdio' && args ? args.split(/\s+/) : [],
        bearer: auth === 'bearer' && bearer ? bearer : null,
      })
      setTest(result.ok ? { phase: 'ok', detail: result.detail } : { phase: 'failed', detail: result.detail })
    } catch (err) {
      setTest({ phase: 'failed', detail: err instanceof ApiError ? err.message : 'The backend didn’t respond.' })
    }
  }

  /** Opens the provider sign-in and waits for the callback to land (2 min cap). */
  async function awaitSignIn(serverId: number, authorizeUrl: string, serverName: string) {
    const popup = window.open(authorizeUrl, '_blank')
    if (!popup) {
      toast('err', 'The sign-in window was blocked — allow pop-ups and press Connect.')
      return
    }
    toast('info', `Complete the ${serverName} sign-in in the new tab…`)
    const deadline = Date.now() + 120_000
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 3000))
      try {
        const list = await api.get<McpServer[]>('/api/mcp-servers')
        const s = list.find((x) => x.id === serverId)
        if (!s) return
        if (s.oauth_status === 'connected') {
          toast('ok', `${serverName} connected — enable it and pick chats that may use it`)
          void servers.refetch()
          return
        }
        if (s.oauth_status === 'error') {
          toast('err', s.oauth_error ?? `${serverName} sign-in failed`)
          void servers.refetch()
          return
        }
      } catch {
        // transient — keep polling
      }
    }
    toast('err', `Sign-in not completed — ${serverName} stays unconnected. Press Connect to retry.`)
    void servers.refetch()
  }

  async function add(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const created = await api.post<McpServer & { authorize_url: string | null }>('/api/mcp-servers', {
        name,
        transport,
        url: transport === 'http' ? url : null,
        command: transport === 'stdio' ? command : null,
        args: transport === 'stdio' && args ? args.split(/\s+/) : [],
        headers: auth === 'bearer' && bearer ? { Authorization: `Bearer ${bearer}` } : null,
        auth_type: transport === 'http' && auth === 'oauth' ? 'oauth' : 'none',
        oauth_client_id: oauthClientId || null,
        oauth_client_secret: oauthClientSecret || null,
        oauth_scopes: oauthScopes || null,
      })
      setName(''); setUrl(''); setCommand(''); setArgs(''); setBearer('')
      setOauthClientId(''); setOauthClientSecret(''); setOauthScopes('')
      setTest({ phase: 'idle' })
      void servers.refetch()
      if (created.auth_type === 'oauth') {
        if (created.authorize_url) {
          await awaitSignIn(created.id, created.authorize_url, created.name)
        } else {
          toast('err', created.oauth_error ?? 'OAuth setup failed — see the server row for details')
        }
      } else {
        toast('ok', `Registered ${name} — enable it per chat from the chat's Tools tab`)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Couldn’t register the server')
    } finally {
      setBusy(false)
    }
  }

  async function connect(server: McpServer) {
    try {
      const res = await api.post<McpServer & { authorize_url: string | null }>(
        `/api/mcp-servers/${server.id}/connect`,
      )
      if (res.authorize_url) await awaitSignIn(server.id, res.authorize_url, server.name)
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t start the sign-in')
    }
  }

  async function testRegistered(server: McpServer) {
    toast('info', `Testing ${server.name}…`)
    try {
      const result = await api.post<{ ok: boolean; detail: string }>(
        `/api/mcp-servers/${server.id}/test`,
      )
      toast(result.ok ? 'ok' : 'err', `${server.name}: ${result.detail}`)
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : `Couldn’t test ${server.name}`)
    }
  }

  async function toggleEnabled(server: McpServer) {
    try {
      await api.put(`/api/mcp-servers/${server.id}`, {
        name: server.name,
        transport: server.transport,
        url: server.url,
        command: server.command,
        args: server.args,
        headers: null, // keep whatever is stored
        enabled: !server.enabled,
      })
      toast('ok', `${server.name} ${server.enabled ? 'disabled' : 'enabled'}`)
      void servers.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t update the server')
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
                <Select
                  value={transport}
                  ariaLabel="Transport"
                  onChange={(v) => edit(setTransport)(v as 'http' | 'stdio')}
                  options={[
                    { value: 'http', label: 'Streamable HTTP' },
                    { value: 'stdio', label: 'stdio (local command)' },
                  ]}
                />
              </Field>
            </div>
            {transport === 'http' ? (
              <>
                <div className="grid-2">
                  <Field label="URL">
                    <input
                      className="mono"
                      value={url}
                      onChange={(e) => edit(setUrl)(e.target.value)}
                      placeholder="http://calendar:8000/mcp"
                    />
                  </Field>
                  <Field
                    label="Authentication"
                    hint={
                      auth === 'oauth'
                        ? 'You sign in once in the browser; Convoke keeps refreshed tokens (encrypted).'
                        : auth === 'bearer'
                          ? 'A static token sent with every request. Stored encrypted.'
                          : 'For open servers that need no credentials.'
                    }
                  >
                    <Select
                      value={auth}
                      ariaLabel="Authentication"
                      onChange={(v) => edit(setAuth)(v as typeof auth)}
                      options={[
                        { value: 'none', label: 'None' },
                        { value: 'bearer', label: 'Bearer token' },
                        { value: 'oauth', label: 'OAuth sign-in' },
                      ]}
                    />
                  </Field>
                </div>
                {auth === 'bearer' && (
                  <Field label="Bearer token">
                    <input type="password" value={bearer} onChange={(e) => edit(setBearer)(e.target.value)} />
                  </Field>
                )}
                {auth === 'oauth' && (
                  <>
                    <div className="grid-2">
                      <Field
                        label="Client id"
                        hint={
                          <>
                            Leave blank first — most servers register Convoke automatically.
                            <br />
                            Only needed for providers like Google.
                          </>
                        }
                      >
                        <input className="mono" value={oauthClientId} onChange={(e) => setOauthClientId(e.target.value)} />
                      </Field>
                      <Field label="Client secret" hint="Paired with the client id, if the provider issued one.">
                        <input type="password" value={oauthClientSecret} onChange={(e) => setOauthClientSecret(e.target.value)} />
                      </Field>
                    </div>
                    <Field label="Scopes" hint="Optional, space-separated. Defaults to what the provider advertises.">
                      <input className="mono" value={oauthScopes} onChange={(e) => setOauthScopes(e.target.value)} placeholder="https://www.googleapis.com/auth/calendar.events" />
                    </Field>
                  </>
                )}
              </>
            ) : (
              <div className="grid-2">
                <Field label="Command">
                  <input className="mono" value={command} onChange={(e) => edit(setCommand)(e.target.value)} placeholder="mcp-filesystem" />
                </Field>
                <Field label="Arguments">
                  <input className="mono" value={args} onChange={(e) => edit(setArgs)(e.target.value)} placeholder="--root /data" />
                </Field>
              </div>
            )}
            {error && <p className="field-error">{error}</p>}
            <div className="row">
              {auth !== 'oauth' && (
                <button
                  type="button"
                  className="btn btn--quiet"
                  disabled={test.phase === 'testing' || (transport === 'http' ? !url : !command)}
                  onClick={() => void runTest()}
                >
                  {test.phase === 'testing' ? 'Testing…' : 'Test connection'}
                </button>
              )}
              <button
                className="btn btn--primary"
                disabled={
                  busy ||
                  !name ||
                  (transport === 'http' ? !url : !command) ||
                  (auth !== 'oauth' && test.phase !== 'ok')
                }
                title={auth !== 'oauth' && test.phase !== 'ok' ? 'Test the connection first' : undefined}
              >
                {busy ? 'Registering…' : auth === 'oauth' ? 'Register & sign in' : 'Register server'}
              </button>
            </div>
            {test.phase === 'ok' && (
              <p>
                <span className="pill pill--ok">
                  <span className="lamp" aria-hidden />
                  connection ok
                </span>{' '}
                <span className="muted">{test.detail}</span>
              </p>
            )}
            {test.phase === 'failed' && <p className="field-error">{test.detail}</p>}
            {test.phase === 'idle' && auth !== 'oauth' && (
              <p className="muted" style={{ fontSize: 12.5 }}>
                Test the connection to enable registering. OAuth servers verify through the
                sign-in instead.
              </p>
            )}
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
                  <th>Status</th>
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
                    <td>
                      <span className={`pill ${s.enabled ? 'pill--ok' : 'pill--idle'}`}>
                        <span className="lamp" aria-hidden />
                        {s.enabled ? 'enabled' : 'off'}
                      </span>
                    </td>
                    <td className="mono">{s.transport}</td>
                    <td className="mono muted">{s.url ?? `${s.command} ${s.args.join(' ')}`}</td>
                    <td>
                      {s.auth_type === 'oauth' ? (
                        <div>
                          <span
                            className={`pill ${
                              s.oauth_status === 'connected'
                                ? 'pill--ok'
                                : s.oauth_status === 'error'
                                  ? 'pill--err'
                                  : 'pill--warn'
                            }`}
                          >
                            <span className="lamp" aria-hidden />
                            {s.oauth_status === 'connected'
                              ? 'oauth · connected'
                              : s.oauth_status === 'error'
                                ? 'oauth · error'
                                : 'sign-in required'}
                          </span>
                          {s.oauth_error && (
                            <div className="field-error" style={{ maxWidth: 280, marginTop: 4 }}>
                              {s.oauth_error}
                            </div>
                          )}
                        </div>
                      ) : (
                        <span className="muted">{s.has_headers ? 'bearer token' : 'none'}</span>
                      )}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <span className="row" style={{ justifyContent: 'flex-end' }}>
                        {s.auth_type === 'oauth' && (
                          <button className="btn btn--quiet btn--sm" onClick={() => void connect(s)}>
                            {s.oauth_status === 'connected' ? 'Reconnect' : 'Connect'}
                          </button>
                        )}
                        <button className="btn btn--quiet btn--sm" onClick={() => void testRegistered(s)}>
                          Test
                        </button>
                        <button className="btn btn--quiet btn--sm" onClick={() => void toggleEnabled(s)}>
                          {s.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button className="btn btn--danger btn--sm" onClick={() => void remove(s)}>
                          Remove
                        </button>
                      </span>
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
