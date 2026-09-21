import { useMemo, useRef, useState, useEffect } from 'react'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useVirtualizer } from '@tanstack/react-virtual'
import { api, type Paper } from '../lib/api'
import PaperDetailPanel from '../components/PaperDetailPanel'
import { usePdfMatchAudit } from '../components/PdfMatchAuditPanel'
import { ProgressBar, useJobRunner } from '../components/JobRunner'
import { loadLibrarySession, saveLibrarySession, loadLastPdf, type LibrarySession } from '../lib/session'

const PAGE_SIZE = 200

function initialLibraryState() {
  const saved = loadLibrarySession()
  return {
    q: saved?.q ?? '',
    qInput: saved?.qInput ?? '',
    yearFrom: saved?.yearFrom ?? '',
    yearTo: saved?.yearTo ?? '',
    hasPdf: (saved?.hasPdf ?? '') as '' | 'true' | 'false',
    missingMetadataSync: saved?.missingMetadataSync ?? false,
    onlyPendingMetadata: saved?.onlyPendingMetadata ?? false,
    sort: saved?.sort ?? 'updated_at',
    order: (saved?.order ?? 'desc') as 'asc' | 'desc',
    offset: saved?.offset ?? 0,
    selectedId: saved?.selectedId ?? null,
    scrollTop: saved?.scrollTop ?? 0,
  }
}

