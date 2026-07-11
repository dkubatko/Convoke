import { Fragment, useState } from 'react'
import { api, ApiError } from '../lib/api'
import { AppSetting } from '../lib/types'
import { useQuery } from '../hooks/useQuery'
import { useToast } from './Toast'
import { Card, CardSkeleton, Check, ErrorNote } from './ui'
import { LevelSlider } from './LevelSlider'

/** Reusable editor for a set of tunables served by `endpoint` (global or
    per-chat). Renders a save-on-demand form; only changed keys are sent. */
export default function SettingsEditor({ endpoint }: { endpoint: string }) {
  const settings = useQuery<AppSetting[]>(() => api.get(endpoint), [endpoint])
  const toast = useToast()
  const [edits, setEdits] = useState<Record<string, number>>({})
  // Raw text for a number field mid-edit, so it can be cleared to empty without
  // snapping back to 0 on every keystroke.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)

  const data = settings.data ?? []
  const valueOf = (s: AppSetting) => (s.key in edits ? edits[s.key] : s.value)
  const dirty = data.filter((s) => s.key in edits && edits[s.key] !== s.value)
  const stage = (key: string, value: number) => setEdits((p) => ({ ...p, [key]: value }))
  const clearDraft = (key: string) =>
    setDrafts((d) => {
      if (!(key in d)) return d
      const next = { ...d }
      delete next[key]
      return next
    })

  async function save() {
    if (!dirty.length) return
    setBusy(true)
    try {
      await api.put(
        endpoint,
        dirty.map((s) => ({ key: s.key, value: edits[s.key] })),
      )
      toast('ok', 'Saved — applies within a few seconds')
      // Refetch first, THEN drop the local edits — so the fresh server values
      // are already in place and the fields never flash back to the old ones.
      await settings.refetch()
      setEdits({})
    } catch (err) {
      toast('err', err instanceof ApiError ? err.message : 'Couldn’t save settings')
    } finally {
      setBusy(false)
    }
  }

  // Discard every staged edit and revert to the saved values.
  function cancel() {
    setEdits({})
    setDrafts({})
  }

  return (
    // pad=false: the list owns its padding (horizontal only), so no vertical card
    // padding adds space above the first group or below the last; the action row
    // is a padded footer inside the card.
    <Card pad={false}>
      {settings.loading ? (
        <div className="card-pad">
          <CardSkeleton lines={4} />
        </div>
      ) : settings.error ? (
        <div className="card-pad">
          <ErrorNote message={settings.error} onRetry={() => void settings.refetch()} />
        </div>
      ) : (
        <>
          <div className="setting-list">
            {data.map((s, i) => {
              const val = valueOf(s)
              const changed = s.key in edits && edits[s.key] !== s.value
              const atDefault = val === s.default
              const header =
                s.group && (i === 0 || data[i - 1].group !== s.group) ? (
                  <div className="setting-group">
                    <span className="setting-group-label">{s.group}</span>
                  </div>
                ) : null
              // Minimal circular-arrow reset that keeps its footprint even when
              // hidden, so it never shifts the value.
              const resetBtn = (
                <button
                  type="button"
                  className={`reset-btn${atDefault ? ' reset-btn--off' : ''}`}
                  title="Reset to default"
                  aria-label="Reset to default"
                  tabIndex={atDefault ? -1 : 0}
                  onClick={() => stage(s.key, s.default)}
                >
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                    <path d="M3 3v5h5" />
                  </svg>
                </button>
              )
              // A boolean-shaped setting (0..1, no step labels) is a bare
              // toggle — the label carries the meaning, the checkbox carries
              // no caption.
              if (s.minimum === 0 && s.maximum === 1 && !s.step_labels) {
                return (
                  <Fragment key={s.key}>
                    {header}
                    <div className={`setting setting--row${changed ? ' setting--changed' : ''}`}>
                      <div className="setting-main">
                        <label className="setting-label">{s.label}</label>
                        <p className="setting-desc">{s.description}</p>
                      </div>
                      <div className="setting-control">
                        <div className="setting-value">
                          {resetBtn}
                          {/* Same 64px column as .setting-num, so the box
                              centers on the shared axis of the inputs. */}
                          <span className="setting-bool">
                            <Check
                              checked={val === 1}
                              onChange={(v) => stage(s.key, v ? 1 : 0)}
                            >
                              {''}
                            </Check>
                          </span>
                        </div>
                      </div>
                    </div>
                  </Fragment>
                )
              }
              // A step-labelled knob renders as a lever; everything else is a
              // numeric box. Both put the reset + control in the right column.
              if (s.step_labels) {
                return (
                  <Fragment key={s.key}>
                    {header}
                    <div className={`setting setting--row${changed ? ' setting--changed' : ''}`}>
                      <div className="setting-main">
                        <label className="setting-label">{s.label}</label>
                        <p className="setting-desc">{s.description}</p>
                      </div>
                      <div className="setting-control setting-control--lever">
                        <div className="setting-value">
                          {resetBtn}
                          <LevelSlider
                            value={val}
                            min={s.minimum}
                            max={s.maximum}
                            labels={s.step_labels}
                            ariaLabel={s.label}
                            onChange={(v) => stage(s.key, v)}
                          />
                        </div>
                      </div>
                    </div>
                  </Fragment>
                )
              }
              return (
                <Fragment key={s.key}>
                  {header}
                  <div className={`setting setting--row${changed ? ' setting--changed' : ''}`}>
                    <div className="setting-main">
                      <label className="setting-label" htmlFor={s.key}>
                        {s.label}
                      </label>
                      <p className="setting-desc">{s.description}</p>
                    </div>
                    <div className="setting-control">
                      <div className="setting-value">
                        {resetBtn}
                        <input
                          id={s.key}
                          type="number"
                          inputMode="numeric"
                          className="mono setting-num"
                          min={s.minimum}
                          max={s.maximum}
                          value={s.key in drafts ? drafts[s.key] : val}
                          onChange={(e) => {
                            const raw = e.target.value
                            if (raw === '') {
                              setDrafts((d) => ({ ...d, [s.key]: '' }))
                            } else {
                              clearDraft(s.key)
                              stage(s.key, Number(raw))
                            }
                          }}
                          onBlur={() => {
                            if (drafts[s.key] === '') stage(s.key, s.minimum)
                            clearDraft(s.key)
                          }}
                        />
                      </div>
                      <span className="unit">{s.unit}</span>
                    </div>
                  </div>
                </Fragment>
              )
            })}
          </div>

          <div className="settings-actions">
            <button
              className="btn btn--primary"
              disabled={busy || dirty.length === 0}
              onClick={() => void save()}
            >
              {busy ? 'Saving…' : dirty.length ? `Save ${dirty.length}` : 'Saved'}
            </button>
            {dirty.length > 0 && (
              <button type="button" className="btn btn--quiet" disabled={busy} onClick={cancel}>
                Cancel
              </button>
            )}
          </div>
        </>
      )}
    </Card>
  )
}
