import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { ApiError } from '../lib/api'

/* Debug switch: clicking the sidebar health pill flips every query to
   `loading`, so any page can be compared against its skeleton in place.
   Purely presentational — data and polling underneath are untouched, so
   toggling back restores the loaded view instantly. */
let forcedLoading = false
const forcedSubs = new Set<() => void>()
export const debugForcedLoading = {
  read: () => forcedLoading,
  subscribe: (fn: () => void) => {
    forcedSubs.add(fn)
    return () => void forcedSubs.delete(fn)
  },
  toggle: () => {
    forcedLoading = !forcedLoading
    forcedSubs.forEach((fn) => fn())
  },
}

interface QueryState<T> {
  data: T | null
  error: string | null
  loading: boolean
  refetch: () => Promise<void>
}

/* Last-known result per query identity. A remounted consumer (tab revisit,
   back/forward between detail pages) renders its previous data immediately and
   refreshes in place — no skeleton flash for data we've already shown. The
   identity is the query fn's source text plus its deps: the same request from
   two call sites coalesces; different params (deps) stay distinct. */
const queryCache = new Map<string, unknown>()

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
  const cacheKey = `${fn.toString()}|${JSON.stringify(deps)}`
  const [data, setData] = useState<T | null>(() => (queryCache.get(cacheKey) as T) ?? null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(enabled && !queryCache.has(cacheKey))
  // Derived-state swap for an in-place identity change (e.g. a chatId prop
  // changing without a remount): show the new key's cached data — or its
  // skeleton — instead of the previous key's stale result.
  const [prevKey, setPrevKey] = useState(cacheKey)
  if (prevKey !== cacheKey) {
    setPrevKey(cacheKey)
    setData((queryCache.get(cacheKey) as T | undefined) ?? null)
    setError(null)
    setLoading(enabled && !queryCache.has(cacheKey))
  }
  const keyRef = useRef(cacheKey)
  keyRef.current = cacheKey
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
    const key = keyRef.current // capture now: deps may change while in flight
    const isCurrent = () => aliveRef.current && ticket === ticketRef.current
    try {
      const result = await fnRef.current()
      // Cache whenever this is the latest request for the key — even if the
      // consumer unmounted mid-flight (tab switched away), the result is valid
      // and lets the next visit render instantly.
      if (ticket === ticketRef.current) queryCache.set(key, result)
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
    // Only skeleton when there's nothing cached to show — a remount or key
    // change with cached data refreshes silently, in place.
    if (!queryCache.has(keyRef.current)) setLoading(true)
    void refetch()
  }, [refetch, enabled])

  useEffect(() => {
    if (!enabled || !pollMs) return
    const t = setInterval(() => void refetch(), pollMs)
    return () => clearInterval(t)
  }, [refetch, pollMs, enabled])

  const forced = useSyncExternalStore(debugForcedLoading.subscribe, debugForcedLoading.read)
  return { data, error, loading: loading || forced, refetch }
}
