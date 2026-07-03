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

  const refetch = useCallback(async () => {
    try {
      const result = await fnRef.current()
      setData(result)
      setError(null)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong loading this data')
    } finally {
      setLoading(false)
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
