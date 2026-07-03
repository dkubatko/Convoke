import { ReactNode } from 'react'

/* Small shared primitives. Anything used on two or more pages lives here. */

export function PageHead({ title, lede, actions }: {
  title: string
  lede?: string
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

export function Card({ title, children, pad = true }: {
  title?: string
  children: ReactNode
  pad?: boolean
}) {
  return (
    <section className="card">
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
  hint?: string
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
  throttled: 'warn',
  classifier_error: 'err',
}

const PILL_LABEL: Record<string, string> = {
  pending_auth: 'waiting for admin',
  confirm_wait: 'awaiting confirmation',
  prefilter_skip: 'listening',
  no_match: 'listening · no intent',
  accumulating: 'gathering info',
  cooldown: 'cooling down',
  throttled: 'rate-limited',
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
