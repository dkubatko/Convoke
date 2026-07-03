import { FormEvent, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { Bot } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  EmptyState,
  ErrorNote,
  Field,
  LoadingWire,
  PageHead,
  StatusPill,
} from '../components/ui'

export default function Bots() {
  const bots = useQuery<Bot[]>(() => api.get('/api/bots'), [], { pollMs: 15000 })
  const toast = useToast()
  const confirm = useConfirm()
  const [token, setToken] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function connect(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setFormError(null)
    try {
      const bot = await api.post<Bot>('/api/bots', { token })
      setToken('')
      toast('ok', `Connected @${bot.username}`)
      if (!bot.can_read_all_group_messages) {
        toast('err', `@${bot.username} has privacy mode on — it can't hear group messages yet.`)
      }
      void bots.refetch()
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : 'Couldn’t reach the backend')
    } finally {
      setBusy(false)
    }
  }

  async function recheck(bot: Bot) {
    const updated = await api.post<Bot>(`/api/bots/${bot.id}/recheck`)
    toast(
      updated.can_read_all_group_messages ? 'ok' : 'err',
      updated.can_read_all_group_messages
        ? `@${updated.username} can hear all group messages`
        : `@${updated.username} still has privacy mode on — remember to remove and re-add it to each group after changing it`,
    )
    void bots.refetch()
  }

  async function remove(bot: Bot) {
    const ok = await confirm({
      title: `Remove @${bot.username}?`,
      body: 'Its chats, stored messages, and memory go with it. This can’t be undone.',
      actionLabel: 'Remove bot',
      danger: true,
    })
    if (!ok) return
    await api.delete(`/api/bots/${bot.id}`)
    toast('ok', `Removed @${bot.username}`)
    void bots.refetch()
  }

  return (
    <>
      <PageHead
        title="Bots"
        lede="Each connected bot polls Telegram on its own line. Paste a token from @BotFather to put one on the wire."
      />
      <div className="stack">
        <Card title="Connect a bot">
          <form className="row" onSubmit={connect} style={{ alignItems: 'flex-start' }}>
            <div style={{ flex: '1 1 320px' }}>
              <Field
                label="Bot token"
                error={formError}
                hint="From @BotFather. Run /setprivacy → Disable first, or the bot only hears mentions."
              >
                <input
                  type="password"
                  placeholder="123456789:AAF…"
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value)
                    setFormError(null)
                  }}
                  autoComplete="off"
                />
              </Field>
            </div>
            <div className="field">
              <label aria-hidden>&nbsp;</label>
              <button className="btn btn--primary" type="submit" disabled={busy || !token}>
                {busy ? 'Checking with Telegram…' : 'Connect bot'}
              </button>
            </div>
          </form>
        </Card>

        <Card pad={false}>
          {bots.loading ? (
            <LoadingWire />
          ) : bots.error ? (
            <ErrorNote message={bots.error} onRetry={() => void bots.refetch()} />
          ) : (bots.data ?? []).length === 0 ? (
            <EmptyState
              title="No bots connected"
              hint="Create one in Telegram with @BotFather, then paste its token above."
            />
          ) : (
            <table className="data">
              <thead>
                <tr>
                  <th>Bot</th>
                  <th>Status</th>
                  <th>Hearing</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {bots.data!.map((b) => (
                  <tr key={b.id}>
                    <td>
                      <b>@{b.username}</b>
                      <div className="muted" style={{ fontSize: 12 }}>{b.name}</div>
                    </td>
                    <td>
                      <StatusPill status={b.status} live={b.status === 'active'} />
                      {b.last_error && (
                        <div className="field-error" style={{ marginTop: 4 }}>{b.last_error}</div>
                      )}
                    </td>
                    <td>
                      {b.can_read_all_group_messages ? (
                        <span className="pill pill--ok">
                          <span className="lamp" aria-hidden />
                          all messages
                        </span>
                      ) : (
                        <div>
                          <span className="pill pill--err">
                            <span className="lamp" aria-hidden />
                            mentions only
                          </span>
                          <div className="field-error" style={{ marginTop: 4, maxWidth: 320 }}>
                            Privacy mode is on, so chat memory stays empty. In @BotFather:
                            /setprivacy → Disable, then remove and re-add the bot to each group.
                          </div>
                        </div>
                      )}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <span className="row" style={{ justifyContent: 'flex-end' }}>
                        <button className="btn btn--quiet btn--sm" onClick={() => void recheck(b)}>
                          Re-check
                        </button>
                        <button className="btn btn--danger btn--sm" onClick={() => void remove(b)}>
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
