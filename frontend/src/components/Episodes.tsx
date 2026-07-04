import { EpisodeInfo, SlotSpec } from '../lib/types'
import { episodeBadge, openEpisodes } from '../lib/intent'
import { shortDateTime, timeAgo, truncate } from '../lib/format'
import { Chip } from './ui'

/** The tracked topics of an intent workflow in one chat — one row per episode,
    open first. The status-colored left rail reads like the console's signal
    lamps: a glance down the list shows every topic's lifecycle state. */
export function EpisodeList({
  episodes,
  requiredSlots,
  showClosed = 3,
}: {
  episodes: EpisodeInfo[]
  requiredSlots: SlotSpec[]
  showClosed?: number
}) {
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
          <EpisodeRow key={e.id} e={e} requiredSlots={requiredSlots} />
        ))}
      </div>
    </div>
  )
}

function EpisodeRow({ e, requiredSlots }: { e: EpisodeInfo; requiredSlots: SlotSpec[] }) {
  const filled = Object.entries(e.slots ?? {})
  // Missing details only matter while the topic is still being gathered.
  const gathering = ['candidate', 'tracking', 'converged'].includes(e.status)
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
          {filled.map(([name, v]) => (
            <span className="slotchip" key={name}>
              <span className="k">{name}</span>
              {String(v.value)}
            </span>
          ))}
          {missing.map((r) => (
            <span className="slotchip slotchip--missing" key={r.name} title={r.description}>
              <span className="k">{r.name}</span>?
            </span>
          ))}
        </div>
      )}

      {e.execution_summary && (
        <div className="episode-acted">
          <span className="tick">✓ acted{e.fired_at ? ` ${timeAgo(e.fired_at)}` : ''}</span>
          <span>{truncate(e.execution_summary, 160)}</span>
        </div>
      )}

      {e.status === 'converged' && (
        <div className="episode-note">
          ready — waiting out the rate limit; double-checks it&rsquo;s still wanted, then acts
        </div>
      )}
    </div>
  )
}
