import { CSSProperties, Fragment, ReactNode, useEffect, useRef, useState } from 'react'
import { ToolCall } from '../lib/types'
import { truncate } from '../lib/format'

/* Small shared primitives. Anything used on two or more pages lives here. */

/** The tools an agent called during a run, grouped by provider. Null = run
    predates capture (render nothing); [] = called no tools (render nothing).
    One chip per provider (MCP server name, or "built-in"); the chip is red if
    any of that provider's calls was retried. Hover a chip to see exactly which
    tools were called under it, with their arguments. */
export function ToolCalls({ calls }: { calls: ToolCall[] | null | undefined }) {
  if (!calls || calls.length === 0) return null
  // Group by provider, preserving first-seen order. Calls captured before
  // grouping have no provider — fall back to the (prefixed) tool name so they
  // still render as their own chip.
  const groups: { provider: string; calls: ToolCall[] }[] = []
  for (const c of calls) {
    const provider = c.provider ?? c.tool
    let g = groups.find((x) => x.provider === provider)
    if (!g) {
      g = { provider, calls: [] }
      groups.push(g)
    }
    g.calls.push(c)
  }
  return (
    <span className="toolcalls">
      {groups.map((g, i) => {
        const retried = g.calls.some((c) => !c.ok)
        return (
          <HoverCard
            key={i}
            align="right"
            content={
              <div className="hover-detail hover-detail--mono">
                <span className="hover-detail-label">{g.provider}</span>
                {g.calls.map((c, j) => (
                  <div key={j} className="toolcall-line">
                    <span className={c.ok ? '' : 'toolcall-line--err'}>
                      {c.tool}
                      {c.ok ? '' : ' · retried'}
                    </span>
                    {c.args ? <div className="toolcall-args">{c.args}</div> : null}
                  </div>
                ))}
              </div>
            }
          >
            <span className={`toolcall${retried ? ' toolcall--err' : ''}`}>
              {g.provider}
              {g.calls.length > 1 ? ` ·${g.calls.length}` : ''}
            </span>
          </HoverCard>
        )
      })}
    </span>
  )
}

/** Truncated text that reveals its full contents in a hover popover when it
    doesn't fit. Renders an em-dash for empty text and plain (un-hoverable) text
    when nothing is cut. */
export function HoverText({ text, max = 90, mono = false }: {
  text: string | null | undefined
  max?: number
  mono?: boolean
}) {
  const full = (text ?? '').trim()
  if (!full) return <>—</>
  if (full.length <= max) return <>{full}</>
  return (
    <HoverCard
      wide
      align="right"
      content={<div className={`hover-detail${mono ? ' hover-detail--mono' : ''}`}>{full}</div>}
    >
      <span className="hover-underline">{truncate(full, max)}</span>
    </HoverCard>
  )
}

export function PageHead({ title, lede, actions }: {
  title: ReactNode
  lede?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="page-head">
      <div className="page-head-row">
        <div>
          <h2>{title}</h2>
          <div className="wire" aria-hidden />
        </div>
        {actions}
      </div>
      {lede && <p className="lede">{lede}</p>}
    </header>
  )
}

export function Card({ title, children, pad = true, onClick, style }: {
  title?: string
  children: ReactNode
  pad?: boolean
  onClick?: () => void
  style?: CSSProperties
}) {
  return (
    <section className="card" onClick={onClick} style={style}>
      {title && (
        /* Flush cards hold a table.data whose cells pad 14px — align the
           title with the first column's text, not the 20px card padding. */
        <div className="card-pad" style={{ paddingBottom: 0, ...(pad ? {} : { paddingLeft: 14, paddingRight: 14 }) }}>
          <div className="card-title">{title}</div>
        </div>
      )}
      {pad ? (
        <div className="card-pad" style={title ? { paddingTop: 0 } : undefined}>{children}</div>
      ) : (
        children
      )}
    </section>
  )
}

