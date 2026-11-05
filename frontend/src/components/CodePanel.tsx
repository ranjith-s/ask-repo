import { FileCode2, Loader2, X } from 'lucide-react'
import { Highlight, themes } from 'prism-react-renderer'
import { useEffect, useRef, useState } from 'react'
import { api, type Location } from '../api'

interface Props {
  repoId: number
  repoUrl: string
  commit: string | null
  location: Location
  onClose: () => void
}

function usePrefersDark() {
  const [dark, setDark] = useState(() => window.matchMedia('(prefers-color-scheme: dark)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) => setDark(e.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])
  return dark
}

export function CodePanel({ repoId, repoUrl, commit, location, onClose }: Props) {
  const [file, setFile] = useState<{ path: string; content: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const dark = usePrefersDark()

  useEffect(() => {
    let cancelled = false
    setError(null)
    if (file?.path !== location.path) setFile(null)
    api
      .getFile(repoId, location.path)
      .then((f) => !cancelled && setFile(f))
      .catch((e: Error) => !cancelled && setError(e.message))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoId, location.path])

  useEffect(() => {
    const target = scroller.current?.querySelector(`[data-line="${location.start}"]`)
    target?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [file, location.start])

  const githubLink =
    repoUrl.startsWith('https://github.com/') && commit
      ? `${repoUrl}/blob/${commit}/${location.path}#L${location.start}-L${location.end}`
      : null

  return (
    <section className="flex h-full min-h-0 flex-col border-stone-200 bg-white lg:border-l dark:border-stone-800 dark:bg-stone-900" aria-label="Source viewer">
      <header className="flex items-center gap-2 border-b border-stone-200 px-4 py-2.5 dark:border-stone-800">
        <FileCode2 className="size-4 shrink-0 text-stone-400" />
        <span className="truncate font-mono text-xs">{location.path}</span>
        <span className="shrink-0 font-mono text-xs text-stone-400">
          L{location.start}
          {location.end !== location.start && `–${location.end}`}
        </span>
        {githubLink && (
          <a href={githubLink} target="_blank" rel="noreferrer" className="ml-auto shrink-0 text-xs text-indigo-600 hover:underline dark:text-indigo-400">
            Open on GitHub ↗
          </a>
        )}
        <button onClick={onClose} className={`${githubLink ? '' : 'ml-auto'} rounded p-1 text-stone-400 hover:bg-stone-100 hover:text-stone-700 dark:hover:bg-stone-800`} aria-label="Close source viewer">
          <X className="size-4" />
        </button>
      </header>
      <div ref={scroller} className="min-h-0 flex-1 overflow-auto">
        {error && <p className="p-4 text-sm text-red-600">{error}</p>}
        {!file && !error && (
          <div className="flex items-center gap-2 p-4 text-sm text-stone-500">
            <Loader2 className="size-4 animate-spin" /> Loading…
          </div>
        )}
        {file && (
          <Highlight theme={dark ? themes.vsDark : themes.github} code={file.content} language={languageFor(file.path)}>
            {({ tokens, getLineProps, getTokenProps }) => (
              <pre className="py-2 font-mono text-[12.5px] leading-5" style={{ background: 'transparent' }}>
                {tokens.map((line, i) => {
                  const n = i + 1
                  const hit = n >= location.start && n <= location.end
                  return (
                    <div
                      key={i}
                      data-line={n}
                      {...getLineProps({ line })}
                      className={`flex pr-4 ${hit ? 'bg-amber-100/70 dark:bg-amber-500/15' : ''}`}
                    >
                      <span className={`w-12 shrink-0 pr-3 text-right select-none ${hit ? 'text-amber-700 dark:text-amber-400' : 'text-stone-400'}`}>{n}</span>
                      <span className="whitespace-pre">
                        {line.map((token, key) => (
                          <span key={key} {...getTokenProps({ token })} />
                        ))}
                      </span>
                    </div>
                  )
                })}
              </pre>
            )}
          </Highlight>
        )}
      </div>
    </section>
  )
}

function languageFor(path: string): string {
  if (path.endsWith('.py')) return 'python'
  if (path.endsWith('.md')) return 'markdown'
  if (path.endsWith('.toml') || path.endsWith('.cfg') || path.endsWith('.ini')) return 'ini'
  if (path.endsWith('.yml') || path.endsWith('.yaml')) return 'yaml'
  return 'clike'
}
