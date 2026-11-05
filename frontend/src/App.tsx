import { ArrowUp, Gauge, Layers, Loader2, Sparkles, Square, Zap } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, BUSY_STATUSES, type Location, type Mode, type Repo, type ServerConfig } from './api'
import { AnswerCard, type Turn } from './components/AnswerCard'
import { CodePanel } from './components/CodePanel'
import { ProgressBar } from './components/ProgressBar'
import { Sidebar } from './components/Sidebar'
import { phaseProgress } from './progress'

const PROVIDER_LABEL: Record<string, string> = { anthropic: 'Claude', gemini: 'Gemini', ollama: 'Ollama' }

// Names for the setup steps, shared with the sidebar's status line.
export const STEP_LABEL: Partial<Record<Repo['status'], string>> = {
  cloning: 'Download',
  parsing: 'Read files',
  embedding: 'Make searchable',
}

const SUGGESTIONS = [
  'What does this project do, and what are its main parts?',
  'How do I install it and start using it?',
  'What other software does it depend on?',
  'How does it handle errors?',
]

export default function App() {
  const [config, setConfig] = useState<ServerConfig | null>(null)
  const [repos, setRepos] = useState<Repo[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [conversations, setConversations] = useState<Record<number, Turn[]>>({})
  const [openLocation, setOpenLocation] = useState<Location | null>(null)
  const [mode, setMode] = useState<Mode>('deep')
  const abortRef = useRef<AbortController | null>(null)

  const refreshRepos = useCallback(async () => {
    const list = await api.listRepos()
    setRepos(list)
    setSelectedId((current) => current ?? list.find((r) => r.status === 'ready')?.id ?? list[0]?.id ?? null)
  }, [])

  useEffect(() => {
    void api.config().then(setConfig).catch(() => setConfig(null))
    void refreshRepos()
  }, [refreshRepos])

  // Poll while anything is being indexed.
  const anyBusy = repos.some((r) => BUSY_STATUSES.includes(r.status))
  useEffect(() => {
    if (!anyBusy) return
    const timer = setInterval(() => void refreshRepos(), 1500)
    return () => clearInterval(timer)
  }, [anyBusy, refreshRepos])

  const repo = useMemo(() => repos.find((r) => r.id === selectedId) ?? null, [repos, selectedId])
  const turns = useMemo(() => (selectedId ? (conversations[selectedId] ?? []) : []), [conversations, selectedId])
  const streaming = turns.some((t) => t.status === 'streaming')

  async function indexRepo(url: string) {
    const created = await api.indexRepo(url)
    await refreshRepos()
    setSelectedId(created.id)
    setOpenLocation(null)
  }

  function updateTurn(repoId: number, turnId: string, update: (t: Turn) => Turn) {
    setConversations((all) => ({
      ...all,
      [repoId]: (all[repoId] ?? []).map((t) => (t.id === turnId ? update(t) : t)),
    }))
  }

  async function ask(question: string) {
    if (!repo || repo.status !== 'ready' || streaming) return
    const repoId = repo.id
    const history = turns.filter((t) => t.status === 'done').map((t) => ({ question: t.question, answer: t.answer }))
    const turn: Turn = { id: crypto.randomUUID(), question, mode, steps: [], answer: '', status: 'streaming' }
    setConversations((all) => ({ ...all, [repoId]: [...(all[repoId] ?? []), turn] }))

    const controller = new AbortController()
    abortRef.current = controller
    try {
      await api.ask(
        repoId,
        { question, mode, history },
        (event) => {
          updateTurn(repoId, turn.id, (t) => {
            switch (event.type) {
              case 'step':
                // Any text before a tool call was the model narrating, not the answer.
                return { ...t, answer: '', steps: [...t.steps, { ...event, note: t.answer.trim() || undefined }] }
              case 'text':
                return { ...t, answer: t.answer + event.delta }
              case 'done':
                return { ...t, status: 'done', citations: event.citations, usage: event.usage, latencyMs: event.latency_ms, model: event.model }
              case 'error':
                return { ...t, status: 'error', error: event.message }
            }
          })
        },
        controller.signal,
      )
    } catch (e) {
      const aborted = (e as Error).name === 'AbortError'
      updateTurn(repoId, turn.id, (t) => ({ ...t, status: 'error', error: aborted ? 'Stopped.' : (e as Error).message }))
    } finally {
      abortRef.current = null
    }
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-stone-200 bg-white px-4 py-2.5 dark:border-stone-800 dark:bg-stone-900">
        <img src="/favicon.svg" alt="" className="size-6" />
        <h1 className="text-sm font-semibold tracking-tight">Ask Repo</h1>
        <span className="hidden text-xs text-stone-500 sm:inline">Ask questions about any GitHub project. Answers point to the exact code.</span>
        <div className="ml-auto flex items-center gap-2 text-xs text-stone-500">
          {config && (
            <span className="hidden items-center gap-1 rounded-full bg-stone-100 px-2 py-0.5 sm:flex dark:bg-stone-800">
              <Sparkles className="size-3" /> {PROVIDER_LABEL[config.provider] ?? config.provider} · {config.model}
            </span>
          )}
          {config?.llm_problem && (
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-800 dark:bg-amber-950 dark:text-amber-300">
              {config.llm_problem}
            </span>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <Sidebar repos={repos} selectedId={selectedId} onSelect={(id) => { setSelectedId(id); setOpenLocation(null) }} onIndex={indexRepo} />

        <main className={`flex min-h-0 flex-1 flex-col ${openLocation ? 'hidden lg:flex' : ''}`}>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto max-w-3xl space-y-6 px-4 py-6">
              {!repo && <EmptyState />}
              {repo && repo.status !== 'ready' && <IndexingState repo={repo} />}
              {repo?.status === 'ready' && turns.length === 0 && <Welcome repo={repo} onAsk={ask} />}
              {turns.map((t) => (
                <AnswerCard key={t.id} turn={t} onOpen={setOpenLocation} />
              ))}
              <ScrollAnchor dep={turns} />
            </div>
          </div>
          {repo?.status === 'ready' && (
            <Composer mode={mode} onMode={setMode} streaming={streaming} onAsk={ask} onStop={() => abortRef.current?.abort()} />
          )}
        </main>

        {openLocation && repo && (
          <div className="min-h-0 flex-1 lg:max-w-[48%]">
            <CodePanel repoId={repo.id} repoUrl={repo.url} commit={repo.commit_sha} location={openLocation} onClose={() => setOpenLocation(null)} />
          </div>
        )}
      </div>
    </div>
  )
}

function Composer(props: { mode: Mode; onMode: (m: Mode) => void; streaming: boolean; onAsk: (q: string) => void; onStop: () => void }) {
  const [text, setText] = useState('')
  const submit = () => {
    const q = text.trim()
    if (q.length < 3 || props.streaming) return
    props.onAsk(q)
    setText('')
  }
  return (
    <div className="border-t border-stone-200 bg-white/80 px-4 py-3 backdrop-blur dark:border-stone-800 dark:bg-stone-900/80">
      <div className="mx-auto max-w-3xl">
        <div className="flex items-end gap-2 rounded-xl border border-stone-300 bg-white p-2 focus-within:border-indigo-500 focus-within:ring-2 focus-within:ring-indigo-500/20 dark:border-stone-700 dark:bg-stone-950">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            rows={1}
            maxLength={2000}
            placeholder="Ask how something works or where to find it…"
            className="max-h-40 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none field-sizing-content"
            aria-label="Question"
          />
          {props.streaming ? (
            <button onClick={props.onStop} className="rounded-lg bg-stone-800 p-2 text-white hover:bg-stone-700" aria-label="Stop">
              <Square className="size-4" />
            </button>
          ) : (
            <button onClick={submit} disabled={text.trim().length < 3} className="rounded-lg bg-indigo-600 p-2 text-white hover:bg-indigo-500 disabled:opacity-30" aria-label="Ask">
              <ArrowUp className="size-4" />
            </button>
          )}
        </div>
        <div className="mt-2 flex items-center gap-1 text-xs">
          <ModeButton active={props.mode === 'deep'} onClick={() => props.onMode('deep')} icon={<Layers className="size-3.5" />} label="Thorough" hint="Looks through the code step by step before answering. Best for “how does it work” questions." />
          <ModeButton active={props.mode === 'quick'} onClick={() => props.onMode('quick')} icon={<Zap className="size-3.5" />} label="Quick" hint="Answers in one go from the most relevant code. Faster and cheaper, best for simple questions." />
          <span className="ml-auto hidden text-stone-400 sm:inline">Enter to send · Shift+Enter for a new line</span>
        </div>
      </div>
    </div>
  )
}

function ModeButton({ active, onClick, icon, label, hint }: { active: boolean; onClick: () => void; icon: React.ReactNode; label: string; hint: string }) {
  return (
    <button
      onClick={onClick}
      title={hint}
      aria-pressed={active}
      className={`flex items-center gap-1 rounded-md px-2 py-1 transition ${
        active ? 'bg-indigo-50 font-medium text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300' : 'text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800'
      }`}
    >
      {icon} {label}
    </button>
  )
}

function Welcome({ repo, onAsk }: { repo: Repo; onAsk: (q: string) => void }) {
  return (
    <div className="pt-8 text-center">
      <h2 className="text-xl font-semibold tracking-tight">Ask anything about {repo.name}</h2>
      <p className="mt-1 text-sm text-stone-500">
        <span title={repo.commit_sha ? `Version ${repo.commit_sha.slice(0, 7)} · ${repo.chunk_count.toLocaleString()} searchable sections` : undefined}>
          {repo.file_count.toLocaleString()} files read
        </span>
        . Every answer links to the lines of code it's based on, so you can check it.
      </p>
      <div className="mx-auto mt-6 grid max-w-2xl gap-2 text-left sm:grid-cols-2">
        {SUGGESTIONS.map((s) => (
          <button key={s} onClick={() => onAsk(s)} className="rounded-xl border border-stone-200 bg-white p-3 text-sm text-stone-700 transition hover:border-indigo-300 hover:shadow-sm dark:border-stone-800 dark:bg-stone-900 dark:text-stone-300 dark:hover:border-indigo-800">
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

function IndexingState({ repo }: { repo: Repo }) {
  if (repo.status === 'failed') {
    return <p className="pt-16 text-center text-sm text-red-600">Couldn't add this project. {repo.error}</p>
  }
  const steps: Repo['status'][] = ['cloning', 'parsing', 'embedding']
  const current = steps.indexOf(repo.status)
  const progress = phaseProgress(repo)
  return (
    <div className="pt-16 text-center">
      <Loader2 className="mx-auto size-6 animate-spin text-indigo-500" />
      <h2 className="mt-3 font-semibold">Getting {repo.name} ready</h2>
      <ol className="mx-auto mt-4 flex max-w-sm justify-center gap-4 text-xs">
        {steps.map((s, i) => (
          <li key={s} className={i < current ? 'text-emerald-600' : i === current ? 'font-medium text-indigo-600' : 'text-stone-400'}>
            {i < current ? '✓' : `${i + 1}.`} {STEP_LABEL[s]}
          </li>
        ))}
      </ol>
      <div className="mx-auto mt-5 max-w-sm">
        <ProgressBar percent={progress?.percent ?? 0} label={`Getting ${repo.name} ready: ${STEP_LABEL[repo.status] ?? 'starting'}`} />
        <p className="mt-2 flex justify-between gap-3 text-xs text-stone-500 tabular-nums">
          <span className="shrink-0">
            {progress ? `${progress.percent}% · ${progress.summary}` : 'Starting…'}
          </span>
          <span className="min-w-0 truncate font-mono text-[11px]" title={progress?.aside}>
            {progress?.aside}
          </span>
        </p>
      </div>
      {repo.status === 'embedding' && (
        <p className="mt-4 text-xs text-stone-400">This is the longest step: about a minute for a typical project.</p>
      )}
    </div>
  )
}

function EmptyState() {
  return (
    <div className="pt-20 text-center">
      <Gauge className="mx-auto size-8 text-stone-300" />
      <h2 className="mt-3 text-lg font-semibold">Add a GitHub project to get started</h2>
      <p className="mt-1 text-sm text-stone-500">Paste the link to a public project in the “Add a project” box, or try the example.</p>
    </div>
  )
}

function ScrollAnchor({ dep }: { dep: unknown }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    // Braces matter: scrollIntoView returns a Promise in newer browsers, and React
    // would treat a returned Promise as a (broken) cleanup function.
    ref.current?.scrollIntoView({ block: 'end' })
  }, [dep])
  return <div ref={ref} />
}
