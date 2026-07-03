import { createContext, ReactNode, useCallback, useContext, useRef, useState } from 'react'

interface ConfirmOptions {
  title: string
  body: string
  actionLabel: string
  danger?: boolean
}

type Confirm = (opts: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<Confirm>(() => Promise.resolve(false))

export function useConfirm() {
  return useContext(ConfirmContext)
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [opts, setOpts] = useState<ConfirmOptions | null>(null)
  const resolver = useRef<(v: boolean) => void>(() => {})

  const confirm = useCallback<Confirm>((options) => {
    setOpts(options)
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve
    })
  }, [])

  function close(value: boolean) {
    resolver.current(value)
    setOpts(null)
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {opts && (
        <div className="dialog-backdrop" onClick={() => close(false)}>
          <div
            className="dialog"
            role="alertdialog"
            aria-modal="true"
            aria-label={opts.title}
            onClick={(e) => e.stopPropagation()}
          >
            <h3>{opts.title}</h3>
            <p>{opts.body}</p>
            <div className="row">
              <button className="btn btn--quiet" onClick={() => close(false)} autoFocus>
                Keep it
              </button>
              <button
                className={`btn ${opts.danger ? 'btn--danger' : 'btn--primary'}`}
                onClick={() => close(true)}
              >
                {opts.actionLabel}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  )
}
