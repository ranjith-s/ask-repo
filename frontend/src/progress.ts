import type { Repo } from './api'

export type Phase = 'cloning' | 'parsing' | 'embedding'

export interface PhaseProgress {
  phase: Phase
  percent: number // 0-100, whole number
  summary: string // e.g. "Downloading · 2.8 MB", "12 of 117 files", "1,280 of 2,292 sections"
  aside: string // right-hand detail: file being parsed, or time left
}

const PHASES: Phase[] = ['cloning', 'parsing', 'embedding']

/** Progress of the current indexing phase, or null when the repo is not being indexed. */
export function phaseProgress(repo: Repo): PhaseProgress | null {
  if (!PHASES.includes(repo.status as Phase)) return null
  const phase = repo.status as Phase
  const total = repo.progress_total ?? 0
  const done = total > 0 ? Math.min(repo.progress_done, total) : 0
  const percent = total > 0 ? Math.floor((100 * done) / total) : 0

  if (phase === 'cloning') {
    // Git reports a percentage (total is 100) and a stage, e.g. "Downloading · 2.8 MB".
    return { phase, percent, summary: repo.progress_detail ?? 'Connecting to GitHub…', aside: '' }
  }
  if (phase === 'parsing') {
    const summary = total > 0 ? `${done.toLocaleString()} of ${total.toLocaleString()} files` : 'Listing files…'
    return { phase, percent, summary, aside: repo.progress_detail ?? '' }
  }
  const summary = `${done.toLocaleString()} of ${total.toLocaleString()} sections`
  // After the last batch the results are saved; say so instead of "0 s left".
  const aside = repo.progress_detail ?? formatTimeLeft(secondsLeft(done, total, repo.phase_elapsed_s))
  return { phase, percent, summary, aside }
}

/** Remaining embedding time from the rate so far; null until there is enough data. */
export function secondsLeft(done: number, total: number, elapsed: number | null): number | null {
  // The first batch includes model warm-up, so wait for ~5% before estimating.
  if (total <= 0 || done < total * 0.05 || elapsed === null || elapsed < 2) return null
  return Math.max(0, (elapsed / done) * (total - done))
}

export function formatTimeLeft(seconds: number | null): string {
  if (seconds === null) return 'estimating time…'
  if (seconds < 5) return 'a few seconds left'
  if (seconds < 60) return `about ${Math.round(seconds / 5) * 5} s left`
  return `about ${Math.round(seconds / 60)} min left`
}
