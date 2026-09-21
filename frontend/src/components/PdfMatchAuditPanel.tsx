import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type Job } from '../lib/api'
import { ProgressBar, useJobRunner } from './JobRunner'

export type PdfMatchIssue = {
  paper_id: number
  stored_title?: string | null
  pdf_title?: string | null
  pdf_ads_title?: string | null
  title_similarity?: number | null
  junk_metadata?: boolean
  stored_bibcode?: string | null
  pdf_bibcode?: string | null
  issue_kind?: string
  has_source_text?: boolean
  source_title?: string | null
  source_vs_meta?: string | null
  source_vs_pdf?: string | null
  issue_hint?: string | null
}

const HINT_LABEL: Record<string, string> = {
  pdf_wrong: 'PDF 错',
  metadata_wrong: '元数据错',
  junk_metadata: '垃圾元数据',
  mismatch: '错配',
}

function issueBadge(issue: PdfMatchIssue): string {
  if (issue.issue_hint && HINT_LABEL[issue.issue_hint]) return HINT_LABEL[issue.issue_hint]
  if (issue.junk_metadata) return '垃圾元数据'
  return '错配'
}

type AuditResult = {
  ok: boolean
  scanned: number
  mismatch_count: number
  junk_count: number
  issue_count?: number
  issues?: PdfMatchIssue[]
  mismatches?: PdfMatchIssue[]
}

const STORAGE_KEY = 'rc-pdf-match-audit'

function loadCached(): AuditResult | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    return raw ? (JSON.parse(raw) as AuditResult) : null
  } catch {
    return null
  }
}

function saveCached(result: AuditResult | null) {
  try {
    if (!result) sessionStorage.removeItem(STORAGE_KEY)
    else sessionStorage.setItem(STORAGE_KEY, JSON.stringify(result))
  } catch {
    /* ignore */
  }
}

function normalizeResult(data: AuditResult): { result: AuditResult; issues: PdfMatchIssue[] } {
  const list = data.issues ?? data.mismatches ?? []
  return { result: { ...data, issues: list, issue_count: list.length }, issues: list }
}

export default function PdfMatchAuditPanel({
  onSelectPaper,
}: {
  onSelectPaper: (id: number) => void
}) {
  const { toolbar, panel } = usePdfMatchAudit(onSelectPaper)
  return (
    <>
      {toolbar}
      {panel}
    </>
  )
}

