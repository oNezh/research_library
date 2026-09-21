import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import type { PaperDetail } from '../lib/api'
import { openExternalUrl } from '../lib/openExternal'

function formatAuthors(authors: string[]): string {
  if (!authors.length) return ''
  if (authors.length <= 3) return authors.join(', ')
  return `${authors.slice(0, 3).join(', ')} et al.`
}

function firstAuthorSurname(authors: string[]): string {
  const first = authors[0]?.trim()
  if (!first) return ''
  const comma = first.indexOf(',')
  if (comma > 0) return first.slice(0, comma).trim()
  const parts = first.split(/\s+/).filter(Boolean)
  return parts[parts.length - 1] ?? first
}

function sortReferencesByAuthor(refs: PaperDetail['references']) {
  return [...refs].sort((a, b) => {
    const sa = firstAuthorSurname(a.authors).toLocaleLowerCase()
    const sb = firstAuthorSurname(b.authors).toLocaleLowerCase()
    if (!sa && !sb) return a.ref_bibcode.localeCompare(b.ref_bibcode)
    if (!sa) return 1
    if (!sb) return -1
    const cmp = sa.localeCompare(sb)
    return cmp !== 0 ? cmp : a.ref_bibcode.localeCompare(b.ref_bibcode)
  })
}

function formatReferenceLine(r: PaperDetail['references'][number]): string {
  return [formatAuthors(r.authors), r.year, r.journal, r.title].filter(Boolean).join(', ')
}

const linkCls = 'shrink-0 text-neutral-400 hover:text-blue-500'

export default function PdfReferencesDock({
  references,
  onExpand,
}: {
  references: PaperDetail['references']
  onExpand?: () => void
}) {
  const sorted = useMemo(() => sortReferencesByAuthor(references), [references])
  if (!sorted.length) return null

  return (
    <div className="glass-panel pointer-events-auto absolute bottom-4 left-4 right-4 z-10 mx-auto max-w-3xl">
      <details
        className="group"
        onToggle={(e) => {
          if ((e.target as HTMLDetailsElement).open) onExpand?.()
        }}
      >
        <summary className="cursor-pointer list-none px-4 py-2.5 text-sm font-semibold text-neutral-700 dark:text-neutral-200">
          <span className="mr-2 inline-block transition group-open:rotate-90">▸</span>
          参考文献（{sorted.length}）
        </summary>
        <ul className="max-h-56 space-y-2 overflow-auto border-t border-white/20 px-3 py-2 dark:border-white/10">
          {sorted.map((r) => {
            const hasPdf = Boolean(r.local_paper_id && r.local_has_pdf)
            const arxiv = r.arxiv_id
            return (
              <li key={r.ref_bibcode} className="flex items-start gap-2 text-xs leading-relaxed">
                <span className="min-w-0 flex-1 text-neutral-700 dark:text-neutral-200" title={r.title}>
                  {formatReferenceLine(r)}
                </span>
                <span className="flex shrink-0 gap-1.5">
                  {hasPdf && (
                    <Link to={`/pdf/${r.local_paper_id}`} className={linkCls}>
                      PDF
                    </Link>
                  )}
                  <button
                    type="button"
                    onClick={() =>
                      openExternalUrl(
                        `https://ui.adsabs.harvard.edu/abs/${encodeURIComponent(r.ref_bibcode)}/abstract`,
                      )
                    }
                    className={linkCls}
                  >
                    ADS
                  </button>
                  {arxiv && (
                    <button
                      type="button"
                      onClick={() => openExternalUrl(`https://arxiv.org/abs/${arxiv}`)}
                      className={linkCls}
                    >
                      arXiv
                    </button>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
      </details>
    </div>
  )
}
