import type { Citation, Location } from './api'

// Matches the backend's citation format: [path/to/file.py:L10-L25] or [file.py:L7]
const CITATION = /\[([\w./-]+):L?(\d+)(?:\s*-\s*L?(\d+))?\]/g

/** Rewrite citations into markdown links with a `cite:` scheme the renderer turns into chips. */
export function linkCitations(markdown: string): string {
  return markdown.replace(CITATION, (_m, path: string, start: string, end?: string) => {
    const e = end ?? start
    return `[${path}:${start}-${e}](cite:${encodeURIComponent(path)}#${start}-${e})`
  })
}

export function parseCiteHref(href: string): Location | null {
  const match = /^cite:([^#]+)#(\d+)-(\d+)$/.exec(href)
  if (!match) return null
  return { path: decodeURIComponent(match[1]), start: Number(match[2]), end: Number(match[3]) }
}

export function findCitation(citations: Citation[] | undefined, loc: Location): Citation | undefined {
  return citations?.find((c) => c.path === loc.path && c.start === loc.start && c.end === loc.end)
}

export function basename(path: string): string {
  return path.split('/').pop() ?? path
}
