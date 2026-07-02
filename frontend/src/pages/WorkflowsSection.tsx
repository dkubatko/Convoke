import { FormEvent, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

interface ChatRef {
  id: number
  title: string
  tg_chat_id: number
}

interface Workflow {
  id: number
  name: string
  type: string
  enabled: boolean
  action_prompt: string
  cron: string | null
  next_fire_at: string | null
  trigger_prompt: string | null
  required_slots: { name: string; description: string }[]
  confirm: boolean
  cooldown_seconds: number
  threshold: number | null
  examples_status: string
  chat_ids: number[]
}

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

export default function WorkflowsSection({ chats }: { chats: ChatRef[] }) {
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [type, setType] = useState<'intent' | 'scheduled'>('intent')
  const [name, setName] = useState('')
  const [cron, setCron] = useState('0 9 * * *')
  const [trigger, setTrigger] = useState('')
  const [slots, setSlots] = useState('')
  const [action, setAction] = useState('')
  const [confirm, setConfirm] = useState(true)
  const [chatIds, setChatIds] = useState<number[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = () => api.get<Workflow[]>('/api/workflows').then(setWorkflows).catch(() => {})
  useEffect(() => {
    load()
    const t = setInterval(load, 15000)
    return () => clearInterval(t)
  }, [])

  async function create(e: FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      await api.post('/api/workflows', {
        name,
        type,
        action_prompt: action,
        cron: type === 'scheduled' ? cron : null,
        trigger_prompt: type === 'intent' ? trigger : null,
        required_slots: type === 'intent' ? parseSlots(slots) : [],
        confirm,
        chat_ids: chatIds,
      })
      setName(''); setTrigger(''); setSlots(''); setAction('')
      load()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create workflow')
    }
  }

  async function toggle(wf: Workflow) {
    await api.put(`/api/workflows/${wf.id}`, { ...wf, enabled: !wf.enabled })
    load()
  }

  async function remove(id: number) {
    if (!window.confirm('Delete this workflow?')) return
    await api.delete(`/api/workflows/${id}`)
    load()
  }

  return (
    <section>
      <h2>Workflows</h2>
      <form onSubmit={create} className="wf-form">
        <div className="row">
          <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
          <select value={type} onChange={(e) => setType(e.target.value as 'intent' | 'scheduled')}>
            <option value="intent">intent-based</option>
            <option value="scheduled">scheduled</option>
          </select>
          {type === 'scheduled' ? (
            <input placeholder="Cron (0 9 * * *)" value={cron} onChange={(e) => setCron(e.target.value)} />
          ) : (
            <label>
              <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />{' '}
              ask before acting
            </label>
          )}
        </div>
        {type === 'intent' && (
          <>
            <textarea
              placeholder="Trigger (plain text): e.g. When the chat converges on scheduling an event, with a specific date and time agreed"
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              rows={2}
            />
            <textarea
              placeholder={'Required slots, one per line as name: description\ndate: the agreed date and time\ntitle: what the event is'}
              value={slots}
              onChange={(e) => setSlots(e.target.value)}
              rows={3}
            />
          </>
        )}
        <textarea
          placeholder="Action: what the agent should do when triggered, e.g. Create the event via the calendar tools and confirm in chat"
          value={action}
          onChange={(e) => setAction(e.target.value)}
          rows={2}
        />
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <span>Chats:</span>
          {chats.map((c) => (
            <label key={c.id}>
              <input
                type="checkbox"
                checked={chatIds.includes(c.id)}
                onChange={(e) =>
                  setChatIds(e.target.checked ? [...chatIds, c.id] : chatIds.filter((i) => i !== c.id))
                }
              />{' '}
              {c.title || c.tg_chat_id}
            </label>
          ))}
          <button type="submit" disabled={!name || !action || (type === 'intent' && !trigger)}>
            Create workflow
          </button>
        </div>
      </form>
      {error && <div className="error">{error}</div>}

      {workflows.length > 0 && (
        <table>
          <thead>
            <tr><th>Name</th><th>Type</th><th>Status</th><th>Detail</th><th>Chats</th><th /></tr>
          </thead>
          <tbody>
            {workflows.map((w) => (
              <tr key={w.id}>
                <td>{w.name}</td>
                <td>{w.type}</td>
                <td>{w.enabled ? 'enabled' : 'disabled'}</td>
                <td className="detail">
                  {w.type === 'scheduled'
                    ? `${w.cron} · next: ${w.next_fire_at ? new Date(w.next_fire_at).toLocaleString() : '—'}`
                    : `examples: ${w.examples_status}${w.threshold ? ` · threshold ${w.threshold.toFixed(2)}` : ''}${w.confirm ? ' · confirm' : ''}`}
                </td>
                <td>{w.chat_ids.length}</td>
                <td>
                  <button onClick={() => toggle(w)}>{w.enabled ? 'Disable' : 'Enable'}</button>{' '}
                  <button className="danger" onClick={() => remove(w.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
