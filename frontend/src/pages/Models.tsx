import { FormEvent, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { Provider } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { Card, Field, LoadingWire, PageHead } from '../components/ui'

const ROLES: { role: string; title: string; blurb: string }[] = [
  {
    role: 'agent',
    title: 'Agent — the voice',
    blurb: 'The strong model that writes replies and carries out workflow actions. Required.',
  },
  {
    role: 'intent',
    title: 'Intent — the listener',
    blurb:
      'A cheap, fast model that classifies conversation windows for intent workflows. Falls back to the agent model if unset.',
  },
]

export default function Models() {
  const providers = useQuery<Provider[]>(() => api.get('/api/providers'), [])

  return (
    <>
      <PageHead
        title="Models"
        lede="Point each role at any OpenAI-compatible endpoint — Ollama, LM Studio, OpenRouter, OpenAI. Keys are stored encrypted."
      />
      {providers.loading ? (
        <LoadingWire />
      ) : (
        <div className="stack">
          {ROLES.map((r) => (
            <ProviderCard
              key={r.role}
              {...r}
              current={providers.data?.find((p) => p.role === r.role)}
              onSaved={() => void providers.refetch()}
            />
          ))}
          <Card title="Embeddings — the memory">
            <p className="muted">
              Chat memory is embedded locally with <code>multilingual-e5-small</code> inside the
              worker — no endpoint to configure, works offline. Swapping embedding models is a
              deployment-level change since stored vectors would need rebuilding.
            </p>
          </Card>
        </div>
      )}
    </>
  )
}

function ProviderCard({ role, title, blurb, current, onSaved }: {
  role: string
  title: string
  blurb: string
  current: Provider | undefined
  onSaved: () => void
}) {
  const toast = useToast()
  const [baseUrl, setBaseUrl] = useState('')
  const [modelName, setModelName] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setBaseUrl(current?.base_url ?? '')
    setModelName(current?.model_name ?? '')
    setApiKey('')
  }, [current])

  async function save(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      const body: Record<string, unknown> = { base_url: baseUrl, model_name: modelName }
      if (apiKey) body.api_key = apiKey
      await api.put(`/api/providers/${role}`, body)
      toast('ok', `Saved the ${role} model`)
      onSaved()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : `Couldn’t save the ${role} model`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title={title}>
      <p className="muted" style={{ marginBottom: 14 }}>{blurb}</p>
      <form className="row" style={{ alignItems: 'flex-start' }} onSubmit={save}>
        <div style={{ flex: '2 1 260px' }}>
          <Field label="Base URL" hint="From Docker, your host is http://host.docker.internal">
            <input
              className="mono"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://host.docker.internal:11434/v1"
            />
          </Field>
        </div>
        <div style={{ flex: '1 1 150px' }}>
          <Field label="Model" hint="The id the endpoint expects.">
            <input
              className="mono"
              value={modelName}
              onChange={(e) => setModelName(e.target.value)}
              placeholder={role === 'intent' ? 'qwen3:4b' : 'qwen3:8b'}
            />
          </Field>
        </div>
        <div style={{ flex: '1 1 150px' }}>
          <Field label="API key" hint={current?.has_api_key ? 'A key is saved; leave blank to keep it.' : 'Optional for local endpoints.'}>
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="off"
            />
          </Field>
        </div>
        <div className="field">
          <label aria-hidden>&nbsp;</label>
          <button className="btn btn--primary" disabled={busy || !baseUrl || !modelName}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </form>
    </Card>
  )
}
