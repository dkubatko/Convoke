import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { AppSetting } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from './Toast'
import { Card, CardSkeleton, ErrorNote } from './ui'

/** Reusable editor for a set of tunables served by `endpoint` (global or
    per-chat). Renders a save-on-demand form; only changed keys are sent. */
export default function SettingsEditor({
  endpoint,
  title,
  intro,
  collapsible = false,
}: {
  endpoint: string
  title: string
  intro?: string
  collapsible?: boolean
}) {
  const settings = useQuery<AppSetting[]>(() => api.get(endpoint), [endpoint])
  const toast = useToast()
  const [edits, setEdits] = useState<Record<string, number>>({})
  const [busy, setBusy] = useState(false)
  const [open, setOpen] = useState(!collapsible)

  const data = settings.data ?? []
  const valueOf = (s: AppSetting) => (s.key in edits ? edits[s.key] : s.value)
  const dirty = data.filter((s) => s.key in edits && edits[s.key] !== s.value)
  const stage = (key: string, value: number) => setEdits((p) => ({ ...p, [key]: value }))

  async function save() {
    if (!dirty.length) return
    setBusy(true)
    try {
      await api.put(
        endpoint,
        dirty.map((s) => ({ key: s.key, value: edits[s.key] })),
      )
      toast('ok', 'Saved — applies within a few seconds')
      setEdits({})
      void settings.refetch()
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t save settings')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card style={{ maxWidth: 720 }}>
      <div className="page-head-row">
        <button
          type="button"
          onClick={() => collapsible && setOpen((v) => !v)}
          style={{
            background: 'none', border: 'none', padding: 0, cursor: collapsible ? 'pointer' : 'default',
            font: 'inherit', color: 'inherit', display: 'flex', alignItems: 'center', gap: 8,
          }}
          aria-expanded={collapsible ? open : undefined}
        >
          <span className="card-title" style={{ margin: 0 }}>{title}</span>
          {collapsible && <span className="muted">{open ? '▾' : '▸'}</span>}
        </button>
        {open && (
          <button className="btn btn--primary btn--sm" disabled={busy || dirty.length === 0} onClick={() => void save()}>
            {busy ? 'Saving…' : dirty.length ? `Save ${dirty.length}` : 'Saved'}
          </button>
        )}
      </div>

      {open &&
        (settings.loading ? (
          <div style={{ marginTop: 12 }}><CardSkeleton lines={4} /></div>
        ) : settings.error ? (
          <div style={{ marginTop: 12 }}>
            <ErrorNote message={settings.error} onRetry={() => void settings.refetch()} />
          </div>
        ) : (
          <div style={{ marginTop: 12 }}>
            {intro && (
              <p className="muted" style={{ fontSize: 12.5, margin: '0 0 4px', maxWidth: 640 }}>
                {intro}
              </p>
            )}
            <div className="setting-list">
              {data.map((s) => {
                const val = valueOf(s)
                const changed = s.key in edits && edits[s.key] !== s.value
                return (
                  <div className="setting" key={s.key}>
                    <label className="setting-label" htmlFor={s.key}>
                      {s.label}
                      {changed && (
                        <span className="pill pill--warn">
                          <span className="lamp" aria-hidden />unsaved
                        </span>
                      )}
                    </label>
                    <div className="setting-control">
                      {s.step_labels ? (
                        <div className="steps" role="radiogroup" aria-label={s.label}>
                          {s.step_labels.map((lbl, i) => {
                            const v = s.minimum + i
                            return (
                              <button
                                type="button"
                                key={lbl}
                                role="radio"
                                aria-checked={val === v}
                                className={`step${val === v ? ' step--on' : ''}`}
                                onClick={() => stage(s.key, v)}
                              >
                                {lbl}
                              </button>
                            )
                          })}
                        </div>
                      ) : (
                        <>
                          <input
                            id={s.key}
                            type="number"
                            className="mono"
                            min={s.minimum}
                            max={s.maximum}
                            value={val}
                            onChange={(e) => stage(s.key, Number(e.target.value))}
                          />
                          <span className="unit">{s.unit}</span>
                        </>
                      )}
                    </div>
                    <p className="setting-desc">
                      {s.description}{' '}
                      {val === s.default ? (
                        <span style={{ opacity: 0.7 }}>Default.</span>
                      ) : (
                        <button type="button" className="linkbtn" onClick={() => stage(s.key, s.default)}>
                          Reset to default ({s.step_labels ? s.step_labels[s.default - s.minimum] : s.default})
                        </button>
                      )}
                    </p>
                  </div>
                )
              })}
            </div>
          </div>
        ))}
    </Card>
  )
}