export function Field({ label, hint, error, children }: {
  label: string
  hint?: ReactNode
  error?: string | null
  children: ReactNode
}) {
  return (
    <div className={`field${error ? ' field--invalid' : ''}`}>
      <label>{label}</label>
      {children}
      {error ? <span className="field-error">{error}</span> : hint && <span className="field-hint">{hint}</span>}
    </div>
  )
}

/** A labelled checkbox row. Only for a plain box + text — never wrap other
    interactive elements (a nested link would double-fire the toggle). */
export function Check({ checked, onChange, disabled, children }: {
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
  children: ReactNode
}) {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{children}</span>
    </label>
  )
}

/** A wrap of toggle chips for picking a handful of items (multi-select). */
export function ChoiceChips<T extends string | number>({ options, selected, onToggle }: {
  options: { value: T; label: string }[]
  selected: T[]
  onToggle: (value: T, on: boolean) => void
}) {
  return (
    <div className="choice-group">
      {options.map((o) => {
        const on = selected.includes(o.value)
        return (
          <button
            type="button"
            key={o.value}
            className={`choice${on ? ' choice--on' : ''}`}
            aria-pressed={on}
            onClick={() => onToggle(o.value, !on)}
          >
            <span className="choice-tick" aria-hidden>✓</span>
            {o.label}
          </button>
        )
      })}
    </div>
  )
}

export function EmptyState({ title, hint, action }: {
  title: string
  hint: string
  action?: ReactNode
}) {
  return (
    <div className="empty">
      <h4>{title}</h4>
      <p>{hint}</p>
      {action}
    </div>
  )
}

export function LoadingWire() {
  return <div className="wire wire--live loading-wire" role="status" aria-label="Loading" />
}

/* ---------- skeletons ----------
   Placeholders are built from the REAL components' markup and CSS classes
   (table.data, .kv, .pill dimensions, --ctl-h controls), so the loaded content
   lands exactly where its skeleton was — no reflow when data arrives. Static
   text (column headers, card titles, blurbs) renders as real text even while
   loading; only the data itself shimmers. */

/** A text-shaped bar: 1em tall, inherits the surrounding font, sits on the
    baseline — so it occupies exactly one line of whatever text it stands in
    for (a 13.5px table cell, a 26px page title, an 11px mono caption).
    Pass `text` (the real string, rendered invisible) instead of `w` when the
    bar sits in a content-sized track (auto grid/table column): the track then
    solves to the same width it will have once loaded. */
export function SkeletonText({ w = '70%', text }: { w?: number | string; text?: string }) {
  if (text !== undefined)
    return (
      <span className="skeleton skeleton--text skeleton--label" aria-hidden>
        {text}
      </span>
    )
  return <span className="skeleton skeleton--text" style={{ width: w }} />
}

/** Matches .pill (20px capsule). `shrink` lets it give way inside a nowrap
    flex row instead of overflowing a fixed table column (.skeleton is
    flex: none by default so bars hold their width elsewhere). */
export function SkeletonPill({ w = 76, shrink = false }: { w?: number; shrink?: boolean }) {
  return (
    <span
      className="skeleton skeleton--pill"
      style={{ width: w, ...(shrink ? { flex: '0 1 auto', minWidth: 0 } : {}) }}
    />
  )
}

/** Matches a .btn (36px) or .btn--sm (26px) footprint. `shrink` as on SkeletonPill. */
export function SkeletonButton({
  w = 76,
  sm = false,
  shrink = false,
}: {
  w?: number
  sm?: boolean
  shrink?: boolean
}) {
  return (
    <span
      className="skeleton skeleton--btn"
      style={{ width: w, height: sm ? 26 : 36, ...(shrink ? { flex: '0 1 auto', minWidth: 0 } : {}) }}
    />
  )
}

/** Matches the custom checkbox (18px rounded square). */
export function SkeletonCheckbox() {
  return <span className="skeleton skeleton--check" />
}

