import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

interface TraceNode {
  depth: number
  label: string
  parent?: string | null
  rationale?: string
  excerpts?: string[]
  follow_ref_numbers?: number[]
  unresolved?: boolean
  reason?: string
  ref_line?: string
  library_paper_id?: number | null
  note?: string
}

export interface ChainResult {
  question: string
  max_hops: number
  markdown_report: string
  trace: TraceNode[]
  library_ingested_ok: number
}

export default function ChainResultPanel({ result }: { result: ChainResult }) {
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      {result.markdown_report && (
        <article className="prose prose-sm max-w-none rounded-lg border border-neutral-200 bg-white p-6 dark:border-neutral-800 dark:bg-neutral-900 dark:prose-invert">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{result.markdown_report}</ReactMarkdown>
        </article>
      )}
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          追踪轨迹（{result.trace.length} 个节点，入库 {result.library_ingested_ok} 篇）
        </h3>
        <ul className="space-y-1.5">
          {result.trace.map((t, i) => (
            <li
              key={i}
              style={{ marginLeft: Math.max(0, t.depth) * 20 }}
              className={`rounded-md border px-3 py-2 text-xs ${
                t.unresolved
                  ? 'border-neutral-200 bg-neutral-50 text-neutral-400 dark:border-neutral-800 dark:bg-neutral-900'
                  : 'border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900'
              }`}
            >
              <div className="font-medium">
                {t.depth >= 0 && <span className="mr-1 text-neutral-400">H{t.depth}</span>}
                {t.label}
                {t.unresolved && <span className="ml-2 text-amber-500">未获取</span>}
              </div>
              {t.ref_line && <div className="mt-0.5 text-neutral-400">{t.ref_line}</div>}
              {t.reason && <div className="mt-0.5 text-neutral-400">{t.reason}</div>}
              {t.note && <div className="mt-0.5 text-neutral-400">{t.note}</div>}
              {t.rationale && <p className="mt-1 text-neutral-500">{t.rationale}</p>}
              {t.follow_ref_numbers && t.follow_ref_numbers.length > 0 && (
                <div className="mt-1 text-neutral-400">追引文: {t.follow_ref_numbers.join(', ')}</div>
              )}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
