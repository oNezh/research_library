import { useState, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, type Job, type Paper } from '../lib/api'
import {
  getHomeModeConfig,
  investigateSubLabel,
  parseHomeMode,
  type HomeMode,
  type InvestigateSubMode,
} from '../lib/homeModes'
import { ProgressBar, useJobRunner } from '../components/JobRunner'
import PaperDetailPanel from '../components/PaperDetailPanel'
import HomeInputBar from '../components/HomeInputBar'
import SearchResultsPanel from '../components/SearchResultsPanel'
import ChainResultPanel, { type ChainResult } from '../components/ChainResultPanel'
import ImportPanel from '../components/ImportPanel'

interface SourceRow {
  tag: string
  paper_id: number
  chunk_id?: number
  bibcode?: string | null
  title?: string
  section?: string
  page?: number
}

interface ReportResult {
  query?: string
  topic?: string
  markdown: string
  source_index?: SourceRow[]
  chunks?: unknown[]
}

function isSearchMode(mode: HomeMode): mode is 'search_semantic' | 'search_fts' | 'search_remote' {
  return mode === 'search_semantic' || mode === 'search_fts' || mode === 'search_remote'
}

export default function InvestigatePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const modeParam = parseHomeMode(searchParams.get('mode'))
  const [mode, setMode] = useState<HomeMode>(modeParam ?? 'investigate')
  const [investigateSub, setInvestigateSub] = useState<InvestigateSubMode>('semantic_report')
  const [input, setInput] = useState('')
  const [seed, setSeed] = useState<Paper | null>(null)
  const [maxHops, setMaxHops] = useState(2)
  const [searchQuery, setSearchQuery] = useState('')
  const runner = useJobRunner()
  const importRunner = useJobRunner()
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const loadedJobRef = useRef<string | null>(null)

  const jobIdParam = searchParams.get('job')

  useEffect(() => {
    if (modeParam) {
      setMode(modeParam)
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        next.delete('mode')
        return next
      }, { replace: true })
    }
  }, [modeParam, setSearchParams])

  useEffect(() => {
    if (!jobIdParam || loadedJobRef.current === jobIdParam) return
    loadedJobRef.current = jobIdParam
    const id = Number(jobIdParam)
    if (!Number.isFinite(id)) return
    ;(async () => {
      try {
        const job = await api.job(id)
        if (job.kind === 'topic_dossier') {
          setMode('investigate')
          setInvestigateSub('topic_dossier')
        } else if (job.kind === 'semantic_report') {
          setMode('investigate')
          setInvestigateSub('semantic_report')
        } else if (job.kind === 'reference_chain') {
          setMode('reference_chain')
        } else if (job.kind === 'ingest_ref') {
          setMode('import')
        }

        const p = job.params as {
          query?: string
          topic?: string
          question?: string
          paper_id?: number
          max_hops?: number
          text?: string
        } | undefined

        if (job.kind === 'reference_chain') {
          if (p?.question) setInput(p.question)
          if (p?.max_hops) setMaxHops(p.max_hops)
          if (p?.paper_id) setSeed(await api.paper(p.paper_id))
          await runner.loadFromJob(job)
        } else if (job.kind === 'ingest_ref') {
          if (p?.text) setInput(p.text)
          await importRunner.loadFromJob(job)
        } else {
          setInput(p?.query || p?.topic || '')
          await runner.loadFromJob(job)
        }
        setSearchParams({}, { replace: true })
      } catch {
        loadedJobRef.current = null
      }
    })()
  }, [jobIdParam, setSearchParams, runner, importRunner])

  const history = useQuery({
    queryKey: ['jobs-home-history'],
    queryFn: () => api.jobs(),
    select: (d) =>
      d.items
        .filter((j) => j.kind === 'semantic_report' || j.kind === 'topic_dossier' || j.kind === 'reference_chain')
        .slice(0, 20),
  })

  const handleModeChange = (m: HomeMode) => {
    setMode(m)
    setSearchQuery('')
    if (m !== 'reference_chain') setSeed(null)
  }

  const submit = () => {
    const t = input.trim()
    switch (mode) {
      case 'investigate':
        if (!t) return
        if (investigateSub === 'semantic_report') {
          runner.start('semantic_report', { query: t, synthesize: true })
        } else {
          runner.start('topic_dossier', { topic: t, synthesize: true })
        }
        break
      case 'search_semantic':
      case 'search_fts':
      case 'search_remote':
        if (!t) return
        setSearchQuery(t)
        break
      case 'reference_chain':
        if (!seed || !t) return
        runner.start('reference_chain', { paper_id: seed.id, question: t, max_hops: maxHops })
        break
      case 'import':
        if (!t) return
        importRunner.start('ingest_ref', { text: t })
        setInput('')
        break
    }
  }

  const reportResult = runner.result as ReportResult | ChainResult | null
  const isChainResult = (r: unknown): r is ChainResult =>
    r != null && typeof r === 'object' && 'markdown_report' in r && 'trace' in r
  const isReportResult = (r: unknown): r is ReportResult =>
    r != null && typeof r === 'object' && 'markdown' in r

  const jobActive =
    (mode === 'investigate' || mode === 'reference_chain') &&
    (runner.status === 'running' || runner.result !== null)
  const importActive = mode === 'import' && (importRunner.status !== 'idle' || importRunner.result !== null)
  const searchActive = isSearchMode(mode) && !!searchQuery
  const active = jobActive || importActive || searchActive

  const cfg = getHomeModeConfig(mode)
  const activeRunner = mode === 'import' ? importRunner : runner

  const statusLabel = () => {
    if (mode === 'investigate') return investigateSubLabel(investigateSub)
    if (mode === 'reference_chain' && seed) return `起点：${seed.title.slice(0, 40)}…`
    return cfg.label
  }

  return (
    <div className="flex h-full">
      <div className="relative flex min-w-0 flex-1 flex-col">
        {!active && (
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center px-6 pb-28">
            <h2 className="mb-2 text-2xl font-bold">一问</h2>
            <p className="mb-8 max-w-lg text-center text-sm text-neutral-400">
              检索、深入调查、链式追踪与文献导入，统一入口。
            </p>
            <div className="w-full max-w-2xl">
              <HomeInputBar
                mode={mode}
                onModeChange={handleModeChange}
                investigateSub={investigateSub}
                onInvestigateSubChange={setInvestigateSub}
                seed={seed}
                onSeedSelect={setSeed}
                onSeedClear={() => setSeed(null)}
                maxHops={maxHops}
                onMaxHopsChange={setMaxHops}
                input={input}
                onInputChange={setInput}
                onSubmit={submit}
                running={activeRunner.status === 'running'}
              />
              {cfg.hint && <p className="mt-2 text-center text-xs text-neutral-400">{cfg.hint}</p>}
              {mode === 'import' && (
                <div className="mt-4">
                  <ImportPanel runner={importRunner} />
                </div>
              )}
            </div>

            {mode === 'investigate' && history.data && history.data.length > 0 && (
              <section className="mt-10 w-full max-w-2xl">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">历史记录</h3>
                <ul className="space-y-1.5">
                  {history.data.map((j: Job) => (
                    <li key={j.id}>
                      <button
                        onClick={async () => {
                          const p = j.params as {
                            query?: string
                            topic?: string
                            question?: string
                            paper_id?: number
                          } | undefined
                          if (j.kind === 'reference_chain') {
                            setMode('reference_chain')
                            if (p?.question) setInput(p.question)
                            if (p?.paper_id) setSeed(await api.paper(p.paper_id))
                          } else {
                            setMode('investigate')
                            setInvestigateSub(j.kind as InvestigateSubMode)
                            setInput(p?.query || p?.topic || '')
                          }
                          await runner.loadFromJob(j)
                        }}
                        disabled={j.status !== 'done'}
                        className="w-full rounded-md border border-neutral-200 bg-white px-3 py-2 text-left text-sm hover:border-blue-400 disabled:opacity-50 dark:border-neutral-800 dark:bg-neutral-900"
                      >
                        <span className="font-mono text-xs text-neutral-400">#{j.id}</span>{' '}
                        {(() => {
                          const p = j.params as { query?: string; topic?: string; question?: string } | undefined
                          return p?.query || p?.topic || p?.question || j.kind
                        })()}
                        <span className="float-right text-xs text-neutral-400">
                          {j.created_at?.slice(0, 16).replace('T', ' ')}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        )}

        {active && (
          <>
            <div className="border-b border-neutral-200 bg-white px-4 py-2 dark:border-neutral-800 dark:bg-neutral-900">
              <div className="flex items-center gap-2 text-sm">
                <span className="font-medium">{input.trim() || searchQuery || '处理中'}</span>
                <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-500 dark:bg-neutral-800">
                  {statusLabel()}
                </span>
              </div>
            </div>

            <div className="min-h-0 flex-1 overflow-auto p-4 pb-36">
              {activeRunner.status === 'running' && (
                <ProgressBar progress={activeRunner.progress} message={activeRunner.message} />
              )}
              {activeRunner.status === 'error' && (
                <pre className="rounded-lg bg-red-50 p-3 text-xs text-red-600 dark:bg-red-950 dark:text-red-300">
                  {activeRunner.error}
                </pre>
              )}

              {searchActive && isSearchMode(mode) && (
                <SearchResultsPanel mode={mode} query={searchQuery} onSelectPaper={setSelectedId} />
              )}

              {mode === 'import' && <ImportPanel runner={importRunner} />}

              {mode === 'reference_chain' && isChainResult(reportResult) && (
                <ChainResultPanel result={reportResult} />
              )}

              {mode === 'investigate' && isReportResult(reportResult) && (
                <div className="mx-auto max-w-3xl space-y-4">
                  {reportResult.markdown ? (
                    <article className="prose prose-sm rounded-2xl border border-neutral-200 bg-white p-6 shadow-sm dark:border-neutral-800 dark:bg-neutral-900 dark:prose-invert">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{reportResult.markdown}</ReactMarkdown>
                    </article>
                  ) : (
                    <p className="text-sm text-neutral-400">（未生成综述文本，仅返回检索块）</p>
                  )}
                  {reportResult.source_index && reportResult.source_index.length > 0 && (
                    <section>
                      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
                        来源（点击查看论文）
                      </h3>
                      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                        {reportResult.source_index.map((s) => (
                          <button
                            key={s.tag}
                            onClick={() => setSelectedId(s.paper_id)}
                            className="flex items-baseline gap-2 rounded-md border border-neutral-200 bg-white px-2.5 py-1.5 text-left text-xs hover:border-blue-400 dark:border-neutral-800 dark:bg-neutral-900"
                          >
                            <span className="shrink-0 font-mono font-bold text-blue-600 dark:text-blue-400">
                              {s.tag}
                            </span>
                            <span className="min-w-0">
                              <span className="line-clamp-1">{s.title || s.bibcode}</span>
                              <span className="text-neutral-400">
                                {s.bibcode}
                                {s.section ? ` · ${s.section}` : ''}
                              </span>
                            </span>
                          </button>
                        ))}
                      </div>
                    </section>
                  )}
                </div>
              )}
            </div>

            <div className="absolute bottom-0 left-0 right-0 border-t border-neutral-200 bg-white/95 px-4 py-3 backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/95">
              <div className="mx-auto max-w-3xl">
                <HomeInputBar
                  mode={mode}
                  onModeChange={handleModeChange}
                  investigateSub={investigateSub}
                  onInvestigateSubChange={setInvestigateSub}
                  seed={seed}
                  onSeedSelect={setSeed}
                  onSeedClear={() => setSeed(null)}
                  maxHops={maxHops}
                  onMaxHopsChange={setMaxHops}
                  input={input}
                  onInputChange={setInput}
                  onSubmit={submit}
                  running={activeRunner.status === 'running'}
                />
              </div>
            </div>
          </>
        )}
      </div>

      {selectedId !== null && (
        <PaperDetailPanel paperId={selectedId} onClose={() => setSelectedId(null)} onSelectPaper={setSelectedId} />
      )}
    </div>
  )
}
