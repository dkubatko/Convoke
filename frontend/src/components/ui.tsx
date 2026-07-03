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
}

const PILL_LABEL: Record<string, string> = {
  pending_auth: 'waiting for admin',
  confirm_wait: 'awaiting confirmation',
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
