import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from '../lib/api'

interface QueryState<T> {
  data: T | null
  error: string | null
  loading: boolean
  refetch: () => Promise<void>
}

/**
 * Minimal data hook: loading on first fetch, keeps stale data during
 * refetches, optional polling. Errors land in `error` as display-ready text.
 */
export function useQuery<T>(
  fn: () => Promise<T>,
  deps: unknown[],
  opts: { pollMs?: number; enabled?: boolean } = {},
): QueryState<T> {
  const { pollMs, enabled = true } = opts
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(enabled)
  const fnRef = useRef(fn)
  fnRef.current = fn
  // Guards against setState after unmount and against an older response
  // (previous deps, or a poll issued before a mutation) overwriting a newer
  // one. Every call takes a ticket; only the latest ticket may commit.
  const aliveRef = useRef(true)
  const ticketRef = useRef(0)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const refetch = useCallback(async () => {
    const ticket = ++ticketRef.current
    const isCurrent = () => aliveRef.current && ticket === ticketRef.current
    try {
      const result = await fnRef.current()
      if (isCurrent()) {
        setData(result)
        setError(null)
      }
    } catch (err) {
      if (isCurrent()) {
        setError(err instanceof ApiError ? err.message : 'Something went wrong loading this data')
      }
    } finally {
      if (isCurrent()) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    if (!enabled) return
    setLoading(true)
    void refetch()
  }, [refetch, enabled])

  useEffect(() => {
    if (!enabled || !pollMs) return
    const t = setInterval(() => void refetch(), pollMs)
    return () => clearInterval(t)
  }, [refetch, pollMs, enabled])

  return { data, error, loading, refetch }
}
