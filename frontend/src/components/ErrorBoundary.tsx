import { Component, ReactNode } from 'react'

interface State {
  error: Error | null
}

/** A render crash anywhere below would otherwise blank the whole app. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="empty" style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
          <div>
            <h4>Something broke in the interface</h4>
            <p className="mono" style={{ fontSize: 12 }}>{String(this.state.error)}</p>
            <button className="btn btn--primary" onClick={() => window.location.reload()}>
              Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
