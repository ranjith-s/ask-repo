// The only module that talks to the backend.

export type RepoStatus = 'pending' | 'cloning' | 'parsing' | 'embedding' | 'ready' | 'failed'

export interface Repo {
  id: number
  url: string
  name: string
  commit_sha: string | null
  status: RepoStatus
  error: string | null
  file_count: number
  chunk_count: number
  indexed_at: string | null
}

export interface Location {
  path: string
  start: number
  end: number
}

export interface Citation extends Location {
  valid: boolean
  grounded: boolean
}

export interface Usage {
  input_tokens: number
  output_tokens: number
  cache_write_tokens: number
  cache_read_tokens: number
  cost_usd: number
  priced: boolean
}

export type AskEvent =
  | { type: 'step'; tool: string; summary: string; input?: Record<string, unknown>; is_error?: boolean; duration_ms: number; locations: Location[] }
  | { type: 'text'; delta: string }
  | { type: 'done'; citations: Citation[]; usage: Usage; latency_ms: number; model: string }
  | { type: 'error'; message: string }

export type Mode = 'quick' | 'deep'

export interface ServerConfig {
  provider: 'anthropic' | 'gemini' | 'ollama'
  model: string
  llm_configured: boolean
  llm_problem: string | null
  embedding_model: string
}

export const BUSY_STATUSES: RepoStatus[] = ['pending', 'cloning', 'parsing', 'embedding']

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const detail = typeof body.detail === 'string' ? body.detail : res.statusText
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  config: () => fetch('/api/config').then((r) => json<ServerConfig>(r)),
  listRepos: () => fetch('/api/repos').then((r) => json<Repo[]>(r)),
  indexRepo: (url: string) =>
    fetch('/api/repos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    }).then((r) => json<Repo>(r)),
  getFile: (repoId: number, path: string) =>
    fetch(`/api/repos/${repoId}/file?path=${encodeURIComponent(path)}`).then((r) =>
      json<{ path: string; content: string }>(r),
    ),

  /** Stream answer events (server-sent events over a POST). */
  async ask(
    repoId: number,
    body: { question: string; mode: Mode; history: { question: string; answer: string }[] },
    onEvent: (e: AskEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await fetch(`/api/repos/${repoId}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    })
    if (!res.ok || !res.body) {
      await json(res) // throws with the server's message
      return
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const line = frame.split('\n').find((l) => l.startsWith('data: '))
        if (line) onEvent(JSON.parse(line.slice(6)) as AskEvent)
      }
    }
  },
}
