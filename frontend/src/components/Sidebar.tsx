import { AlertCircle, CheckCircle2, GitBranch, Loader2, Plus } from 'lucide-react'
import { useState } from 'react'
import { BUSY_STATUSES, type Repo } from '../api'

const STATUS_LABEL: Record<Repo['status'], string> = {
  pending: 'Queued',
  cloning: 'Cloning',
  parsing: 'Parsing code',
  embedding: 'Embedding',
  ready: 'Ready',
  failed: 'Failed',
}

interface Props {
  repos: Repo[]
  selectedId: number | null
  onSelect: (id: number) => void
  onIndex: (url: string) => Promise<void>
}

export function Sidebar({ repos, selectedId, onSelect, onIndex }: Props) {
  const [url, setUrl] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function submit(value: string) {
    setError(null)
    setSubmitting(true)
    try {
      await onIndex(value.trim())
      setUrl('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <aside className="flex w-full flex-col border-stone-200 max-md:max-h-72 bg-white md:w-72 md:border-r dark:border-stone-800 dark:bg-stone-900">
      <form
        className="border-b border-stone-200 p-4 dark:border-stone-800"
        onSubmit={(e) => {
          e.preventDefault()
          if (url.trim()) void submit(url)
        }}
      >
        <label htmlFor="repo-url" className="text-xs font-medium tracking-wide text-stone-500 uppercase">
          Add a repository
        </label>
        <div className="mt-2 flex gap-2">
          <input
            id="repo-url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            className="min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-stone-700 dark:bg-stone-950"
          />
          <button
            type="submit"
            disabled={submitting || !url.trim()}
            className="rounded-md bg-indigo-600 px-2.5 text-white hover:bg-indigo-500 disabled:opacity-40"
            aria-label="Index repository"
          >
            {submitting ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
          </button>
        </div>
        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
        {repos.length === 0 && (
          <button
            type="button"
            onClick={() => void submit('https://github.com/encode/starlette')}
            className="mt-3 text-xs text-indigo-600 hover:underline dark:text-indigo-400"
          >
            Try it with encode/starlette →
          </button>
        )}
        <p className="mt-3 text-xs leading-relaxed text-stone-500">
          Public GitHub repos. Python code, Markdown docs and config files are indexed.
        </p>
      </form>

      <nav className="flex-1 overflow-y-auto p-2" aria-label="Repositories">
        {repos.map((repo) => {
          const busy = BUSY_STATUSES.includes(repo.status)
          const active = repo.id === selectedId
          return (
            <button
              key={repo.id}
              onClick={() => onSelect(repo.id)}
              className={`mb-1 w-full rounded-lg px-3 py-2.5 text-left transition ${
                active ? 'bg-indigo-50 ring-1 ring-indigo-200 dark:bg-indigo-950/50 dark:ring-indigo-900' : 'hover:bg-stone-100 dark:hover:bg-stone-800'
              }`}
            >
              <div className="flex items-center gap-2">
                <GitBranch className="size-3.5 shrink-0 text-stone-400" />
                <span className="truncate text-sm font-medium">{repo.name}</span>
              </div>
              <div className="mt-1 flex items-center gap-1.5 pl-5.5 text-xs text-stone-500">
                {busy && <Loader2 className="size-3 animate-spin text-indigo-500" />}
                {repo.status === 'ready' && <CheckCircle2 className="size-3 text-emerald-500" />}
                {repo.status === 'failed' && <AlertCircle className="size-3 text-red-500" />}
                <span>{STATUS_LABEL[repo.status]}</span>
                {repo.status === 'ready' && (
                  <span className="text-stone-400">
                    · {repo.file_count} files · {repo.chunk_count.toLocaleString()} chunks
                  </span>
                )}
              </div>
              {repo.status === 'failed' && repo.error && (
                <p className="mt-1 pl-5.5 text-xs text-red-600">{repo.error}</p>
              )}
            </button>
          )
        })}
      </nav>
    </aside>
  )
}