/** Split toolbar controls vs result strip so LibraryPage can place the strip below the toolbar. */
export function usePdfMatchAudit(onSelectPaper: (id: number) => void) {
  const runner = useJobRunner()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [finished, setFinished] = useState(false)
  const initial = useMemo(() => loadCached(), [])
  const [result, setResult] = useState<AuditResult | null>(initial)
  const [issues, setIssues] = useState<PdfMatchIssue[]>(() => initial?.issues ?? initial?.mismatches ?? [])
  const [splittingId, setSplittingId] = useState<number | null>(null)
  const [splitError, setSplitError] = useState<string | null>(null)

  const applyResult = useCallback((data: AuditResult) => {
    const normalized = normalizeResult(data)
    setResult(normalized.result)
    setIssues(normalized.issues)
    saveCached(normalized.result)
    setFinished(true)
    setOpen(true)
  }, [])

  useEffect(() => {
    if (runner.status !== 'done' || !runner.result) return
    applyResult(runner.result as AuditResult)
  }, [runner.status, runner.result, applyResult])

  useEffect(() => {
    if (runner.status !== 'running' || runner.jobId == null) return
    let cancelled = false
    const tick = async () => {
      try {
        const job: Job = await api.job(runner.jobId!)
        if (cancelled) return
        if (job.status === 'done' && job.result) {
          applyResult(job.result as AuditResult)
        } else if (job.status === 'error') {
          setOpen(true)
        }
      } catch {
        /* ignore */
      }
    }
    const id = window.setInterval(tick, 2000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [runner.status, runner.jobId, applyResult])

  const split = useMutation({
    mutationFn: (paperId: number) => api.splitPdf(paperId, false),
    onMutate: (paperId) => {
      setSplittingId(paperId)
      setSplitError(null)
    },
    onSuccess: (_data, paperId) => {
      setIssues((prev) => {
        const next = prev.filter((i) => i.paper_id !== paperId)
        if (result) saveCached({ ...result, issues: next, issue_count: next.length })
        return next
      })
      setSplittingId(null)
      qc.invalidateQueries({ queryKey: ['papers'] })
      qc.invalidateQueries({ queryKey: ['paper'] })
      qc.invalidateQueries({ queryKey: ['paper-facets'] })
    },
    onError: (err) => {
      setSplitError(String((err as Error).message))
      setSplittingId(null)
    },
  })

  const startScan = () => {
    setSplitError(null)
    setFinished(false)
    setOpen(true)
    setResult(null)
    setIssues([])
    runner.start('pdf_metadata_audit', {})
  }

  const scanning = runner.status === 'running' && !finished
  const showPanel = open || scanning || runner.status === 'error'

  const toolbar = (
    <div className="flex items-center gap-1">
      <button
        type="button"
        onClick={startScan}
        disabled={scanning}
        className="rounded-md border border-neutral-300 px-2.5 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
      >
        {scanning ? '扫描中…' : '扫描 PDF 匹配'}
      </button>
      {!showPanel && result && (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="rounded-md px-2 py-1.5 text-xs text-amber-700 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-950/40"
        >
          {issues.length > 0 ? `查看结果 (${issues.length})` : '查看上次扫描'}
        </button>
      )}
    </div>
  )

  const panel = showPanel ? (
    <div className="shrink-0 border-b border-neutral-200 bg-amber-50/80 dark:border-neutral-800 dark:bg-amber-950/30">
      <div className="flex items-center justify-between px-4 py-2">
        <span className="text-sm font-semibold text-amber-900 dark:text-amber-200">
          PDF / 元数据扫描
          {scanning && runner.jobId != null && (
            <span className="ml-2 font-normal text-amber-700 dark:text-amber-400">任务 #{runner.jobId}</span>
          )}
        </span>
        <button
          type="button"
          onClick={() => setOpen(false)}
          disabled={scanning}
          className="text-amber-700/70 hover:text-amber-900 disabled:opacity-40 dark:text-amber-400"
        >
          ✕
        </button>
      </div>

      {scanning && (
        <div className="px-4 pb-3">
          <ProgressBar progress={runner.progress} message={runner.message || '正在扫描带 PDF 的条目…'} />
        </div>
      )}

      {runner.status === 'error' && !finished && (
        <p className="px-4 pb-3 text-xs text-red-600">扫描失败：{runner.error}</p>
      )}

      {!scanning && result && (
        <div className="border-t border-amber-200/60 px-4 py-2 text-xs text-amber-800 dark:border-amber-900 dark:text-amber-300">
          已扫描 {result.scanned} 篇 · 错配 {result.mismatch_count} · 垃圾元数据 {result.junk_count}
          {issues.length > 0 ? ` · 下列 ${issues.length} 条可逐条处理` : ''}
        </div>
      )}

      {splitError && <p className="px-4 pb-2 text-xs text-red-600">{splitError}</p>}

      {!scanning && result && issues.length === 0 && (
        <p className="px-4 pb-3 text-sm text-neutral-600 dark:text-neutral-300">
          未发现明显错配（按当前检测规则）。若你仍看到错位条目，可在详情页手动拆分。
        </p>
      )}

      {issues.length > 0 && (
        <ul className="max-h-56 overflow-auto border-t border-amber-200/60 dark:border-amber-900">
          {issues.map((issue) => (
            <li
              key={issue.paper_id}
              className="flex flex-wrap items-start gap-2 border-b border-amber-100 px-4 py-2.5 last:border-0 dark:border-amber-950"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-start gap-2">
                  <button
                    type="button"
                    onClick={() => onSelectPaper(issue.paper_id)}
                    className="min-w-0 flex-1 text-left text-sm font-medium leading-snug text-blue-700 hover:underline dark:text-blue-400"
                  >
                    {issue.stored_title || `(#${issue.paper_id})`}
                  </button>
                  <span className="shrink-0 rounded bg-amber-200/80 px-1.5 py-0.5 text-[10px] text-amber-900 dark:bg-amber-900 dark:text-amber-200">
                    {issueBadge(issue)}
                  </span>
                </div>
                {issue.source_title && (
                  <p className="mt-0.5 truncate text-xs text-neutral-600 dark:text-neutral-400" title={issue.source_title}>
                    源文本：{issue.source_title}
                    {issue.source_vs_meta ? ` · vs元数据 ${issue.source_vs_meta}` : ''}
                    {issue.source_vs_pdf ? ` · vsPDF ${issue.source_vs_pdf}` : ''}
                  </p>
                )}
                {issue.pdf_title && (
                  <p className="mt-0.5 truncate text-xs text-neutral-600 dark:text-neutral-400" title={issue.pdf_title}>
                    PDF：{issue.pdf_title}
                  </p>
                )}
                {issue.pdf_ads_title && issue.pdf_ads_title !== issue.pdf_title && (
                  <p className="truncate text-xs text-neutral-500" title={issue.pdf_ads_title}>
                    ADS：{issue.pdf_ads_title}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {typeof issue.title_similarity === 'number' && (
                  <span className="text-[10px] text-neutral-500">
                    {Math.round(issue.title_similarity * 100)}%
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => onSelectPaper(issue.paper_id)}
                  className="text-xs text-neutral-600 hover:text-blue-600"
                >
                  查看
                </button>
                <button
                  type="button"
                  onClick={() => split.mutate(issue.paper_id)}
                  disabled={splittingId === issue.paper_id}
                  className="rounded bg-amber-600 px-2 py-1 text-xs font-medium text-white hover:bg-amber-700 disabled:opacity-50"
                >
                  {splittingId === issue.paper_id ? '拆分中…' : '拆分'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  ) : null

  return { toolbar, panel }
}
