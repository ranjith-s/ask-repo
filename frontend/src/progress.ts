import type { Repo } from './api'

export interface EmbeddingProgress {
  done: number
  total: number
  percent: number // 0-100, whole number
  secondsLeft: number | null // null until there is enough data for a fair estimate
}

/** Progress of the embedding step, or null when the repo is not embedding. */
export function embeddingProgress(repo: Repo): EmbeddingProgress | null {
  if (repo.status !== 'embedding' || repo.chunk_count <= 0) return null
  const total = repo.chunk_count
  const done = Math.min(repo.embedded_count, total)
  const elapsed = repo.embedding_elapsed_s
  // The first batch includes model warm-up, so wait for ~5% before estimating.
  const enough = done >= total * 0.05 && elapsed !== null && elapsed >= 2
  return {
    done,
    total,
    percent: Math.floor((100 * done) / total),
    secondsLeft: enough ? Math.max(0, (elapsed / done) * (total - done)) : null,
  }
}

export function formatTimeLeft(seconds: number | null): string {
  if (seconds === null) return 'estimating time…'
  if (seconds < 5) return 'a few seconds left'
  if (seconds < 60) return `about ${Math.round(seconds / 5) * 5} s left`
  return `about ${Math.round(seconds / 60)} min left`
}