/** Matches an input/select (--ctl-h tall). */
export function SkeletonControl({ w = '100%' }: { w?: number | string }) {
  return <span className="skeleton skeleton--ctl" style={{ width: w }} />
}

/** Deterministic per-cell width variation — organic-looking, stable across
    re-renders (a skeleton that reshuffles is its own kind of jank). */
const TEXT_WIDTHS = ['72%', '54%', '86%', '63%', '78%', '48%']
const textWidth = (seed: number) => TEXT_WIDTHS[seed % TEXT_WIDTHS.length]

export interface SkeletonCol {
  /** Real column label; omit on every column for headerless tables. */
  header?: string
  kind?: 'text' | 'mono' | 'twoline' | 'para' | 'pill' | 'pills' | 'check' | 'actions'
  /** Fixed cell width, when the real table sizes this column. */
  w?: number | string
  /** Bar width inside the cell; defaults to a per-row variation. */
  bar?: number | string
  /** Pill count for kind 'pills' / button count for 'actions'. */
  n?: number
  /** 'twoline' sub-line font-size — MUST match the real cell's, or the row
      heights drift by fractions of a pixel per line-height difference. */
  sub?: number
  /** 'twoline' sub-line family: true (default) for mono sub-lines, false when
      the real sub-line is body sans (e.g. Bots), so the bar mirrors its font. */
  subMono?: boolean
}

function SkeletonCell({ col, seed }: { col: SkeletonCol; seed: number }) {
  switch (col.kind ?? 'text') {
    case 'mono':
      return (
        <span className="mono">
          <SkeletonText w={col.bar ?? textWidth(seed)} />
        </span>
      )
    case 'twoline':
      return (
        <>
          <b style={{ display: 'block' }}>
            <SkeletonText w={col.bar ?? textWidth(seed)} />
          </b>
          <div className={col.subMono === false ? 'muted' : 'muted mono'} style={{ fontSize: col.sub ?? 11.5 }}>
            <SkeletonText w="38%" />
          </div>
        </>
      )
    // Long free text (summaries, outcomes): two lines, the second shorter.
    // These tables use .data--rows2, which pins every row to the two-line
    // height — so the bars land exactly where wrapped text does.
    case 'para':
      return (
        <>
          <span style={{ display: 'block' }}>
            <SkeletonText w={col.bar ?? '92%'} />
          </span>
          <span style={{ display: 'block' }}>
            <SkeletonText w={textWidth(seed)} />
          </span>
        </>
      )
    case 'pill':
      return <SkeletonPill w={64 + ((seed * 7) % 3) * 14} />
    case 'pills':
      // nowrap + shrinkable: pill groups always occupy one line, like the
      // loaded cell — a wrapped second pill row would change the row height.
      return (
        <span className="row" style={{ gap: 4, flexWrap: 'nowrap', minWidth: 0 }}>
          {Array.from({ length: col.n ?? 2 }, (_, i) => (
            <SkeletonPill key={i} shrink w={48 + ((seed + i) % 3) * 16} />
          ))}
        </span>
      )
    case 'check':
      return <SkeletonCheckbox />
    case 'actions':
      return (
        <span className="row" style={{ justifyContent: 'flex-end', flexWrap: 'nowrap', minWidth: 0 }}>
          {Array.from({ length: col.n ?? 2 }, (_, i) => (
            <SkeletonButton key={i} sm shrink w={58 + ((seed + i) % 2) * 14} />
          ))}
        </span>
      )
    default:
      return <SkeletonText w={col.bar ?? textWidth(seed)} />
  }
}

/** The real thead of a table whose columns are described by SkeletonCol.
    Render this in the LOADED table too, with the same cols array the skeleton
    uses: table.data is table-layout fixed, so the shared `w` declarations are
    what pins skeleton and loaded columns to identical widths. */
