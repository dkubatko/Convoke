import { EpisodeInfo, SlotSpec } from '../lib/types'
import { DEFAULT_FIRE_BAR, episodeBadge, openEpisodes } from '../lib/intent'
import { shortDateTime, timeAgo, truncate } from '../lib/format'
import { Chip } from './ui'

/** The tracked topics of an intent workflow in one chat — one row per episode,
    open first. The status-colored left rail reads like the console's signal
    lamps: a glance down the list shows every topic's lifecycle state. */
export function EpisodeList({
  episodes,
  requiredSlots,
  minFireConfidence,
  showClosed = 3,
}: {
  episodes: EpisodeInfo[]
  requiredSlots: SlotSpec[]
  minFireConfidence: number
  showClosed?: number
}) {
  // A backend older than this frontend omits min_fire_confidence entirely.
  const fireBar = minFireConfidence ?? DEFAULT_FIRE_BAR
  const open = openEpisodes(episodes)
  const closed = episodes.filter((e) => e.status === 'closed').slice(0, showClosed)
  if (open.length === 0 && closed.length === 0) return null
  return (
    <div>
      <div className="card-title">
        Tracked topics{open.length > 0 && ` · ${open.length} open`}
      </div>
      <div className="episodes">
        {[...open, ...closed].map((e) => (
          <EpisodeRow
            key={e.id}
            e={e}
            requiredSlots={requiredSlots}
            minFireConfidence={fireBar}
          />
        ))}
      </div>
    </div>
  )
}

function EpisodeRow({
  e,
  requiredSlots,
  minFireConfidence,
}: {
  e: EpisodeInfo
  requiredSlots: SlotSpec[]
  minFireConfidence: number
}) {
  const filled = Object.entries(e.slots ?? {})
  // Missing details only matter while the topic is still being gathered.
  const gathering = ['candidate', 'converged'].includes(e.status)
  const missing = gathering
    ? requiredSlots.filter((r) => !(r.name in (e.slots ?? {})))
    : []
  return (
    <div className={`episode episode--${e.status}`}>
      <div className="episode-head">
        <Chip {...episodeBadge(e)} />
        <span className={`episode-summary${e.summary ? '' : ' episode-summary--none'}`}>
          {e.summary || 'no summary yet'}
        </span>
        <span
          className="episode-when"
          title={`first seen ${shortDateTime(e.opened_at)}${
            e.fired_at ? ` · acted ${shortDateTime(e.fired_at)}` : ''
          }`}
        >
          {timeAgo(e.last_activity_at)}
        </span>
      </div>

      {(filled.length > 0 || missing.length > 0) && (
        <div className="episode-chips">
          {filled.map(([name, v]) => {
            // Confirmed vs probable only matters pre-fire; handled topics
            // keep neutral chips.
            const confirmed = (v.confidence ?? 0) >= minFireConfidence
            const cls = !gathering
              ? 'slotchip'
              : confirmed
                ? 'slotchip slotchip--confirmed'
                : 'slotchip slotchip--probable'
            const title = !gathering
              ? undefined
              : confirmed
                ? 'confirmed — counts toward firing'
                : `probable — needs to be restated or confirmed in the chat before it counts (requires ${minFireConfidence.toFixed(2)})`
            return (
              <span className={cls} key={name} title={title}>
                <span className="k">{name}</span>
                {String(v.value)}
                {gathering && !confirmed && (
                  <span className="conf">{(v.confidence ?? 0).toFixed(2)}</span>
                )}
              </span>
            )
          })}
          {missing.map((r) => (
            <span className="slotchip slotchip--missing" key={r.name} title={r.description}>
              <span className="k">{r.name}</span>?
            </span>
          ))}
        </div>
      )}

      {e.execution_summary &&
        (e.execution_summary.startsWith('Decided not to act') ? (
          // The agent reviewed this topic and stood down (NO_ACTION).
          <div className="episode-acted">
            <span className="muted" style={{ flex: 'none', fontWeight: 600, fontSize: 12 }}>
              ∅ no action
            </span>
            <span>{truncate(e.execution_summary.replace(/^Decided not to act\s*—?\s*/, ''), 160)}</span>
          </div>
        ) : (
          <div className="episode-acted">
            <span className="tick">✓ acted{e.fired_at ? ` ${timeAgo(e.fired_at)}` : ''}</span>
            <span>{truncate(e.execution_summary, 160)}</span>
          </div>
        ))}

      {e.status === 'converged' && (
        <div className="episode-note">
          ready — waiting out the rate limit; double-checks it&rsquo;s still wanted, then acts
        </div>
      )}
    </div>
  )
}
