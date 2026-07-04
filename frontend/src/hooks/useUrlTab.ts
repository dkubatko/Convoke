import { useSearchParams } from 'react-router-dom'

/** Keeps the active sub-tab in the URL (?tab=) so a refresh or shared link
    lands on the same tab. Shared by every page with sub-tabs. */
export function useUrlTab<T extends string>(
  tabs: readonly T[],
  fallback: T,
): [T, (t: T) => void] {
  const [params, setParams] = useSearchParams()
  const current = params.get('tab')
  const tab = (tabs as readonly string[]).includes(current ?? '') ? (current as T) : fallback
  const setTab = (t: T) =>
    setParams(
      (prev) => {
        prev.set('tab', t)
        return prev
      },
      { replace: true },
    )
  return [tab, setTab]
}
