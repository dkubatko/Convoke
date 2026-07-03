import { createContext, ReactNode, useCallback, useContext, useRef, useState } from 'react'

type Kind = 'ok' | 'err' | 'info'

interface Toast {
  id: number
  kind: Kind
  message: string
}

interface ToastApi {
  push: (kind: Kind, message: string) => void
}

const ToastContext = createContext<ToastApi>({ push: () => {} })

/** Returns push(kind, message) directly: `toast('ok', 'Saved')`. */
export function useToast() {
  return useContext(ToastContext).push
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const push = useCallback((kind: Kind, message: string) => {
    const id = nextId.current++
    setToasts((t) => [...t, { id, kind, message }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === 'err' ? 8000 : 4500)
  }, [])

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast--${t.kind}`}>
            <span className="lamp" />
            <span>{t.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
