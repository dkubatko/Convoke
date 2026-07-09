export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

/** Fired when any request comes back 401 so the app can return to sign-in. */
export const UNAUTHORIZED_EVENT = 'convoke:unauthorized'

async function parseError(resp: Response): Promise<ApiError> {
  let detail = resp.statusText || `Request failed (${resp.status})`
  try {
    const body = await resp.json()
    if (typeof body.detail === 'string') {
      detail = body.detail
    } else if (Array.isArray(body.detail)) {
      // FastAPI 422 validation errors: [{loc, msg, …}, …] → readable text.
      const msgs = body.detail
        .map((d: { msg?: string; loc?: (string | number)[] }) =>
          d?.msg ? [d.loc?.slice(1).join('.'), d.msg].filter(Boolean).join(': ') : null,
        )
        .filter(Boolean)
      if (msgs.length) detail = msgs.join('; ')
    }
  } catch {
    // non-JSON error body
  }
  return new ApiError(resp.status, detail)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, { credentials: 'same-origin', ...init })
  if (resp.status === 401 && !path.startsWith('/api/auth/')) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
  }
  if (!resp.ok) throw await parseError(resp)
  if (resp.status === 204) return undefined as T
  return resp.json() as Promise<T>
}

const json = (body: unknown): RequestInit => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', ...(body === undefined ? {} : json(body)) }),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', ...json(body) }),
  delete: <T = void>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload<T>(path: string, file: File): Promise<T> {
    const form = new FormData()
    form.append('file', file)
    return request<T>(path, { method: 'POST', body: form })
  },
}
