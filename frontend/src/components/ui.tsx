import { CSSProperties, ReactNode, useEffect, useRef, useState } from 'react'
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
  title: string
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
        <div className="card-pad" style={{ paddingBottom: 0 }}>
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

export function Skeleton({ width = '100%', height = 13, style }: {
  width?: number | string
  height?: number
  style?: React.CSSProperties
}) {
  return <span className="skeleton" style={{ width, height, display: 'inline-block', ...style }} />
}

/** Table-shaped placeholder: keeps row height/spacing so content doesn't jump in. */
export function TableSkeleton({ rows = 4 }: { rows?: number }) {
  const widths = [
    ['18%', '30%', '12%', '24%'],
    ['22%', '24%', '14%', '18%'],
    ['16%', '34%', '10%', '22%'],
  ]
  return (
    <div role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton-row">
          {widths[i % widths.length].map((w, j) => (
            <Skeleton key={j} width={w} height={j === 0 ? 15 : 12} />
          ))}
        </div>
      ))}
    </div>
  )
}

/** Card-shaped placeholder with a title bar and a few text lines. */
export function CardSkeleton({ lines = 3 }: { lines?: number }) {
  return (
    <section className="card card-pad" role="status" aria-label="Loading">
      <Skeleton width={140} height={11} style={{ marginBottom: 14 }} />
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton
          key={i}
          width={`${88 - i * 16}%`}
          height={12}
          style={{ display: 'block', marginBottom: 9 }}
        />
      ))}
    </section>
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
