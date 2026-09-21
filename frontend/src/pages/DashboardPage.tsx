import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, type Health } from '../lib/api'
import PaperDetailPanel from '../components/PaperDetailPanel'
import ZoteroSyncSection from '../components/ZoteroSyncSection'
import ArxivKeywordsSection from '../components/ArxivKeywordsSection'

interface Dashboard {
  counts: {
    papers: number
    with_pdf: number
    indexed_papers: number
    reference_edges: number
    zotero_linked: number
  }
  per_year: { year: string; count: number }[]
  per_source: { source: string; count: number }[]
  added_per_month: { month: string; count: number }[]
  recent: { id: number; title: string; bibcode: string | null; published: string | null; created_at: string }[]
}

async function fetchDashboard(): Promise<Dashboard> {
  const r = await fetch('/api/dashboard')
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export default function DashboardPage() {
  const { data } = useQuery({ queryKey: ['dashboard'], queryFn: fetchDashboard })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: api.health })
  const [selectedId, setSelectedId] = useState<number | null>(null)

  return (
    <div className="flex h-full">
      <div className="min-w-0 flex-1 overflow-auto p-4">
        <div className="mx-auto max-w-4xl space-y-6">
          {data && (
            <>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                <StatCard label="论文" value={data.counts.papers} />
                <StatCard label="有 PDF" value={data.counts.with_pdf} />
                <StatCard label="已索引" value={data.counts.indexed_papers} />
                <StatCard label="引用边" value={data.counts.reference_edges} />
                <StatCard label="Zotero 关联" value={data.counts.zotero_linked} />
              </div>

              <ZoteroSyncSection />

              <ArxivKeywordsSection />

              <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
                <h2 className="mb-3 text-sm font-bold">论文年代分布</h2>
                <BarChart data={data.per_year.map((d) => ({ label: d.year, value: d.count }))} />
              </section>

              <div className="grid gap-6 md:grid-cols-2">
                <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
                  <h2 className="mb-3 text-sm font-bold">入库趋势（月）</h2>
                  <BarChart data={data.added_per_month.map((d) => ({ label: d.month.slice(2), value: d.count }))} />
                </section>
                <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
                  <h2 className="mb-3 text-sm font-bold">来源分布</h2>
                  <ul className="space-y-1 text-sm">
                    {data.per_source.map((s) => (
                      <li key={s.source} className="flex justify-between">
                        <span className="truncate text-neutral-600 dark:text-neutral-300">{s.source}</span>
                        <span className="font-mono text-neutral-400">{s.count}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              </div>

              <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
                <h2 className="mb-2 text-sm font-bold">最近入库</h2>
                <ul className="divide-y divide-neutral-100 dark:divide-neutral-800">
                  {data.recent.map((p) => (
                    <li key={p.id}>
                      <button
                        onClick={() => setSelectedId(p.id)}
                        className="w-full py-1.5 text-left text-sm hover:text-blue-600"
                      >
                        <span className="line-clamp-1">{p.title}</span>
                        <span className="font-mono text-xs text-neutral-400">
                          {p.bibcode || ''} · {p.created_at?.slice(0, 10)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            </>
          )}

          {health && <SettingsCard health={health} />}
        </div>
      </div>
      {selectedId !== null && (
        <PaperDetailPanel paperId={selectedId} onClose={() => setSelectedId(null)} onSelectPaper={setSelectedId} />
      )}
    </div>
  )
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-neutral-200 bg-white p-3 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="text-xl font-bold">{value.toLocaleString()}</div>
      <div className="text-xs text-neutral-400">{label}</div>
    </div>
  )
}

function BarChart({ data }: { data: { label: string; value: number }[] }) {
  const max = Math.max(1, ...data.map((d) => d.value))
  return (
    <div className="flex h-32 items-end gap-px overflow-hidden">
      {data.map((d) => (
        <div key={d.label} className="group relative flex-1" style={{ minWidth: 3 }}>
          <div
            className="w-full rounded-t bg-blue-500/80 transition-colors group-hover:bg-blue-600"
            style={{ height: `${Math.max(2, (d.value / max) * 120)}px` }}
            title={`${d.label}: ${d.value}`}
          />
        </div>
      ))}
    </div>
  )
}

function SettingsCard({ health }: { health: Health & { data_dir?: string; db_path?: string } }) {
  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <h2 className="mb-2 text-sm font-bold">环境配置</h2>
      <dl className="space-y-1 text-sm">
        <div className="flex justify-between gap-4">
          <dt className="text-neutral-400">数据目录</dt>
          <dd className="truncate font-mono text-xs">{health.data_dir}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-neutral-400">语义后端</dt>
          <dd className="font-mono text-xs">{health.semantic.backend}</dd>
        </div>
        <div className="flex justify-between gap-4">
          <dt className="text-neutral-400">API 凭证</dt>
          <dd className="flex gap-2 text-xs">
            <TokenDot ok={health.tokens.ads} label="ADS" />
            <TokenDot ok={health.tokens.llm} label="LLM" />
            <TokenDot ok={health.tokens.embedding ?? false} label="Emb" />
            <TokenDot ok={health.tokens.zotero} label="Zotero" />
          </dd>
        </div>
      </dl>
      <Link to="/settings" className="mt-2 inline-block text-xs text-blue-600 hover:underline dark:text-blue-400">
        前往设置 →
      </Link>
    </section>
  )
}

function TokenDot({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={`h-2 w-2 rounded-full ${ok ? 'bg-green-500' : 'bg-red-400'}`} />
      {label}
    </span>
  )
}
