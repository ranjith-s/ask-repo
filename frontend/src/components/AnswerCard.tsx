import { AlertTriangle, ChevronRight, FileCode2, FolderTree, Search, ShieldCheck, TextSearch } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import Markdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Citation, Location, Usage } from '../api'
import { basename, findCitation, linkCitations, parseCiteHref } from '../citations'

export interface Step {
  tool: string
  summary: string
  is_error?: boolean
  duration_ms: number
  locations: Location[]
  note?: string // text the model wrote before deciding to call a tool
}

export interface Turn {
  id: string
  question: string
  mode: 'quick' | 'deep'
  steps: Step[]
  answer: string
  status: 'streaming' | 'done' | 'error'
  error?: string
  citations?: Citation[]
  usage?: Usage
  latencyMs?: number
  model?: string
}

const TOOL_ICON: Record<string, ReactNode> = {
  search_code: <Search className="size-3.5" />,
  grep: <TextSearch className="size-3.5" />,
  read_file: <FileCode2 className="size-3.5" />,
  list_files: <FolderTree className="size-3.5" />,
}

export function AnswerCard({ turn, onOpen }: { turn: Turn; onOpen: (loc: Location) => void }) {
  const [stepsOpen, setStepsOpen] = useState(false)
  const streaming = turn.status === 'streaming'
  const totalStepMs = turn.steps.reduce((sum, s) => sum + s.duration_ms, 0)

  return (
    <article className="space-y-3">
      <div className="flex justify-end">
        <p className="max-w-[85%] rounded-2xl rounded-br-sm bg-indigo-600 px-4 py-2.5 text-sm whitespace-pre-wrap text-white">
          {turn.question}
        </p>
      </div>

      <div className="rounded-2xl rounded-tl-sm border border-stone-200 bg-white p-4 shadow-sm dark:border-stone-800 dark:bg-stone-900">
        {turn.steps.length > 0 && (
          <div className="mb-3">
            <button
              onClick={() => setStepsOpen((o) => !o)}
              className="flex items-center gap-1.5 text-xs font-medium text-stone-500 hover:text-stone-800 dark:hover:text-stone-200"
              aria-expanded={stepsOpen}
            >
              <ChevronRight className={`size-3.5 transition ${stepsOpen ? 'rotate-90' : ''}`} />
              {streaming && !turn.answer ? 'Exploring' : 'Explored'} the repo: {turn.steps.length}{' '}
              {turn.steps.length === 1 ? 'step' : 'steps'}
              <span className="text-stone-400">· {(totalStepMs / 1000).toFixed(1)}s</span>
            </button>
            {(stepsOpen || (streaming && !turn.answer)) && (
              <ol className="mt-2 space-y-1.5 border-l border-stone-200 pl-3 dark:border-stone-800">
                {turn.steps.map((step, i) => (
                  <li key={i} className="text-xs">
                    {step.note && <p className="mb-1 text-stone-500 italic">{step.note}</p>}
                    <div className={`flex items-start gap-1.5 ${step.is_error ? 'text-amber-600' : 'text-stone-600 dark:text-stone-400'}`}>
                      <span className="mt-0.5 text-stone-400">{TOOL_ICON[step.tool] ?? <Search className="size-3.5" />}</span>
                      <span className="font-mono">{step.summary}</span>
                      <span className="ml-auto shrink-0 text-stone-400">{step.duration_ms}ms</span>
                    </div>
                    {step.tool === 'read_file' && step.locations[0] && (
                      <button onClick={() => onOpen(step.locations[0])} className="ml-5 text-indigo-600 hover:underline dark:text-indigo-400">
                        open
                      </button>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}

        {turn.status === 'error' ? (
          <p className="flex items-center gap-2 text-sm text-red-600">
            <AlertTriangle className="size-4" /> {turn.error}
          </p>
        ) : (
          <div
            className={`prose prose-sm prose-stone max-w-none dark:prose-invert prose-pre:bg-stone-100 prose-pre:text-stone-800 dark:prose-pre:bg-stone-950 dark:prose-pre:text-stone-200 prose-code:before:content-none prose-code:after:content-none ${
              streaming ? 'caret' : ''
            }`}
          >
            {!turn.answer && streaming && turn.steps.length === 0 && <p className="text-stone-400">Thinking…</p>}
            <Markdown
              remarkPlugins={[remarkGfm]}
              urlTransform={(url) => (url.startsWith('cite:') ? url : defaultUrlTransform(url))}
              components={{
                a: ({ href, children }) => {
                  const loc = href ? parseCiteHref(href) : null
                  if (!loc) return <a href={href} target="_blank" rel="noreferrer">{children}</a>
                  return <CitationChip loc={loc} citation={findCitation(turn.citations, loc)} onOpen={onOpen} />
                },
              }}
            >
              {linkCitations(turn.answer)}
            </Markdown>
          </div>
        )}

        {turn.status === 'done' && <Footer turn={turn} />}
      </div>
    </article>
  )
}

function CitationChip({ loc, citation, onOpen }: { loc: Location; citation?: Citation; onOpen: (l: Location) => void }) {
  const unverified = citation && !citation.grounded
  const title = !citation
    ? loc.path
    : !citation.valid
      ? `${loc.path}: this line range does not exist`
      : !citation.grounded
        ? `${loc.path}: the model cited lines it was not shown`
        : `${loc.path}: verified against the lines the model read`
  return (
    <button
      onClick={() => citation?.valid !== false && onOpen(loc)}
      title={title}
      className={`mx-0.5 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 align-baseline font-mono text-[11px] leading-none no-underline ring-1 transition ${
        unverified
          ? 'bg-amber-50 text-amber-800 ring-amber-200 hover:bg-amber-100 dark:bg-amber-950/40 dark:text-amber-300 dark:ring-amber-900'
          : 'bg-indigo-50 text-indigo-700 ring-indigo-200 hover:bg-indigo-100 dark:bg-indigo-950/40 dark:text-indigo-300 dark:ring-indigo-900'
      }`}
    >
      {unverified && <AlertTriangle className="size-3" />}
      {basename(loc.path)}:{loc.start}
      {loc.end !== loc.start && `-${loc.end}`}
    </button>
  )
}

function Footer({ turn }: { turn: Turn }) {
  const cites = turn.citations ?? []
  const grounded = cites.filter((c) => c.grounded).length
  const allGood = cites.length > 0 && grounded === cites.length
  const usage = turn.usage
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-stone-100 pt-3 text-[11px] text-stone-500 dark:border-stone-800">
      <span className="rounded bg-stone-100 px-1.5 py-0.5 font-medium uppercase dark:bg-stone-800">{turn.mode}</span>
      {cites.length > 0 ? (
        <span className={`flex items-center gap-1 ${allGood ? 'text-emerald-600' : 'text-amber-600'}`}>
          {allGood ? <ShieldCheck className="size-3.5" /> : <AlertTriangle className="size-3.5" />}
          {grounded}/{cites.length} citations verified
        </span>
      ) : (
        <span>no citations</span>
      )}
      {turn.latencyMs !== undefined && <span>{(turn.latencyMs / 1000).toFixed(1)}s</span>}
      {usage && (
        <span
          title={`uncached input ${usage.input_tokens} · cache read ${usage.cache_read_tokens} · cache write ${usage.cache_write_tokens} · output ${usage.output_tokens}`}
        >
          {totalIn(usage).toLocaleString()} in
          {usage.cache_read_tokens > 0 && ` (${Math.round((100 * usage.cache_read_tokens) / totalIn(usage))}% cached)`} /{' '}
          {usage.output_tokens.toLocaleString()} out tokens · {costLabel(usage)}
        </span>
      )}
      {turn.model && <span className="text-stone-400">{turn.model}</span>}
    </div>
  )
}

function totalIn(u: Usage): number {
  return u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
}

function costLabel(u: Usage): string {
  if (!u.priced) return 'cost n/a'
  return u.cost_usd === 0 ? 'free' : `$${u.cost_usd.toFixed(4)}`
}
