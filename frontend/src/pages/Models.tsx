import { FormEvent, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { timeAgo } from '../lib/format'
import { ConnectedModel, EmbeddingsInfo, ModelTestResult, RoleAssignment } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  CardSkeleton,
  EmptyState,
  ErrorNote,
  Field,
  PageHead,
  TabBar,
  TableSkeleton,
} from '../components/ui'
import SettingsEditor from '../components/SettingsEditor'
import { Select, SelectOption } from '../components/Select'
import { useUrlTab } from '../hooks/useUrlTab'

const SUBTABS = ['Role assignment', 'Model library', 'Settings'] as const

const CAPABILITIES = ['chat', 'vision', 'transcription', 'video'] as const

const ROLES: { role: string; title: string; blurb: string; recommended: string; note?: string }[] = [
  {
    role: 'agent',
    title: 'Agent — the voice',
    blurb: 'The strong model that writes replies and carries out workflow actions. Required.',
    recommended: 'a strong tool-calling model (gpt-5.4-mini class or better)',
  },
  {
    role: 'intent',
    title: 'Intent — the listener',
    blurb:
      'A cheap, fast model that classifies conversation windows for intent workflows. Falls back to the agent model if unset.',
    recommended: 'a small, fast model (gpt-5.4-nano / gemma4 class)',
    note: 'Cost matters more than brilliance here.',
  },
  {
    role: 'vision',
    title: 'Vision — the eyes',
    blurb:
      'Describes photos, stickers, and video frames so they enter chat memory and intent evaluation as text. Unset: media is stored but not understood.',
    recommended: 'a mid-size vision model (Qwen3-VL-8B / gpt-5.4-mini class)',
  },
  {
    role: 'transcription',
    title: 'Transcription — the ears',
    blurb:
      'Turns voice messages and audio tracks into transcripts via an OpenAI-compatible /audio/transcriptions endpoint (whisper servers work).',
    recommended: 'any whisper-class endpoint (faster-whisper-server, speaches, OpenAI whisper-1)',
  },
  {
    role: 'video',
    title: 'Video — motion understanding',
    blurb:
      'Optional: a model that accepts video input directly. Unset: videos are described from thumbnail + sampled frames + audio transcript instead.',
    recommended: 'a video-native VLM served by vLLM (Qwen3-VL class)',
    note: 'Most setups leave this unassigned.',
  },
]

export default function Models() {
  const models = useQuery<ConnectedModel[]>(() => api.get('/api/models'), [])
  const roles = useQuery<RoleAssignment[]>(() => api.get('/api/model-roles'), [])
  const [subtab, setSubtab] = useUrlTab(SUBTABS, 'Role assignment')

  const refetchAll = () => {
    void models.refetch()
    void roles.refetch()
  }

  return (
    <>
      <PageHead
        title="Models"
        lede="Assign a model to each execution role; the library holds your connected OpenAI-compatible endpoints. Connections are tested before they can be saved; keys are stored encrypted."
      />
      <TabBar tabs={SUBTABS} active={subtab} onSelect={setSubtab} />
      {subtab === 'Settings' && (
        <SettingsEditor endpoint="/api/settings?page=models" />
      )}
      {subtab === 'Model library' && (
        <ModelLibrary models={models} onChanged={refetchAll} />
      )}
      {subtab === 'Role assignment' && (
        <RoleAssignments models={models} roles={roles} onChanged={refetchAll} />
      )}
    </>
  )
}

function CapChip({ name, on }: { name: string; on: boolean }) {
  return (
    <span className={`pill ${on ? 'pill--accent' : 'pill--idle'}`} title={on ? `supports ${name}` : `no ${name}`}>
      {name}
    </span>
  )
}

type TestState =
  | { phase: 'idle' }
  | { phase: 'testing' }
  | { phase: 'done'; result: ModelTestResult }
  | { phase: 'failed'; detail: string }

function testPassed(t: TestState): boolean {
  return t.phase === 'done' && (t.result.chat.ok || t.result.vision.ok || t.result.transcription.ok)
}

