export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      detail = (await resp.json()).detail ?? detail
    } catch {
      // non-JSON error body
    }
    throw new ApiError(resp.status, detail)
  }
  return resp.json() as Promise<T>
}

async function requestVoid(path: string, init?: RequestInit): Promise<void> {
  const resp = await fetch(path, { credentials: 'same-origin', ...init })
  if (!resp.ok) throw new ApiError(resp.status, resp.statusText)
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  delete: (path: string) => requestVoid(path, { method: 'DELETE' }),
}
