import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type Paper } from '../lib/api'

const pickerBtnCls =
  'flex max-w-[7rem] items-center gap-1 rounded-lg border border-neutral-200 bg-neutral-50 px-2 py-1 text-xs font-medium text-neutral-700 hover:bg-neutral-100 dark:border-neutral-600 dark:bg-neutral-800 dark:text-neutral-200 dark:hover:bg-neutral-700'

export default function PaperSeedPicker({
  seed,
  onSelect,
  onClear,
}: {
  seed: Paper | null
  onSelect: (p: Paper) => void
  onClear: () => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const ref = useRef<HTMLDivElement>(null)

  const candidates = useQuery({
    queryKey: ['chain-papers', query],
    queryFn: () => api.papers({ q: query, has_pdf: 'true', limit: 8 }),
    enabled: open && query.trim().length > 1,
  })

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  if (seed) {
    return (
      <div className="flex max-w-[10rem] shrink-0 items-center gap-1 rounded-lg border border-blue-200 bg-blue-50 px-2 py-1.5 text-xs dark:border-blue-900 dark:bg-blue-950">
        <span className="truncate font-medium text-blue-800 dark:text-blue-200" title={seed.title}>
          {seed.title.length > 20 ? `${seed.title.slice(0, 18)}…` : seed.title}
        </span>
        <button type="button" onClick={onClear} className="shrink-0 text-neutral-400 hover:text-neutral-600">
          ✕
        </button>
      </div>
    )
  }

  return (
    <div ref={ref} className="relative shrink-0">
      <button type="button" onClick={() => setOpen((v) => !v)} className={pickerBtnCls}>
        <span className="truncate">选起点论文</span>
        <span className="text-[10px] text-neutral-400">▾</span>
      </button>
      {open && (
        <div className="absolute bottom-full left-0 z-50 mb-1 w-72 rounded-lg border border-neutral-200 bg-white p-2 shadow-lg dark:border-neutral-700 dark:bg-neutral-800">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索有 PDF 的论文…"
            className="w-full rounded-md border border-neutral-300 px-2 py-1.5 text-xs outline-none focus:border-blue-500 dark:border-neutral-600 dark:bg-neutral-900"
            autoFocus
          />
          {candidates.data && candidates.data.items.length > 0 && (
            <ul className="mt-1 max-h-48 overflow-auto">
              {candidates.data.items.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => {
                      onSelect(p)
                      setQuery('')
                      setOpen(false)
                    }}
                    className="w-full rounded px-2 py-1.5 text-left text-xs hover:bg-neutral-100 dark:hover:bg-neutral-700"
                  >
                    <div className="truncate">{p.title}</div>
                    <div className="font-mono text-[10px] text-neutral-400">{p.bibcode}</div>
                  </button>
                </li>
              ))}
            </ul>
          )}
          {query.trim().length > 1 && candidates.data?.items.length === 0 && (
            <p className="mt-1 px-1 text-xs text-neutral-400">无匹配论文</p>
          )}
        </div>
      )}
    </div>
  )
}
