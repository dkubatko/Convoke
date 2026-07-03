import { FormEvent, useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { timeAgo } from '../lib/format'
import { Provider } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { Card, CardSkeleton, ErrorNote, Field, PageHead } from '../components/ui'

const ROLES: { role: string; title: string; blurb: string; placeholder: string }[] = [
  {
    role: 'intent',
    title: 'Intent — the listener',
    blurb:
      'A cheap, fast model that classifies conversation windows for intent workflows. Falls back to the agent model if unset.',
    placeholder: 'gemma4',
  },
  {
    role: 'agent',
    title: 'Agent — the voice',
    blurb: 'The strong model that writes replies and carries out workflow actions. Required.',
    placeholder: 'gpt-5.4-mini',
  },
]

export default function Models() {
  const providers = useQuery<Provider[]>(() => api.get('/api/providers'), [])

  return (
    <>
      <PageHead
        title="Models"
        lede="Point each role at any OpenAI-compatible endpoint — Ollama, LM Studio, OpenRouter, OpenAI. Connections are tested before they can be saved; keys are stored encrypted."
      />
      {providers.loading ? (
        <div className="stack">
          <CardSkeleton lines={2} />
          <CardSkeleton lines={3} />
          <CardSkeleton lines={3} />
        </div>
      ) : providers.error ? (
        <ErrorNote message={providers.error} onRetry={() => void providers.refetch()} />
      ) : (
        <div className="stack">
          <Card>
            <RoleHeader title="Embeddings — the memory">
              <span className="pill pill--ok">
                <span className="lamp" aria-hidden />
                built-in · local
              </span>
            </RoleHeader>
            <p className="muted">
              Chat memory is embedded locally with <code>multilingual-e5-small</code> inside the
              worker — nothing to configure, works offline. Swapping embedding models is a
              deployment-level change since stored vectors would need rebuilding.
            </p>
          </Card>
          {ROLES.map((r) => (
            <ProviderCard
              key={r.role}
              {...r}
              current={providers.data?.find((p) => p.role === r.role)}
              onSaved={() => void providers.refetch()}
            />
          ))}
        </div>
      )}
    </>
  )
}

function RoleHeader({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="page-head-row" style={{ marginBottom: 10 }}>
      <h3 style={{ fontSize: 16 }}>{title}</h3>
      {children}
    </div>
  )
}

type TestState =
  | { phase: 'idle' }
  | { phase: 'testing' }
  | { phase: 'ok'; detail: string }
  | { phase: 'failed'; detail: string }

function ProviderCard({ role, title, blurb, placeholder, current, onSaved }: {
  role: string
  title: string
  blurb: string
  placeholder: string
  current: Provider | undefined
  onSaved: () => void
}) {
  const toast = useToast()
  const [baseUrl, setBaseUrl] = useState('')
  const [modelName, setModelName] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [clearKey, setClearKey] = useState(false)
  const [test, setTest] = useState<TestState>({ phase: 'idle' })
  const [saving, setSaving] = useState(false)
  const [justSaved, setJustSaved] = useState(false)

  useEffect(() => {
    setBaseUrl(current?.base_url ?? '')
    setModelName(current?.model_name ?? '')
    setApiKey('')
    setClearKey(false)
  }, [current])

  // Any edit invalidates a previous test result.
  function edit(setter: (v: string) => void) {
    return (value: string) => {
      setter(value)
      setTest({ phase: 'idle' })
      setJustSaved(false)
    }
  }

  async function runTest() {
    setTest({ phase: 'testing' })
    try {
      const result = await api.post<{ ok: boolean; detail: string }>('/api/providers/test', {
        base_url: baseUrl,
        model_name: modelName,
        // blank field = reuse the key already saved for this role (if any)
        api_key: apiKey || null,
        role,
      })
      setTest(result.ok ? { phase: 'ok', detail: result.detail } : { phase: 'failed', detail: result.detail })
    } catch (err) {
      setTest({
        phase: 'failed',
        detail: err instanceof ApiError ? err.message : 'The backend didn’t respond.',
      })
    }
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (test.phase !== 'ok') return
    setSaving(true)
    try {
      const body: Record<string, unknown> = { base_url: baseUrl, model_name: modelName }
      // "" clears the stored key server-side; omitted keeps it; a value sets it.
      if (clearKey) body.api_key = ''
      else if (apiKey) body.api_key = apiKey
      await api.put(`/api/providers/${role}`, body)
      toast('ok', `Saved the ${role} model`)
      setJustSaved(true)
      onSaved()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : `Couldn’t save the ${role} model`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <RoleHeader title={title}>
        {current ? (
          <span className="pill pill--ok" title={`Saved ${timeAgo(current.updated_at)}`}>
            <span className="lamp" aria-hidden />
            configured · {current.model_name}
          </span>
        ) : (
          <span className="pill pill--warn">
            <span className="lamp" aria-hidden />
            not configured
          </span>
        )}
      </RoleHeader>
      <p className="muted" style={{ marginBottom: 14 }}>{blurb}</p>
      <form className="row" style={{ alignItems: 'flex-start' }} onSubmit={save}>
        <div style={{ flex: '2 1 260px' }}>
          <Field label="Base URL" hint="From Docker, your host is http://host.docker.internal">
            <input
              className="mono"
              value={baseUrl}
              onChange={(e) => edit(setBaseUrl)(e.target.value)}
              placeholder="http://host.docker.internal:11434/v1"
            />
          </Field>
        </div>
        <div style={{ flex: '1 1 150px' }}>
          <Field label="Model" hint="The id the endpoint expects.">
            <input
              className="mono"
              value={modelName}
              onChange={(e) => edit(setModelName)(e.target.value)}
              placeholder={placeholder}
            />
          </Field>
        </div>
        <div style={{ flex: '1 1 150px' }}>
          <Field
            label="API key"
            hint={
              clearKey
                ? 'Will clear the saved key on save.'
                : current?.has_api_key
                  ? 'A key is saved; leave blank to keep it.'
                  : 'Optional for local endpoints.'
            }
          >
            <input
              type="password"
              value={apiKey}
              placeholder={clearKey ? '(cleared)' : ''}
              disabled={clearKey}
              onChange={(e) => edit(setApiKey)(e.target.value)}
              autoComplete="off"
            />
            {current?.has_api_key && (
              <label className="row" style={{ gap: 6, fontSize: 12, marginTop: 4 }}>
                <input
                  type="checkbox"
                  style={{ width: 'auto' }}
                  checked={clearKey}
                  onChange={(e) => { setClearKey(e.target.checked); setApiKey('') }}
                />
                Clear saved key
              </label>
            )}
          </Field>
        </div>
        <div className="field">
          <label aria-hidden>&nbsp;</label>
          <span className="row" style={{ flexWrap: 'nowrap' }}>
            <button
              type="button"
              className="btn btn--quiet"
              disabled={test.phase === 'testing' || !baseUrl || !modelName}
              onClick={() => void runTest()}
            >
              {test.phase === 'testing' ? 'Testing…' : 'Test connection'}
            </button>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={test.phase !== 'ok' || saving}
              title={test.phase !== 'ok' ? 'Test the connection first' : undefined}
            >
              {saving ? 'Saving…' : justSaved ? 'Saved ✓' : 'Save'}
            </button>
          </span>
        </div>
      </form>
      {test.phase === 'ok' && (
        <p style={{ marginTop: 10 }}>
          <span className="pill pill--ok">
            <span className="lamp" aria-hidden />
            connection ok
          </span>{' '}
          <span className="muted">{test.detail}</span>
        </p>
      )}
      {test.phase === 'failed' && (
        <p className="field-error" style={{ marginTop: 10 }}>
          {test.detail}
        </p>
      )}
      {test.phase === 'idle' && !justSaved && (
        <p className="muted" style={{ marginTop: 10, fontSize: 12.5 }}>
          Test the connection to enable saving.
        </p>
      )}
    </Card>
  )
}