export default function LibraryPage() {
  const init = useMemo(() => initialLibraryState(), [])
  const [q, setQ] = useState(init.q)
  const [qInput, setQInput] = useState(init.qInput)
  const [yearFrom, setYearFrom] = useState(init.yearFrom)
  const [yearTo, setYearTo] = useState(init.yearTo)
  const [hasPdf, setHasPdf] = useState<'' | 'true' | 'false'>(init.hasPdf)
  const [missingMetadataSync, setMissingMetadataSync] = useState(init.missingMetadataSync)
  const [onlyPendingMetadata, setOnlyPendingMetadata] = useState(init.onlyPendingMetadata)
  const [sort, setSort] = useState(init.sort)
  const [order, setOrder] = useState<'asc' | 'desc'>(init.order)
  const [offset, setOffset] = useState(init.offset)
  const [selectedId, setSelectedId] = useState<number | null>(init.selectedId)
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [exportBusy, setExportBusy] = useState(false)
  const [exportMsg, setExportMsg] = useState<string | null>(null)
  const lastPdf = useMemo(() => loadLastPdf(), [])
  const scrollRestored = useRef(false)
  const qc = useQueryClient()
  const metadataSyncRunner = useJobRunner()
  const pdfAudit = usePdfMatchAudit((id) => setSelectedId(id))

  const SKIP_REASON: Record<string, string> = {
    missing_metadata_synced: '未同步元数据',
    no_bibcode: '无 bibcode',
    junk_metadata: '垃圾元数据',
    pdf_wrong: 'PDF 错配',
    metadata_wrong: '元数据错配',
    mismatch: 'PDF/元数据错配',
    pdf_mismatch: 'PDF/元数据错配',
    not_found: '不存在',
  }

  const exportVerifiedBibtex = async () => {
    const ids = [...checked]
    if (ids.length === 0 || exportBusy) return
    setExportBusy(true)
    setExportMsg(null)
    try {
      const elig = await api.exportEligibility(ids)
      if (elig.eligible_count === 0) {
        const reasons = elig.skipped
          .slice(0, 5)
          .map((s) => `#${s.id} ${SKIP_REASON[s.reason] || s.reason}`)
          .join('；')
        setExportMsg(`无可导出条目（需 metadata_synced + bibcode）${reasons ? `：${reasons}` : ''}`)
        return
      }
      if (elig.skipped_count > 0) {
        setExportMsg(`已跳过 ${elig.skipped_count} 篇，导出 ${elig.eligible_count} 篇`)
      } else {
        setExportMsg(null)
      }
      const url = `/api/export/bibtex?ids=${elig.eligible.join(',')}&verified_only=true`
      const a = document.createElement('a')
      a.href = url
      a.download = 'export.bib'
      a.click()
    } catch (e) {
      setExportMsg(`导出失败：${String((e as Error).message)}`)
    } finally {
      setExportBusy(false)
    }
  }

  const { data: facets } = useQuery({
    queryKey: ['paper-facets'],
    queryFn: () => api.facets(),
    staleTime: 30_000,
  })

  const params = useMemo(
    () => ({
      q: q || undefined,
      year_from: yearFrom || undefined,
      year_to: yearTo || undefined,
      has_pdf: hasPdf || undefined,
      missing_tag: missingMetadataSync ? 'metadata_synced' : undefined,
      tag: onlyPendingMetadata ? 'pending_metadata' : undefined,
      sort,
      order,
      limit: PAGE_SIZE,
      offset,
    }),
    [q, yearFrom, yearTo, hasPdf, missingMetadataSync, onlyPendingMetadata, sort, order, offset],
  )

  const { data, isFetching, error, isError } = useQuery({
    queryKey: ['papers', params],
    queryFn: () => api.papers(params),
    placeholderData: keepPreviousData,
    retry: 2,
  })

  const items = data?.items ?? []
  const scrollRef = useRef<HTMLDivElement>(null)
  const rowVirtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 64,
    overscan: 12,
  })

  useEffect(() => {
    const session: LibrarySession = {
      q,
      qInput,
      yearFrom,
      yearTo,
      hasPdf,
      missingMetadataSync,
      onlyPendingMetadata,
      sort,
      order,
      offset,
      selectedId,
      scrollTop: scrollRef.current?.scrollTop ?? init.scrollTop,
    }
    saveLibrarySession(session)
  }, [q, qInput, yearFrom, yearTo, hasPdf, missingMetadataSync, onlyPendingMetadata, sort, order, offset, selectedId, init.scrollTop])

  useEffect(() => {
    if (metadataSyncRunner.status !== 'done') return
    qc.invalidateQueries({ queryKey: ['papers'] })
    qc.invalidateQueries({ queryKey: ['paper-facets'] })
  }, [metadataSyncRunner.status, qc])

  useEffect(() => {
    const el = scrollRef.current
    if (!el || scrollRestored.current || items.length === 0) return
    el.scrollTop = init.scrollTop
    scrollRestored.current = true
  }, [items.length, init.scrollTop])

  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      saveLibrarySession({
        q,
        qInput,
        yearFrom,
        yearTo,
        hasPdf,
        missingMetadataSync,
        onlyPendingMetadata,
        sort,
        order,
        offset,
        selectedId,
        scrollTop: el.scrollTop,
      })
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [q, qInput, yearFrom, yearTo, hasPdf, missingMetadataSync, onlyPendingMetadata, sort, order, offset, selectedId])

  const submitSearch = () => {
    setOffset(0)
    setQ(qInput.trim())
  }

  const toggleSort = (col: string) => {
    if (sort === col) {
      setOrder(order === 'asc' ? 'desc' : 'asc')
    } else {
      setSort(col)
      setOrder('desc')
    }
    setOffset(0)
  }

  return (
    <div className="flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-2 border-b border-neutral-200 bg-white px-4 py-2.5 dark:border-neutral-800 dark:bg-neutral-900">
          {lastPdf && (
            <Link
              to={`/pdf/${lastPdf.paperId}`}
              className="glass-panel mr-2 flex max-w-xs items-center gap-2 rounded-full px-3 py-1.5 text-xs text-blue-700 dark:text-blue-300"
            >
              <span>📄</span>
              <span className="truncate">继续阅读：{lastPdf.title}</span>
            </Link>
          )}
          <input
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submitSearch()}
            placeholder="标题/摘要全文检索…"
            className="w-64 rounded-md border border-neutral-300 px-3 py-1.5 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
          />
          <button
            onClick={submitSearch}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
          >
            搜索
          </button>
          <input
            value={yearFrom}
            onChange={(e) => { setYearFrom(e.target.value); setOffset(0) }}
            placeholder="年份从"
            className="w-20 rounded-md border border-neutral-300 px-2 py-1.5 text-sm dark:border-neutral-700 dark:bg-neutral-800"
          />
          <input
            value={yearTo}
            onChange={(e) => { setYearTo(e.target.value); setOffset(0) }}
            placeholder="到"
            className="w-20 rounded-md border border-neutral-300 px-2 py-1.5 text-sm dark:border-neutral-700 dark:bg-neutral-800"
          />
          <select
            value={hasPdf}
            onChange={(e) => { setHasPdf(e.target.value as typeof hasPdf); setOffset(0) }}
            className="rounded-md border border-neutral-300 px-2 py-1.5 text-sm dark:border-neutral-700 dark:bg-neutral-800"
          >
            <option value="">PDF: 全部</option>
            <option value="true">有 PDF</option>
            <option value="false">无 PDF</option>
          </select>
          <label className="flex items-center gap-1.5 text-sm text-neutral-600 dark:text-neutral-300">
            <input
              type="checkbox"
              checked={missingMetadataSync}
              onChange={(e) => { setMissingMetadataSync(e.target.checked); setOffset(0) }}
            />
            仅未同步元数据
          </label>
          <label className="flex items-center gap-1.5 text-sm text-neutral-600 dark:text-neutral-300">
            <input
              type="checkbox"
              checked={onlyPendingMetadata}
              onChange={(e) => { setOnlyPendingMetadata(e.target.checked); setOffset(0) }}
            />
            仅待补全{facets ? ` (${facets.pending_metadata ?? 0})` : ''}
          </label>
          <button
            type="button"
            onClick={() => metadataSyncRunner.start('metadata_sync', { delay: 1 })}
            disabled={metadataSyncRunner.status === 'running' || (facets?.pending_metadata_sync ?? 0) === 0}
            className="rounded-md border border-neutral-300 px-2.5 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            {metadataSyncRunner.status === 'running'
              ? '同步中…'
              : `同步元数据${facets ? ` (${facets.pending_metadata_sync})` : ''}`}
          </button>
          {pdfAudit.toolbar}
          <div className="ml-auto flex items-center gap-2">
            {checked.size > 0 && (
              <>
                <span className="text-xs text-neutral-500">已选 {checked.size} 篇</span>
                <button
                  type="button"
                  onClick={() => void exportVerifiedBibtex()}
                  disabled={exportBusy}
                  className="rounded-md border border-neutral-300 px-2.5 py-1 text-xs hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
                >
                  {exportBusy ? '检查中…' : 'BibTeX'}
                </button>
                <a
                  href={`/api/export/csv?ids=${[...checked].join(',')}`}
                  download="export.csv"
                  className="rounded-md border border-neutral-300 px-2.5 py-1 text-xs hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
                >
                  CSV
                </a>
                <button
                  onClick={() => setChecked(new Set())}
                  className="text-xs text-neutral-400 hover:text-neutral-600"
                >
                  清除
                </button>
              </>
            )}
            {exportMsg && (
              <span className="max-w-xs truncate text-xs text-amber-700 dark:text-amber-400" title={exportMsg}>
                {exportMsg}
              </span>
            )}
            <span className="text-sm text-neutral-500">
              {isFetching ? '加载中…' : data ? `共 ${data.total} 篇` : ''}
            </span>
          </div>
        </div>

        {pdfAudit.panel}

        {metadataSyncRunner.status === 'running' && (
          <div className="border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
            <ProgressBar progress={metadataSyncRunner.progress} message={metadataSyncRunner.message || '同步元数据中…'} />
          </div>
        )}

        <div className="flex border-b border-neutral-200 bg-neutral-100 px-4 py-1.5 text-xs font-semibold text-neutral-500 dark:border-neutral-800 dark:bg-neutral-900">
          <span className="w-8" />
          <button onClick={() => toggleSort('title')} className="flex-1 text-left hover:text-neutral-800 dark:hover:text-neutral-200">
            标题 {sort === 'title' && (order === 'asc' ? '↑' : '↓')}
          </button>
          <button onClick={() => toggleSort('published')} className="w-24 text-left hover:text-neutral-800 dark:hover:text-neutral-200">
            日期 {sort === 'published' && (order === 'asc' ? '↑' : '↓')}
          </button>
          <span className="w-40">Bibcode</span>
          <span className="w-12 text-center">PDF</span>
        </div>

        {isError && !data ? (
          <div className="p-6 text-sm text-red-500">加载失败：{String(error)}</div>
        ) : (
          <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto">
            <div style={{ height: rowVirtualizer.getTotalSize(), position: 'relative' }}>
              {rowVirtualizer.getVirtualItems().map((vi) => {
                const p = items[vi.index]
                return (
                  <PaperRow
                    key={p.id}
                    paper={p}
                    selected={p.id === selectedId}
                    checked={checked.has(p.id)}
                    onCheck={(on) => {
                      setChecked((prev) => {
                        const next = new Set(prev)
                        if (on) next.add(p.id)
                        else next.delete(p.id)
                        return next
                      })
                    }}
                    onClick={() => setSelectedId(p.id)}
                    style={{
                      position: 'absolute',
                      top: 0,
                      left: 0,
                      width: '100%',
                      height: vi.size,
                      transform: `translateY(${vi.start}px)`,
                    }}
                  />
                )
              })}
            </div>
          </div>
        )}

        {data && data.total > PAGE_SIZE && (
          <div className="flex items-center justify-center gap-3 border-t border-neutral-200 bg-white py-2 text-sm dark:border-neutral-800 dark:bg-neutral-900">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              className="rounded px-3 py-1 hover:bg-neutral-100 disabled:opacity-40 dark:hover:bg-neutral-800"
            >
              上一页
            </button>
            <span className="text-neutral-500">
              {Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(data.total / PAGE_SIZE)}
            </span>
            <button
              disabled={offset + PAGE_SIZE >= data.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
              className="rounded px-3 py-1 hover:bg-neutral-100 disabled:opacity-40 dark:hover:bg-neutral-800"
            >
              下一页
            </button>
          </div>
        )}
      </div>

      {selectedId !== null && (
        <PaperDetailPanel paperId={selectedId} onClose={() => setSelectedId(null)} onSelectPaper={setSelectedId} />
      )}
    </div>
  )
}