export function TableHead({ cols }: { cols: SkeletonCol[] }) {
  return (
    <thead>
      <tr>
        {cols.map((c, j) => (
          <th key={j} style={c.w != null ? { width: c.w } : undefined}>
            {c.header ?? ''}
          </th>
        ))}
      </tr>
    </thead>
  )
}

/** A loading table.data: real header labels, skeleton rows whose cells are
    shaped like the real column content (pills, two-line names, buttons…). */
export function TableSkeleton({
  cols,
  rows = 3,
  className,
}: {
  cols: SkeletonCol[]
  rows?: number
  className?: string
}) {
  const hasHead = cols.some((c) => c.header !== undefined)
  return (
    <table className={className ? `data ${className}` : 'data'} role="status" aria-label="Loading">
      {hasHead && <TableHead cols={cols} />}
      <tbody>
        {Array.from({ length: rows }, (_, i) => (
          <tr key={i}>
            {cols.map((c, j) => (
              <td key={j} style={c.w != null ? { width: c.w } : undefined}>
                <SkeletonCell col={c} seed={i * cols.length + j} />
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** A loading .kv definition list — the shape of every detail card.
    Pass `labels` (the real dt strings, rendered as invisible-text bars): .kv's
    label column is content-sized (`auto 1fr`), so real label widths are the
    only way the value column starts where it will end up. */
export function KVSkeleton({ rows = 4, labels }: { rows?: number; labels?: string[] }) {
  const n = labels?.length ?? rows
  return (
    <dl className="kv" role="status" aria-label="Loading">
      {Array.from({ length: n }, (_, i) => (
        <Fragment key={i}>
          <dt>
            {labels ? <SkeletonText text={labels[i]} /> : <SkeletonText w={40 + ((i * 5) % 3) * 14} />}
          </dt>
          <dd>
            <SkeletonText w={textWidth(i)} />
          </dd>
        </Fragment>
      ))}
    </dl>
  )
}

export function ErrorNote({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="empty">
      <h4>Couldn't load this</h4>
      <p>{message}</p>
      {onRetry && (
        <button className="btn btn--quiet" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}

const PILL_TONE: Record<string, string> = {
  // chats
  authorized: 'ok',
  pending_auth: 'warn',
  left: 'idle',
  // bots
  active: 'ok',
  disabled: 'idle',
  // runs / fires / imports
  done: 'ok',
  declined: 'idle',
  running: 'accent',
  pending: 'warn',
  confirm_wait: 'warn',
  confirmed: 'accent',
  validating: 'accent',
  ingesting: 'accent',
  cancelled: 'idle',
  rejected: 'err',
  failed: 'err',
  error: 'err',
  // workflow examples
  ready: 'ok',
  fallback: 'warn',
  // trigger-state stages
  prefilter_skip: 'idle',
  no_match: 'idle',
  accumulating: 'accent',
  fired: 'ok',
  cooldown: 'warn',
  held: 'warn',
  throttled: 'warn',
  evaluating: 'accent',
  evaluating_prefilter: 'accent',
  classifier_error: 'err',
}

const PILL_LABEL: Record<string, string> = {
  pending_auth: 'waiting for admin',
  confirm_wait: 'awaiting confirmation',
  declined: 'agent declined',
  prefilter_skip: 'listening',
  no_match: 'listening · no intent',
  accumulating: 'gathering info',
  cooldown: 'cooling down',
  held: 'recently fired',
  throttled: 'rate-limited',
  evaluating: 'checking now',
  classifier_error: 'classifier error',
}

export function StatusPill({ status, live = false }: { status: string; live?: boolean }) {
  const tone = PILL_TONE[status] ?? 'idle'
  const pulse = live || status === 'running' || status === 'ingesting' || status === 'validating'
  return (
    <span className={`pill pill--${tone}${pulse ? ' pill--live' : ''}`}>
      <span className="lamp" aria-hidden />
      {PILL_LABEL[status] ?? status.replaceAll('_', ' ')}
    </span>
  )
}

/** Shows a styled popover card on hover/focus. Clicking the trigger PINS it open
    (so its text can be selected and copied) until you click outside or
    hover/click another hovercard. `wide` widens it for paragraphs; `align="right"`
    anchors it to the trigger's right edge so it can't overflow a right-hand
    table column. */
export function HoverCard({ children, content, wide = false, align = 'left' }: {
  children: ReactNode
  content: ReactNode
  wide?: boolean
  align?: 'left' | 'right'
}) {
  const [pinned, setPinned] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)
  // Stable per-instance identity, so the "another card became active" broadcast
  // can tell self from others.
  const id = useRef<object>({})

  // Opening/entering any OTHER hovercard closes this pinned one.
  useEffect(() => {
    const onActive = (e: Event) => {
      if ((e as CustomEvent).detail !== id.current) setPinned(false)
    }
    document.addEventListener('hovercard-active', onActive)
    return () => document.removeEventListener('hovercard-active', onActive)
  }, [])

  // While pinned, a click anywhere outside closes it.
  useEffect(() => {
    if (!pinned) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setPinned(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [pinned])

  const announce = () =>
    document.dispatchEvent(new CustomEvent('hovercard-active', { detail: id.current }))

  return (
    <span
      ref={ref}
      className={`hovercard${pinned ? ' hovercard--pinned' : ''}`}
      tabIndex={0}
      onMouseEnter={announce}
      onClick={() => {
        announce()
        setPinned((p) => !p)
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          announce()
          setPinned((p) => !p)
        } else if (e.key === 'Escape') {
          setPinned(false)
        }
      }}
    >
      {children}
      <span
        className={`hovercard-pop${wide ? ' hovercard-pop--wide' : ''}${align === 'right' ? ' hovercard-pop--right' : ''}`}
        role="tooltip"
        // Clicks inside the pinned popover (selecting text) must not bubble to
        // the trigger's toggle or count as an outside click.
        onClick={(e) => e.stopPropagation()}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {content}
      </span>
    </span>
  )
}

/** A row of sub-tabs, consistent across every page that has them. */
export function TabBar<T extends string>({ tabs, active, onSelect }: {
  tabs: readonly T[]
  active: T
  onSelect: (t: T) => void
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button
          key={t}
          role="tab"
          aria-selected={active === t}
          className={active === t ? 'active' : ''}
          onClick={() => onSelect(t)}
        >
          {t}
        </button>
      ))}
    </div>
  )
}

/** Generic labelled tone chip (when the label isn't a known status string). */
export function Chip({ label, tone = 'idle', live = false }: {
  label: string
  tone?: 'ok' | 'warn' | 'err' | 'accent' | 'idle'
  live?: boolean
}) {
  return (
    <span className={`pill pill--${tone}${live ? ' pill--live' : ''}`}>
      <span className="lamp" aria-hidden />
      {label}
    </span>
  )
}

/** The intent funnel: Prefilter → Classifier → Fire, coloured by how far the
    last evaluation got. 'stop' (amber) = this node deliberately blocked the
    run (dedup/rate limit working); 'held' (dashed) = downstream of a stop;
    'fail' (red) stays reserved for genuine rejections/errors. */
export function Funnel({ steps }: {
  steps: { name: string; status: 'pass' | 'fail' | 'wait' | 'skip' | 'stop' | 'held'; detail?: string }[]
}) {
  return (
    <div className="funnel" role="list">
      {steps.map((st, i) => (
        <div className="funnel-step" key={st.name} role="listitem">
          <div className={`funnel-node funnel-node--${st.status}`}>
            <span className="funnel-name">{st.name}</span>
            {st.detail && <span className="funnel-detail">{st.detail}</span>}
          </div>
          {i < steps.length - 1 && <span className="funnel-arrow" aria-hidden>→</span>}
        </div>
      ))}
    </div>
  )
}