function ModelLibrary({
  models,
  onChanged,
}: {
  models: ReturnType<typeof useQuery<ConnectedModel[]>>
  onChanged: () => void
}) {
  const toast = useToast()
  const confirm = useConfirm()

  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [modelName, setModelName] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [video, setVideo] = useState(false)
  const [test, setTest] = useState<TestState>({ phase: 'idle' })
  const [busy, setBusy] = useState(false)

  // Any edit invalidates the previous probe result.
  function edit(setter: (v: string) => void) {
    return (value: string) => {
      setter(value)
      setTest({ phase: 'idle' })
    }
  }

  async function runTest() {
    setTest({ phase: 'testing' })
    try {
      const result = await api.post<ModelTestResult>('/api/models/test', {
        base_url: baseUrl,
        model_name: modelName,
        api_key: apiKey || null,
      })
      setTest({ phase: 'done', result })
    } catch (err) {
      setTest({
        phase: 'failed',
        detail: err instanceof ApiError ? err.message : 'The backend didn’t respond.',
      })
    }
  }

  async function add(e: FormEvent) {
    e.preventDefault()
    if (!testPassed(test) || test.phase !== 'done') return
    setBusy(true)
    try {
      await api.post('/api/models', {
        name,
        base_url: baseUrl,
        model_name: modelName,
        api_key: apiKey || null,
        capabilities: {
          chat: test.result.chat.ok,
          vision: test.result.vision.ok,
          transcription: test.result.transcription.ok,
          video,
        },
      })
      toast('ok', `Connected ${name} — assign it a role from the Role assignment tab`)
      setName(''); setBaseUrl(''); setModelName(''); setApiKey(''); setVideo(false)
      setTest({ phase: 'idle' })
      onChanged()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t connect the model')
    } finally {
      setBusy(false)
    }
  }

  /** Re-probe a saved model and persist the freshly detected capabilities. */
  async function retest(m: ConnectedModel) {
    toast('info', `Testing ${m.name}…`)
    try {
      const result = await api.post<ModelTestResult>('/api/models/test', {
        base_url: m.base_url,
        model_name: m.model_name,
        api_key: null, // reuse the stored key
        model_id: m.id,
      })
      await api.put(`/api/models/${m.id}`, {
        name: m.name,
        base_url: m.base_url,
        model_name: m.model_name,
        api_key: null,
        capabilities: {
          chat: result.chat.ok,
          vision: result.vision.ok,
          transcription: result.transcription.ok,
          video: m.capabilities.video ?? false, // operator-declared, not probed
        },
      })
      const up = CAPABILITIES.filter(
        (c) => c !== 'video' && result[c as keyof ModelTestResult].ok,
      )
      toast(up.length ? 'ok' : 'err', `${m.name}: ${up.length ? `responds (${up.join(', ')})` : result.chat.detail}`)
      onChanged()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : `Couldn’t test ${m.name}`)
    }
  }

  async function remove(m: ConnectedModel) {
    const ok = await confirm({
      title: `Remove ${m.name}?`,
      body: m.assigned_roles.length
        ? `It is assigned to: ${m.assigned_roles.join(', ')}. Unassign those roles first.`
        : 'The endpoint config and its stored key are deleted.',
      actionLabel: 'Remove model',
      danger: true,
    })
    if (!ok) return
    try {
      await api.delete(`/api/models/${m.id}`)
      toast('ok', `Removed ${m.name}`)
      onChanged()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : `Couldn’t remove ${m.name}`)
    }
  }

  return (
    <div className="stack">
      <Card title="Connect a model">
        <form className="stack" style={{ gap: 14 }} onSubmit={add}>
          <div className="grid-2">
            <Field label="Name" hint="How this connection appears in the library.">
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Qwen3-VL (local)" />
            </Field>
            <Field label="Model" hint="The id the endpoint expects.">
              <input
                className="mono"
                value={modelName}
                onChange={(e) => edit(setModelName)(e.target.value)}
                placeholder="qwen3-vl-8b"
              />
            </Field>
          </div>
          <div className="grid-2">
            <Field label="Base URL" hint="From Docker, your host is http://host.docker.internal">
              <input
                className="mono"
                value={baseUrl}
                onChange={(e) => edit(setBaseUrl)(e.target.value)}
                placeholder="http://host.docker.internal:11434/v1"
              />
            </Field>
            <Field label="API key" hint="Optional for local endpoints. Stored encrypted.">
              <input
                type="password"
                value={apiKey}
                onChange={(e) => edit(setApiKey)(e.target.value)}
                autoComplete="off"
              />
            </Field>
          </div>
          <label className="row" style={{ gap: 6, fontSize: 13 }}>
            <input
              type="checkbox"
              style={{ width: 'auto' }}
              checked={video}
              onChange={(e) => setVideo(e.target.checked)}
            />
            Accepts video input (can’t be probed reliably — check only if the endpoint documents it)
          </label>
          <div className="row">
            <button
              type="button"
              className="btn btn--quiet"
              disabled={test.phase === 'testing' || !baseUrl || !modelName}
              onClick={() => void runTest()}
            >
              {test.phase === 'testing' ? 'Testing…' : 'Test & detect capabilities'}
            </button>
            <button
              className="btn btn--primary"
              disabled={busy || !name || !testPassed(test)}
              title={!testPassed(test) ? 'Test the connection first' : undefined}
            >
              {busy ? 'Connecting…' : 'Connect model'}
            </button>
          </div>
          {test.phase === 'done' && (
            <div className="stack" style={{ gap: 6 }}>
              {(['chat', 'vision', 'transcription'] as const).map((cap) => (
                <p key={cap} style={{ margin: 0 }}>
                  <span className={`pill ${test.result[cap].ok ? 'pill--ok' : 'pill--idle'}`}>
                    <span className="lamp" aria-hidden />
                    {cap}
                  </span>{' '}
                  <span className="muted" style={{ fontSize: 12.5 }}>{test.result[cap].detail}</span>
                </p>
              ))}
              {!testPassed(test) && (
                <p className="field-error">No capability responded — check the URL, model id, and key.</p>
              )}
            </div>
          )}
          {test.phase === 'failed' && <p className="field-error">{test.detail}</p>}
          {test.phase === 'idle' && (
            <p className="muted" style={{ fontSize: 12.5 }}>
              Testing probes chat, vision, and transcription so the library knows what this model can do.
            </p>
          )}
        </form>
      </Card>

      <Card pad={false}>
        {models.loading ? (
          <TableSkeleton rows={2} />
        ) : models.error ? (
          <ErrorNote message={models.error} onRetry={() => onChanged()} />
        ) : (models.data ?? []).length === 0 ? (
          <EmptyState
            title="No models connected yet"
            hint="Connect at least one chat-capable model, then assign it to the agent role."
          />
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Name</th>
                <th>Endpoint</th>
                <th>Capabilities</th>
                <th>Roles</th>
                <th>Tested</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {models.data!.map((m) => (
                <tr key={m.id}>
                  <td>
                    <b>{m.name}</b>
                    <div className="mono muted" style={{ fontSize: 12 }}>{m.model_name}</div>
                  </td>
                  <td className="mono muted">{m.base_url}</td>
                  <td>
                    <span className="row" style={{ gap: 4 }}>
                      {CAPABILITIES.map((c) => (
                        <CapChip key={c} name={c} on={!!m.capabilities[c]} />
                      ))}
                    </span>
                  </td>
                  <td>
                    {m.assigned_roles.length ? (
                      <span className="row" style={{ gap: 4 }}>
                        {m.assigned_roles.map((r) => (
                          <span key={r} className="pill pill--ok">{r}</span>
                        ))}
                      </span>
                    ) : (
                      <span className="muted">unassigned</span>
                    )}
                  </td>
                  <td className="muted">{m.last_tested_at ? timeAgo(m.last_tested_at) : '—'}</td>
                  <td style={{ textAlign: 'right' }}>
                    <span className="row" style={{ justifyContent: 'flex-end' }}>
                      <button className="btn btn--quiet btn--sm" onClick={() => void retest(m)}>
                        Test
                      </button>
                      <button className="btn btn--danger btn--sm" onClick={() => void remove(m)}>
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
  )
}

function RoleAssignments({
  models,
  roles,
  onChanged,
}: {
  models: ReturnType<typeof useQuery<ConnectedModel[]>>
  roles: ReturnType<typeof useQuery<RoleAssignment[]>>
  onChanged: () => void
}) {
  if (models.loading || roles.loading) {
    return (
      <div className="stack">
        <CardSkeleton lines={2} />
        <CardSkeleton lines={2} />
        <CardSkeleton lines={2} />
      </div>
    )
  }
  if (roles.error) return <ErrorNote message={roles.error} onRetry={onChanged} />

  const byRole = new Map((roles.data ?? []).map((r) => [r.role, r]))
  return (
    <div className="stack">
      <EmbeddingsCard />
      {ROLES.map((meta) => (
        <RoleCard
          key={meta.role}
          meta={meta}
          assignment={byRole.get(meta.role)}
          library={models.data ?? []}
          onChanged={onChanged}
        />
      ))}
    </div>
  )
}

const CUSTOM_MODEL = '__custom__'

function EmbeddingsCard() {
  const toast = useToast()
  const confirm = useConfirm()
  const info = useQuery<EmbeddingsInfo>(() => api.get('/api/embeddings'), [], { pollMs: 5000 })
  const [selected, setSelected] = useState('')
  const [customId, setCustomId] = useState('')
  const [busy, setBusy] = useState(false)

  const cur = info.data?.current
  const registry = info.data?.registry ?? []
  const reembedding = cur?.status === 'reembedding'
  // Default the picker to the current model so it reads as the selected option
  // — not a separate "(current: …)" entry duplicating a registry row.
  const effective = selected || cur?.model_id || ''
  const inRegistry = registry.some((r) => r.id === cur?.model_id)
  const modelOptions: SelectOption[] = [
    ...(cur && !inRegistry
      ? [{ value: cur.model_id, label: cur.model_id, hint: '· current (custom)' }]
      : []),
    ...registry.map((r) => ({ value: r.id, label: r.label })),
    { value: CUSTOM_MODEL, label: 'Custom Hugging Face id…' },
  ]
  const targetId = effective === CUSTOM_MODEL ? customId.trim() : effective
  const changed = targetId !== '' && targetId !== cur?.model_id

  async function switchModel() {
    if (!cur || !targetId) return
    const ok = await confirm({
      title: `Switch embeddings to ${targetId}?`,
      body:
        'Every memory vector is rebuilt with the new model. Semantic search and the ' +
        'intent prefilter degrade until the rebuild finishes — workflow windows fall ' +
        'through to the classifier meanwhile, so nothing is missed.',
      actionLabel: 'Switch & re-embed',
    })
    if (!ok) return
    setBusy(true)
    try {
      await api.post('/api/embeddings/model', { model_id: targetId })
      toast('ok', `Re-embedding everything with ${targetId}…`)
      setSelected('')
      setCustomId('')
      void info.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t start the re-embed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <div className="page-head-row" style={{ marginBottom: 10 }}>
        <h3 style={{ fontSize: 16 }}>Embeddings — the memory</h3>
        {cur ? (
          reembedding ? (
            <span className="pill pill--warn pill--live">
              <span className="lamp" aria-hidden />
              rebuilding · {cur.phase ?? 'queued'}
              {cur.total > 0 ? ` (${cur.done}/${cur.total})` : ''}
            </span>
          ) : (
            <span className="pill pill--ok">
              <span className="lamp" aria-hidden />
              {cur.model_id} · {cur.dim}d · local
            </span>
          )
        ) : (
          <span className="pill pill--idle">loading…</span>
        )}
      </div>
      <p className="muted" style={{ marginBottom: 14 }}>
        Chat memory, notes, and the intent prefilter all run on locally computed embeddings
        (CPU, inside the worker). Switching models rebuilds every stored vector and
        recalibrates workflow thresholds.
      </p>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <div style={{ flex: '0 1 420px' }}>
          <Field label="Model">
            <Select
              value={effective}
              options={modelOptions}
              disabled={reembedding}
              onChange={setSelected}
              ariaLabel="Embedding model"
            />
          </Field>
        </div>
        <button
          className="btn btn--primary"
          disabled={!changed || reembedding || busy}
          onClick={() => void switchModel()}
        >
          {reembedding ? 'Rebuilding…' : 'Switch & re-embed'}
        </button>
      </div>
      {/* Hint outside the 420px column so it uses the card's full width. */}
      <div className="field-hint" style={{ marginTop: 6 }}>
        Vetted CPU-viable options, or any sentence-transformers model from Hugging Face.
      </div>
      {effective === CUSTOM_MODEL && (
        <>
          <div style={{ maxWidth: 420, marginTop: 10 }}>
            <Field label="Hugging Face id">
              <input
                className="mono"
                value={customId}
                onChange={(e) => setCustomId(e.target.value)}
                placeholder="org/model-name"
              />
            </Field>
          </div>
          <div className="field-hint" style={{ marginTop: 6 }}>
            Must be loadable by sentence-transformers; the dimension is probed automatically.
          </div>
        </>
      )}
      {cur?.error && <p className="field-error" style={{ marginTop: 8 }}>{cur.error}</p>}
    </Card>
  )
}

function RoleCard({
  meta,
  assignment,
  library,
  onChanged,
}: {
  meta: (typeof ROLES)[number]
  assignment: RoleAssignment | undefined
  library: ConnectedModel[]
  onChanged: () => void
}) {
  const toast = useToast()
  const [selected, setSelected] = useState<number | ''>(assignment?.model_id ?? '')
  const [busy, setBusy] = useState(false)

  const required = assignment?.required_capability ?? 'chat'
  const selectedModel = library.find((m) => m.id === selected)
  const selectionLacksCapability = selectedModel != null && !selectedModel.capabilities[required]

  async function apply() {
    setBusy(true)
    try {
      if (selected === '') {
        await api.delete(`/api/model-roles/${meta.role}`)
        toast('ok', `Unassigned the ${meta.role} role`)
      } else {
        await api.put(`/api/model-roles/${meta.role}`, { model_id: selected })
        toast('ok', `${meta.role} role → ${selectedModel?.name ?? selected}`)
      }
      onChanged()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : `Couldn’t update the ${meta.role} role`)
    } finally {
      setBusy(false)
    }
  }

  const dirty = (assignment?.model_id ?? '') !== selected
  return (
    <Card>
      <div className="page-head-row" style={{ marginBottom: 10 }}>
        <h3 style={{ fontSize: 16 }}>{meta.title}</h3>
        {assignment?.model_id != null ? (
          assignment.capability_ok ? (
            <span className="pill pill--ok">
              <span className="lamp" aria-hidden />
              {assignment.model_name}
            </span>
          ) : (
            <span className="pill pill--warn" title={`This model has no ${required} capability`}>
              <span className="lamp" aria-hidden />
              {assignment.model_name} · no {required}
            </span>
          )
        ) : (
          <span className="pill pill--idle">
            <span className="lamp" aria-hidden />
            not assigned
          </span>
        )}
      </div>
      <p className="muted" style={{ marginBottom: 14 }}>{meta.blurb}</p>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <div style={{ flex: '0 1 420px' }}>
          <Field label="Model">
            <Select
              value={selected === '' ? '' : String(selected)}
              onChange={(v) => setSelected(v === '' ? '' : Number(v))}
              ariaLabel={`${meta.title} model`}
              options={[
                { value: '', label: '(not assigned)' },
                ...library.map((m) => ({
                  value: String(m.id),
                  label: m.name,
                  hint: m.capabilities[required] ? undefined : `— no ${required}`,
                })),
              ]}
            />
          </Field>
        </div>
        <button className="btn btn--primary" disabled={!dirty || busy} onClick={() => void apply()}>
          {busy ? 'Saving…' : 'Apply'}
        </button>
      </div>
      {/* Hints sit outside the 420px column so they use the card's full width. */}
      <div className="field-hint" style={{ marginTop: 6 }}>
        <div>Needs the “{required}” capability.</div>
        <div>Recommended: {meta.recommended}.</div>
        {meta.note && <div>{meta.note}</div>}
      </div>
      {selectionLacksCapability && (
        <p className="field-error" style={{ marginTop: 8 }}>
          {selectedModel!.name} didn’t pass the {required} probe — this role won’t work until a{' '}
          {required}-capable model is assigned.
        </p>
      )}
    </Card>
  )
}
