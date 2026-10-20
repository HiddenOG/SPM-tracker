import { authHeader, sessionEnded } from './session'

/*
  Talking to the FastAPI server. Every call carries the shared login; a 401
  means the session has ended, so the person is sent back to sign in.
*/

export class ApiError extends Error {
  status?: number
  constructor(message: string, status?: number) { super(message); this.status = status }
}

function headers(): Record<string, string> {
  const h: Record<string, string> = { 'Content-Type': 'application/json' }
  const a = authHeader()
  if (a) h.Authorization = a
  return h
}

async function failure(res: Response): Promise<ApiError> {
  if (res.status === 401) sessionEnded()
  let detail = ''
  try { detail = (await res.json()).detail || '' } catch { /* not JSON */ }
  return new ApiError(detail || `The server answered ${res.status}.`, res.status)
}

/** GET a JSON endpoint. */
export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, { headers: headers(), signal })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new ApiError("Couldn't reach the server. Check the connection and try again.")
  }
  if (!res.ok) throw await failure(res)
  return res.json() as Promise<T>
}

/** POST / PUT / PATCH / DELETE with an optional JSON body; the reply's JSON, if any. */
export async function send<T = unknown>(method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, { method, headers: headers(), body: body === undefined ? undefined : JSON.stringify(body) })
  } catch {
    throw new ApiError("Couldn't reach the server. Check the connection and try again.")
  }
  if (!res.ok) throw await failure(res)
  const text = await res.text()
  let data: unknown = null
  try { data = text ? JSON.parse(text) : null } catch { /* not JSON */ }
  if (data && typeof data === 'object' && 'error' in data && (data as { error?: string }).error) {
    throw new ApiError(String((data as { error: string }).error))
  }
  return data as T
}

/** GET a file (a PDF, a spreadsheet) with the login attached. */
export async function getBlob(path: string): Promise<Blob> {
  const h: Record<string, string> = {}
  const a = authHeader()
  if (a) h.Authorization = a
  let res: Response
  try { res = await fetch(path, { headers: h }) }
  catch { throw new ApiError("Couldn't reach the server. Check the connection and try again.") }
  if (!res.ok) throw await failure(res)
  return res.blob()
}

/** Hand a file to the browser as a download. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename
  document.body.appendChild(a); a.click()
  setTimeout(() => { URL.revokeObjectURL(url); a.remove() }, 1000)
}

/** A file's contents as a data: URL (what the import endpoints expect). */
export function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(new ApiError('Could not read the file.'))
    r.readAsDataURL(file)
  })
}

/** Upload one file as multipart form data (the browser sets the content type). */
export async function upload<T = unknown>(path: string, file: File): Promise<T> {
  const fd = new FormData()
  fd.append('file', file, file.name)
  const h: Record<string, string> = {}
  const a = authHeader()
  if (a) h.Authorization = a
  let res: Response
  try { res = await fetch(path, { method: 'POST', headers: h, body: fd }) }
  catch { throw new ApiError("Couldn't reach the server. Check the connection and try again.") }
  if (!res.ok) throw await failure(res)
  return res.json() as Promise<T>
}

/**
  Read a reply as server-sent events, one JSON object each. With a body it is
  a POST; without one, a GET.
*/
export async function streamEvents<T>(path: string, body: unknown, onEvent: (ev: T) => void,
                                      signal?: AbortSignal): Promise<void> {
  let res: Response
  try {
    res = await fetch(path, body === undefined
      ? { headers: headers(), signal }
      : { method: 'POST', headers: headers(), body: JSON.stringify(body), signal })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    throw new ApiError("Couldn't reach the server. Check the connection and try again.")
  }
  if (!res.ok || !res.body) throw await failure(res)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const parts = buf.split('\n\n')
    buf = parts.pop() || ''
    for (const part of parts) {
      const line = part.trim()
      if (!line.startsWith('data: ')) continue
      const d = line.slice(6)
      if (d === '[DONE]') continue
      try { onEvent(JSON.parse(d) as T) } catch { /* a malformed event is skipped */ }
    }
  }
}
