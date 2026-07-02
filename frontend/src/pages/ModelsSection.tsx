import { FormEvent, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

interface Provider {
  role: string
  base_url: string
  model_name: string
  has_api_key: boolean
}

const ROLE_INFO: Record<string, string> = {
  agent: 'Strong model used for replies and workflow actions',
  intent: 'Cheap model used for continuous intent classification',
}

function ProviderForm({ role, current, onSaved }: {
  role: string
  current: Provider | undefined
  onSaved: () => void
}) {
  const [baseUrl, setBaseUrl] = useState('')
  const [modelName, setModelName] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [status, setStatus] = useState<string | null>(null)

  useEffect(() => {
    setBaseUrl(current?.base_url ?? '')
    setModelName(current?.model_name ?? '')
    setApiKey('')
  }, [current])

  async function save(e: FormEvent) {
    e.preventDefault()
    setStatus(null)
    try {
      const body: Record<string, unknown> = { base_url: baseUrl, model_name: modelName }
      if (apiKey) body.api_key = apiKey
      await api.put(`/api/providers/${role}`, body)
      setStatus('saved')
      onSaved()
    } catch (err) {
      setStatus(err instanceof ApiError ? err.message : 'save failed')
    }
  }

  return (
    <form className="row" onSubmit={save}>
      <span style={{ minWidth: '60px' }}><b>{role}</b></span>
      <input
        placeholder="Base URL (e.g. http://host.docker.internal:11434/v1)"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.target.value)}
        style={{ minWidth: '300px' }}
        title={ROLE_INFO[role]}
      />
      <input
        placeholder="Model (e.g. qwen3:8b)"
        value={modelName}
        onChange={(e) => setModelName(e.target.value)}
      />
      <input
        type="password"
        placeholder={current?.has_api_key ? 'API key (saved)' : 'API key (optional)'}
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
      />
      <button type="submit" disabled={!baseUrl || !modelName}>Save</button>
      {status && <span className={status === 'saved' ? 'ok' : 'error'}>{status}</span>}
    </form>
  )
}

export default function ModelsSection() {
  const [providers, setProviders] = useState<Provider[]>([])

  const load = () => api.get<Provider[]>('/api/providers').then(setProviders).catch(() => {})
  useEffect(() => { load() }, [])

  return (
    <section>
      <h2>Models</h2>
      <p className="hint">
        Point each role at any OpenAI-compatible endpoint (Ollama, LM Studio, OpenRouter,
        OpenAI…). Embeddings run locally by default (multilingual-e5-small).
      </p>
      {['agent', 'intent'].map((role) => (
        <ProviderForm
          key={role}
          role={role}
          current={providers.find((p) => p.role === role)}
          onSaved={load}
        />
      ))}
    </section>
  )
}
