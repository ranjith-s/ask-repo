import { AlertCircle, CheckCircle2, GitBranch, Loader2, Plus, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import { BUSY_STATUSES, type Repo } from '../api'
import { phaseProgress } from '../progress'
import { ProgressBar } from './ProgressBar'

const STATUS_LABEL: Record<Repo['status'], string> = {
  pending: 'Starting',
  cloning: 'Downloading',
  parsing: 'Reading files',
  embedding: 'Making searchable',
  ready: 'Ready',
  failed: "Couldn't add",
}

interface Props {
  repos: Repo[]
  selectedId: number | null
  onSelect: (id: number) => void
  onIndex: (url: string) => Promise<void>
  onCancel: (id: number) => Promise<void>
  onRemove: (id: number) => Promise<void>
}

export function Sidebar({ repos, selectedId, onSelect, onIndex, onCancel, onRemove }: Props) {
  const [url, setUrl] = useState('')
  const [confirmingId, setConfirmingId] = useState<number | null>(null)
  const [rowError, setRowError] = useState<{ id: number; message: string } | null>(null)

  async function act(id: number, action: (id: number) => Promise<void>) {
    setRowError(null)
    try {
      await action(id)
      setConfirmingId(null)
    } catch (e) {
      setRowError({ id, message: (e as Error).message })
    }
  }
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
          Add a project
        </label>
        <div className="mt-2 flex gap-2">
          <input
            id="repo-url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="Paste a GitHub link"
            className="min-w-0 flex-1 rounded-md border border-stone-300 bg-white px-2.5 py-1.5 text-sm outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-stone-700 dark:bg-stone-950"
          />
          <button
            type="submit"
            disabled={submitting || !url.trim()}
            className="rounded-md bg-indigo-600 px-2.5 text-white hover:bg-indigo-500 disabled:opacity-40"
            aria-label="Add project"
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
            Not sure? Try an example project →
          </button>
        )}
        <p className="mt-3 text-xs leading-relaxed text-stone-500">
          Works with public GitHub projects in Python, JavaScript or TypeScript. Also reads documentation and settings files.
        </p>
      </form>

      <nav className="flex-1 overflow-y-auto p-2" aria-label="Projects">
        {repos.map((repo) => {
          const busy = BUSY_STATUSES.includes(repo.status)
          const stopping = busy && repo.cancel_requested
          const progress = stopping ? null : phaseProgress(repo)
          const active = repo.id === selectedId
          const label = stopping ? 'Stopping' : STATUS_LABEL[repo.status]
          return (
            <div
              key={repo.id}
              className={`group relative mb-1 rounded-lg transition ${
                active ? 'bg-indigo-50 ring-1 ring-indigo-200 dark:bg-indigo-950/50 dark:ring-indigo-900' : 'hover:bg-stone-100 dark:hover:bg-stone-800'
              }`}
            >
              <button onClick={() => onSelect(repo.id)} className="w-full rounded-lg px-3 py-2.5 pr-10 text-left">
                <div className="flex items-center gap-2">
                  <GitBranch className="size-3.5 shrink-0 text-stone-400" />
                  <span className="truncate text-sm font-medium">{repo.name}</span>
                </div>
                <div className="mt-1 flex items-center gap-1.5 pl-5.5 text-xs text-stone-500">
                  {busy && <Loader2 className="size-3 animate-spin text-indigo-500" />}
                  {repo.status === 'ready' && <CheckCircle2 className="size-3 text-emerald-500" />}
                  {repo.status === 'failed' && <AlertCircle className="size-3 text-red-500" />}
                  <span>{label}</span>
                  {progress && <span className="tabular-nums">{progress.percent}%</span>}
                  {repo.status === 'ready' && (
                    <span className="text-stone-400">· {repo.file_count.toLocaleString()} files</span>
                  )}
                </div>
                {progress && (
                  <ProgressBar percent={progress.percent} label={`${label} ${repo.name}`} className="mt-1.5 ml-5.5" />
                )}
                {repo.status === 'failed' && repo.error && <p className="mt-1 pl-5.5 text-xs text-red-600">{repo.error}</p>}
              </button>

              <RowAction repo={repo} busy={busy} stopping={stopping} confirming={confirmingId === repo.id}
                onAsk={() => setConfirmingId(repo.id)} onCancel={() => void act(repo.id, onCancel)} />

              {confirmingId === repo.id && (
                <div className="mx-3 mb-2.5 flex flex-wrap items-center gap-2 border-t border-stone-200 pt-2 text-xs dark:border-stone-700">
                  <span className="text-stone-600 dark:text-stone-300">Remove this project? You can add it again later.</span>
                  <button
                    onClick={() => void act(repo.id, onRemove)}
                    className="rounded-md bg-red-600 px-2 py-1 font-medium text-white hover:bg-red-500"
                  >
                    Remove
                  </button>
                  <button
                    onClick={() => setConfirmingId(null)}
                    className="rounded-md px-2 py-1 text-stone-600 hover:bg-stone-200 dark:text-stone-300 dark:hover:bg-stone-700"
                  >
                    Keep
                  </button>
                </div>
              )}
              {rowError?.id === repo.id && <p className="mx-3 mb-2 text-xs text-red-600">{rowError.message}</p>}
            </div>
          )
        })}
      </nav>
    </aside>
  )
}

/** The small button at the top right of a project row: cancel while setting up, else remove. */
function RowAction(props: {
  repo: Repo
  busy: boolean
  stopping: boolean
  confirming: boolean
  onAsk: () => void
  onCancel: () => void
}) {
  const { repo, busy, stopping, confirming } = props
  const base = 'absolute top-2 right-2 rounded-md p-1.5 text-stone-400 transition'
  if (busy) {
    return (
      <button
        onClick={props.onCancel}
        disabled={stopping}
        title={stopping ? 'Stopping…' : 'Cancel setup'}
        aria-label={stopping ? `Stopping setup of ${repo.name}` : `Cancel setting up ${repo.name}`}
        className={`${base} hover:bg-stone-200 hover:text-stone-700 disabled:opacity-50 dark:hover:bg-stone-700`}
      >
        {stopping ? <Loader2 className="size-3.5 animate-spin" /> : <X className="size-3.5" />}
      </button>
    )
  }
  if (confirming) return null
  return (
    <button
      onClick={props.onAsk}
      title="Remove project"
      aria-label={`Remove ${repo.name}`}
      className={`${base} opacity-0 group-hover:opacity-100 hover:bg-red-50 hover:text-red-600 focus-visible:opacity-100 max-md:opacity-100 dark:hover:bg-red-950/50`}
    >
      <Trash2 className="size-3.5" />
    </button>
  )
}
