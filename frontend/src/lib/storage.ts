/* localStorage access can throw (blocked-storage profiles, some webviews) —
   degrade to session-only behavior instead of crashing the component tree. */

export function safeStorageGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function safeStorageSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // state persists for this session only
  }
}
