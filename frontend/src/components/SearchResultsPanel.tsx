import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { openExternalUrl } from '../lib/openExternal'

export default function SearchResultsPanel({
  mode,
  query,
  onSelectPaper,
}: {
  mode: 'search_semantic' | 'search_fts' | 'search_remote'
  query: string
  onSelectPaper: (id: number) => void
}) {
  const isRemote = mode === 'search_remote'
  const searchMode = mode === 'search_fts' ? 'fts' : 'semantic'

  const localQ = useQuery({
    queryKey: ['search', searchMode, query],
    queryFn: () => api.search(query, searchMode, 30),
    enabled: !!query && !isRemote,
    retry: false,
  })
  const remoteQ = useQuery({
    queryKey: ['searchRemote', query],
    queryFn: () => api.searchRemote(query),
    enabled: !!query && isRemote,
    retry: false,
  })

  if (!query) {
    return <p className="text-sm text-neutral-400">输入检索式开始搜索。</p>
  }

  if (!isRemote) {
    if (localQ.isFetching) return <p className="text-sm text-neutral-400">检索中…</p>
    if (localQ.error) return <p className="text-sm text-red-500">{String((localQ.error as Error).message)}</p>
    if (!localQ.data) return null
    return (
      <ul className="space-y-3">
        {localQ.data.results.map((r, i) => (
          <li
            key={`${r.paper_id}-${r.chunk_id ?? i}`}
            className="cursor-pointer rounded-lg border border-neutral-200 bg-white p-3 hover:border-blue-400 dark:border-neutral-800 dark:bg-neutral-900"
            onClick={() => onSelectPaper(r.paper_id)}
          >
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-sm font-semibold">{r.title}</span>
              {typeof r.distance === 'number' && (
                <span className="shrink-0 text-xs text-neutral-400">{(1 - r.distance).toFixed(3)}</span>
              )}
            </div>
            {r.bibcode && <div className="font-mono text-xs text-neutral-400">{r.bibcode}</div>}
            {r.snippet && <p className="mt-1 line-clamp-3 text-xs leading-relaxed text-neutral-500">{r.snippet}</p>}
          </li>
        ))}
        {localQ.data.results.length === 0 && <p className="text-sm text-neutral-400">无结果</p>}
      </ul>
    )
  }

  if (remoteQ.isFetching) return <p className="text-sm text-neutral-400">查询 ADS/arXiv…</p>
  if (!remoteQ.data) return null
  return (
    <ul className="space-y-3">
      {remoteQ.data.results.map((c, i) => (
        <li key={i} className="rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-900">
          <div className="text-sm font-semibold">{c.title}</div>
          <div className="mt-0.5 text-xs text-neutral-500">
            {c.authors.slice(0, 4).join('; ')}
            {c.authors.length > 4 ? ' 等' : ''} · {c.year ?? '?'} · {c.journal ?? ''}
          </div>
          <div className="mt-1 flex gap-2 text-xs">
            {c.ads_url && (
              <button
                type="button"
                className="text-blue-600 hover:underline dark:text-blue-400"
                onClick={() => openExternalUrl(c.ads_url!)}
              >
                ADS
              </button>
            )}
            {c.arxiv_url && (
              <button
                type="button"
                className="text-blue-600 hover:underline dark:text-blue-400"
                onClick={() => openExternalUrl(c.arxiv_url!)}
              >
                arXiv
              </button>
            )}
            <span className="text-neutral-400">score {c.score.toFixed(1)}</span>
          </div>
        </li>
      ))}
      {remoteQ.data.results.length === 0 && <p className="text-sm text-neutral-400">无结果</p>}
    </ul>
  )
}