function PaperRow({
  paper,
  selected,
  checked,
  onCheck,
  onClick,
  style,
}: {
  paper: Paper
  selected: boolean
  checked: boolean
  onCheck: (on: boolean) => void
  onClick: () => void
  style: React.CSSProperties
}) {
  return (
    <div
      style={style}
      onClick={onClick}
      className={`flex cursor-pointer items-center border-b border-neutral-100 px-4 dark:border-neutral-800 ${
        selected ? 'bg-blue-50 dark:bg-blue-950' : 'hover:bg-neutral-50 dark:hover:bg-neutral-900'
      }`}
    >
      <span className="w-8" onClick={(e) => e.stopPropagation()}>
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onCheck(e.target.checked)}
        />
      </span>
      <div className="min-w-0 flex-1 pr-3">
        <div className="flex items-center gap-1.5 truncate text-sm font-medium">
          {paper.pending_metadata && (
            <span className="shrink-0 text-[10px] text-amber-600 dark:text-amber-400" title="待补全元数据">
              ?
            </span>
          )}
          {paper.metadata_synced && (
            <span className="shrink-0 text-[10px] text-green-600 dark:text-green-400" title="元数据已同步">
              ✓
            </span>
          )}
          <span className="truncate">{paper.title || '(无标题)'}</span>
        </div>
        <div className="truncate text-xs text-neutral-400">
          {paper.authors.slice(0, 3).join('; ')}
          {paper.authors.length > 3 ? ' 等' : ''}
        </div>
      </div>
      <div className="w-24 text-xs text-neutral-500">{(paper.published || '').slice(0, 10)}</div>
      <div className="w-40 truncate font-mono text-xs text-neutral-400">{paper.bibcode || '—'}</div>
      <div className="w-12 text-center text-xs">{paper.has_pdf ? '📄' : ''}</div>
    </div>
  )
}
