import { FormEvent, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { timeAgo } from '../lib/format'
import { ConnectedModel, EmbedderRole, EmbeddingsInfo, ModelTestResult, RoleAssignment } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/ConfirmDialog'
import {
  Card,
  Check,
  EmptyState,
  ErrorNote,
  Field,
  PageHead,
  SkeletonButton,
  SkeletonControl,
  SkeletonPill,
  TabBar,
  SkeletonCol,
  TableHead,
  TableSkeleton,
} from '../components/ui'
import SettingsEditor from '../components/SettingsEditor'
import { Select, SelectOption } from '../components/Select'
import { useUrlTab } from '../hooks/useUrlTab'

const SUBTABS = ['Role assignment', 'Model library', 'Settings'] as const

/* Shared column spec for skeleton and loaded table (fixed layout) — widths
   match what auto layout solved for typical data, keeping the look unchanged. */
const MODEL_COLS: SkeletonCol[] = [
  { header: 'Name', w: '10.5%', kind: 'twoline', bar: 110, sub: 12 },
  { header: 'Endpoint', w: '19.5%', kind: 'mono', bar: '80%' },
  { header: 'Capabilities', w: '29%', kind: 'pills', n: 4 },
  { header: 'Roles', w: '19.5%', kind: 'pill' },
  { header: 'Tested', w: '8%', bar: 70 },
  { header: '', w: '13.5%', kind: 'actions' },
]

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
  const [apiDialect, setApiDialect] = useState('chat')
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
        api: apiDialect,
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
        api: apiDialect,
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
      setApiDialect('chat')
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
        api: m.api,
        api_key: null, // reuse the stored key
        model_id: m.id,
      })
      await api.put(`/api/models/${m.id}`, {
        name: m.name,
        base_url: m.base_url,
        model_name: m.model_name,
        api: m.api,
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
            <Field
              label="API"
              hint="Responses (OpenAI's agent API) keeps reasoning across tool calls; some models allow reasoning with tools only there. Most endpoints only speak chat/completions."
            >
              <Select
                value={apiDialect}
                onChange={edit(setApiDialect)}
                ariaLabel="Endpoint API dialect"
                options={[
                  { value: 'chat', label: 'chat/completions (standard)' },
                  { value: 'responses', label: 'responses (OpenAI agent API)' },
                ]}
              />
            </Field>
          </div>
          <Check checked={video} onChange={setVideo}>
            Accepts video input (can’t be probed reliably — check only if the endpoint documents it)
          </Check>
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
          <TableSkeleton rows={4} cols={MODEL_COLS} />
        ) : models.error ? (
          <ErrorNote message={models.error} onRetry={() => onChanged()} />
        ) : (models.data ?? []).length === 0 ? (
          <EmptyState
            title="No models connected yet"
            hint="Connect at least one chat-capable model, then assign it to the agent role."
          />
        ) : (
          <table className="data">
            <TableHead cols={MODEL_COLS} />
            <tbody>
              {models.data!.map((m) => (
                <tr key={m.id}>
                  <td>
                    <b>{m.name}</b>
                    <div className="mono muted" style={{ fontSize: 12 }}>
                      {m.model_name}
                      {m.api === 'responses' && ' · responses API'}
                    </div>
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
  if (roles.error) return <ErrorNote message={roles.error} onRetry={onChanged} />

  const loading = models.loading || roles.loading
  const byRole = new Map((roles.data ?? []).map((r) => [r.role, r]))
  return (
    <div className="stack">
      {/* The embeddings cards fetch their own state — they render (with their
          own loading pill) while the library and role queries are in flight. */}
      <EmbeddingsCard role="memory" />
      <EmbeddingsCard role="intent" />
      {ROLES.map((meta) =>
        loading ? (
          <RoleCardSkeleton key={meta.role} meta={meta} />
        ) : (
          <RoleCard
            key={meta.role}
            meta={meta}
            assignment={byRole.get(meta.role)}
            library={models.data ?? []}
            onChanged={onChanged}
          />
        ),
      )}
    </div>
  )
}

/** The capability a role needs is fixed per role but delivered via the
    assignment payload — derive it from the role name too, so the skeleton and
    an unassigned RoleCard render the same hint the assignment will confirm. */
function requiredCapability(meta: (typeof ROLES)[number], assignment?: RoleAssignment) {
  return (
    assignment?.required_capability ??
    (['vision', 'transcription', 'video'].includes(meta.role) ? meta.role : 'chat')
  )
}

/** Shared by RoleCard and its skeleton — the hint is static text that must be
    byte-identical in both, or it swaps visibly when loading finishes. */
function RoleHint({ meta, required }: { meta: (typeof ROLES)[number]; required: string }) {
  return (
    <div className="field-hint" style={{ marginTop: 6 }}>
      <div>Needs the “{required}” capability.</div>
      <div>Recommended: {meta.recommended}.</div>
      <div>
        Reasoning: Default omits the parameter; a picked level is verified with a live
        probe when you Apply — models that don’t support it reject the save harmlessly.
      </div>
      {meta.note && <div>{meta.note}</div>}
    </div>
  )
}

/** A RoleCard before its assignment is known: every static part (title, blurb,
    labels, hints) is real text; only the status pill and picker shimmer. */
function RoleCardSkeleton({ meta }: { meta: (typeof ROLES)[number] }) {
  const required = requiredCapability(meta)
  return (
    <Card>
      <div className="page-head-row" style={{ marginBottom: 10 }} role="status" aria-label="Loading">
        <h3 style={{ fontSize: 16 }}>{meta.title}</h3>
        <SkeletonPill w={104} />
      </div>
      <p className="muted" style={{ marginBottom: 14 }}>{meta.blurb}</p>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <div style={{ flex: '0 1 420px' }}>
          <Field label="Model">
            <SkeletonControl />
          </Field>
        </div>
        <SkeletonButton w={68} />
      </div>
      <RoleHint meta={meta} required={required} />
    </Card>
  )
}

const CUSTOM_MODEL = '__custom__'

const EMBEDDING_COPY: Record<
  EmbedderRole,
  { title: string; blurb: string; confirmBody: string }
> = {
  memory: {
    title: 'Embeddings — chat memory',
    blurb:
      'Chat history search and remembered notes. Hybrid retrieval: this model powers the ' +
      'semantic channel; exact words and names are also matched lexically. Retrieval-trained ' +
      'multilingual models belong here. Switching (or re-picking the current model) re-cuts ' +
      'history into chunks sized by the “Memory chunk size” setting and rebuilds every vector.',
    confirmBody:
      'History is re-chunked for the selected model and every memory vector is rebuilt. ' +
      'Memory search and note recall return nothing until the rebuild finishes — on CPU ' +
      'this can take a while for a large history.',
  },
  intent: {
    title: 'Embeddings — intent prefilter',
    blurb:
      'The cheap gate that decides which conversation windows reach the intent classifier. ' +
      'Paraphrase/similarity models belong here — retrieval models rank topics poorly for ' +
      'this job. Switching rebuilds workflow example vectors and recalibrates thresholds.',
    confirmBody:
      'Workflow example vectors are rebuilt and every workflow threshold recalibrated. ' +
      'This is quick; meanwhile windows fall through to the classifier, so nothing is missed.',
  },
}

function EmbeddingsCard({ role }: { role: EmbedderRole }) {
  const toast = useToast()
  const confirm = useConfirm()
  const copy = EMBEDDING_COPY[role]
  const info = useQuery<EmbeddingsInfo>(
    () => api.get(`/api/embeddings/${role}`),
    [role],
    { pollMs: 5000 },
  )
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

  async function startRebuild(modelId: string, title: string, actionLabel: string) {
    if (!cur) return
    const ok = await confirm({ title, body: copy.confirmBody, actionLabel })
    if (!ok) return
    setBusy(true)
    try {
      await api.post(`/api/embeddings/${role}/model`, { model_id: modelId })
      toast('ok', `Rebuilding ${role} embeddings with ${modelId}…`)
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
        <h3 style={{ fontSize: 16 }}>{copy.title}</h3>
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
        ) : info.error ? (
          // A dead endpoint must not read as eternal loading.
          <span className="pill pill--err" title={info.error}>
            <span className="lamp" aria-hidden />
            unavailable
          </span>
        ) : (
          <span className="pill pill--idle">loading…</span>
        )}
      </div>
      <p className="muted" style={{ marginBottom: 14 }}>{copy.blurb}</p>
      <div className="row" style={{ alignItems: 'flex-end' }}>
        <div style={{ flex: '0 1 420px' }}>
          <Field label="Model">
            <Select
              value={effective}
              options={modelOptions}
              disabled={reembedding}
              onChange={setSelected}
              ariaLabel={`${role} embedding model`}
            />
          </Field>
        </div>
        <button
          className="btn btn--primary"
          disabled={!changed || reembedding || busy}
          onClick={() =>
            void startRebuild(targetId, `Switch ${role} embeddings to ${targetId}?`, 'Switch & re-embed')
          }
        >
          {reembedding ? 'Rebuilding…' : 'Switch & re-embed'}
        </button>
        {role === 'memory' && cur && (
          <button
            className="btn"
            disabled={changed || reembedding || busy}
            onClick={() =>
              void startRebuild(
                cur.model_id,
                'Rebuild the memory index?',
                'Rebuild',
              )
            }
            title="Re-cut history with the current chunk-size setting and re-embed, keeping the same model"
          >
            Rebuild index
          </button>
        )}
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

  const required = requiredCapability(meta, assignment)
  const selectedModel = library.find((m) => m.id === selected)
  const selectionLacksCapability = selectedModel != null && !selectedModel.capabilities[required]

  const savedEffort = assignment?.reasoning_effort ?? ''
  const PRESET_EFFORTS = ['', 'low', 'medium', 'high']
  // '' = Default (parameter omitted); CUSTOM_MODEL sentinel reused for a free-form level.
  const [effortPick, setEffortPick] = useState(
    PRESET_EFFORTS.includes(savedEffort) ? savedEffort : CUSTOM_MODEL,
  )
  const [customEffort, setCustomEffort] = useState(
    PRESET_EFFORTS.includes(savedEffort) ? '' : savedEffort,
  )
  const effort = effortPick === CUSTOM_MODEL ? customEffort.trim() : effortPick

  async function apply() {
    setBusy(true)
    try {
      if (selected === '') {
        await api.delete(`/api/model-roles/${meta.role}`)
        toast('ok', `Unassigned the ${meta.role} role`)
      } else {
        await api.put(`/api/model-roles/${meta.role}`, {
          model_id: selected,
          reasoning_effort: effort || null,
        })
        toast('ok', `${meta.role} role → ${selectedModel?.name ?? selected}`)
      }
      onChanged()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : `Couldn’t update the ${meta.role} role`)
    } finally {
      setBusy(false)
    }
  }

  const dirty = (assignment?.model_id ?? '') !== selected || effort !== savedEffort
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
        {selected !== '' && (
          <div style={{ flex: '0 0 170px' }}>
            <Field label="Reasoning">
              <Select
                value={effortPick}
                onChange={setEffortPick}
                ariaLabel={`${meta.title} reasoning level`}
                options={[
                  { value: '', label: 'Default' },
                  { value: 'low', label: 'low' },
                  { value: 'medium', label: 'medium' },
                  { value: 'high', label: 'high' },
                  { value: CUSTOM_MODEL, label: 'Custom…' },
                ]}
              />
            </Field>
          </div>
        )}
        <button className="btn btn--primary" disabled={!dirty || busy} onClick={() => void apply()}>
          {busy ? 'Saving…' : 'Apply'}
        </button>
      </div>
      {selected !== '' && effortPick === CUSTOM_MODEL && (
        <div style={{ maxWidth: 170, marginTop: 10 }}>
          <Field label="Custom level">
            <input
              className="mono"
              value={customEffort}
              onChange={(e) => setCustomEffort(e.target.value)}
              placeholder="e.g. xhigh"
            />
          </Field>
        </div>
      )}
      {/* Hints sit outside the 420px column so they use the card's full width. */}
      <RoleHint meta={meta} required={required} />
      {selectionLacksCapability && (
        <p className="field-error" style={{ marginTop: 8 }}>
          {selectedModel!.name} didn’t pass the {required} probe — this role won’t work until a{' '}
          {required}-capable model is assigned.
        </p>
      )}
    </Card>
  )
}
