import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, type PaperDetail } from '../lib/api'
import { openExternalUrl } from '../lib/openExternal'

export default function PaperDetailPanel({
  paperId,
  onClose,
  onSelectPaper,
}: {
  paperId: number
  onClose: () => void
  onSelectPaper: (id: number) => void
}) {
  const { data: paper, isLoading, error } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => api.paper(paperId),
    staleTime: 60_000,
    retry: (failureCount, err) =>
      failureCount < 5 &&
      (String(err).includes('Internal Server Error') ||
        String(err).includes('Failed to fetch') ||
        String(err).includes('503')),
    retryDelay: (attempt) => Math.min(400 * 2 ** attempt, 5000),
  })
  const { data: related } = useQuery({
    queryKey: ['related', paperId],
    queryFn: () => api.related(paperId),
    retry: false,
  })
  const qc = useQueryClient()
  const hasPdf = Boolean(paper?.has_pdf || paper?.pdf_relpath)
  const { data: pdfMatch } = useQuery({
    queryKey: ['pdf-match', paperId],
    queryFn: () => api.pdfMatch(paperId),
    enabled: hasPdf,
    staleTime: 120_000,
    retry: false,
  })
  const refreshMeta = useMutation({
    mutationFn: () => api.refreshPaperMetadata(paperId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['paper', paperId] })
      qc.invalidateQueries({ queryKey: ['papers'] })
      qc.invalidateQueries({ queryKey: ['paper-facets'] })
    },
  })
  const confirmMeta = useMutation({
    mutationFn: () => api.confirmPaperMetadata(paperId, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['paper', paperId] })
      qc.invalidateQueries({ queryKey: ['papers'] })
      qc.invalidateQueries({ queryKey: ['paper-facets'] })
      qc.invalidateQueries({ queryKey: ['pdf-match', paperId] })
    },
  })
  const splitPdf = useMutation({
    mutationFn: (force: boolean) => api.splitPdf(paperId, force),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['paper', paperId] })
      qc.invalidateQueries({ queryKey: ['paper', data.pdf_paper_id] })
      qc.invalidateQueries({ queryKey: ['papers'] })
      qc.invalidateQueries({ queryKey: ['pdf-match', paperId] })
      onSelectPaper(data.pdf_paper_id)
    },
  })

  const pendingMeta =
    Boolean(paper?.pending_metadata) ||
    Boolean(paper?.tags?.some((t) => t.tag_key === 'pending_metadata'))

  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
      <div className="flex items-center justify-between border-b border-neutral-200 px-4 py-2 dark:border-neutral-800">
        <span className="text-sm font-semibold text-neutral-500">论文详情</span>
        <button onClick={onClose} className="rounded px-2 py-0.5 text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800">
          ✕
        </button>
      </div>

      {error ? (
        <div className="p-4 text-sm text-red-500">加载失败：{String(error)}</div>
      ) : isLoading || !paper ? (
        <div className="p-4 text-sm text-neutral-400">加载中…</div>
      ) : (
        <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4">
          {pendingMeta && (
            <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
              <p className="font-medium">待补全元数据</p>
              <p className="mt-1 opacity-90">
                入库时未确认匹配，尚未做 embedding。请填写 bibcode / DOI / arXiv 或标题后点「确认并索引」。
              </p>
              <button
                type="button"
                onClick={() => confirmMeta.mutate()}
                disabled={confirmMeta.isPending}
                className="mt-2 rounded bg-amber-700 px-2 py-1 text-white hover:bg-amber-800 disabled:opacity-50"
              >
                {confirmMeta.isPending ? '确认中…' : '确认并索引'}
              </button>
              {confirmMeta.isError && (
                <p className="mt-1 text-red-600">{String(confirmMeta.error)}</p>
              )}
            </div>
          )}
          <MetadataSection paper={paper} />

          <div className="flex flex-wrap gap-2">
            {paper.has_pdf || paper.pdf_relpath ? (
              <Link
                to={`/pdf/${paper.id}`}
                className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
              >
                打开 PDF
              </Link>
            ) : (
              <span className="rounded-md bg-neutral-100 px-3 py-1.5 text-sm text-neutral-400 dark:bg-neutral-800">
                无 PDF
              </span>
            )}
            <button
              type="button"
              onClick={() => refreshMeta.mutate()}
              disabled={refreshMeta.isPending}
              className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm text-neutral-700 hover:bg-neutral-50 disabled:opacity-50 dark:border-neutral-600 dark:text-neutral-200 dark:hover:bg-neutral-800"
            >
              {refreshMeta.isPending ? '刷新中…' : '刷新元数据'}
            </button>
            <span className="self-center text-xs text-neutral-400">{paper.chunks} 个索引块</span>
          </div>

          {refreshMeta.isError && (
            <p className="text-xs text-red-500">刷新失败：{String(refreshMeta.error)}</p>
          )}
          {refreshMeta.isSuccess && (
            <p className="text-xs text-neutral-500">
              {refreshMeta.data.updated
                ? `已从 ADS 更新并标记（${refreshMeta.data.match_method}）`
                : `元数据已是最新，已标记同步（${refreshMeta.data.match_method}）`}
            </p>
          )}

          {pdfMatch?.has_pdf && (pdfMatch.mismatch || pdfMatch.junk_metadata) && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs dark:border-amber-900 dark:bg-amber-950/40">
              <p className="font-semibold text-amber-800 dark:text-amber-300">
                {pdfMatch.junk_metadata ? '库内元数据疑似非论文条目' : 'PDF 与元数据可能不匹配'}
              </p>
              {pdfMatch.pdf_title && (
                <p className="mt-1 text-amber-700 dark:text-amber-400">
                  PDF 识别标题：{pdfMatch.pdf_title}
                </p>
              )}
              {pdfMatch.pdf_ads_title && pdfMatch.pdf_ads_title !== pdfMatch.pdf_title && (
                <p className="mt-0.5 text-amber-700 dark:text-amber-400">
                  PDF→ADS 匹配：{pdfMatch.pdf_ads_title}
                </p>
              )}
              <p className="mt-1 text-amber-600 dark:text-amber-500">
                与库内标题相似度 {Math.round((pdfMatch.title_similarity || 0) * 100)}%
              </p>
              <button
                type="button"
                onClick={() => splitPdf.mutate(false)}
                disabled={splitPdf.isPending}
                className="mt-2 rounded-md bg-amber-600 px-2.5 py-1 text-white hover:bg-amber-700 disabled:opacity-50"
              >
                {splitPdf.isPending ? '拆分中…' : '拆分为两篇'}
              </button>
              {splitPdf.isError && (
                <p className="mt-1 text-red-600">{String(splitPdf.error)}</p>
              )}
            </div>
          )}

          {paper.chunks > 0 && (
            <Link
              to={`/pdf/${paper.id}`}
              className="block rounded-lg border border-blue-200 bg-blue-50/50 px-3 py-2 text-sm text-blue-600 hover:bg-blue-100 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-400"
            >
              在 PDF 阅读器中对话 →
            </Link>
          )}

          <NotesSection paperId={paper.id} initial={paper.notes ?? ''} />

          <TagsSection paperId={paper.id} tags={paper.tags} />

          {paper.tags.length > 0 && null}

          {related && related.results.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-400">相关论文</h3>
              <ul className="space-y-1.5">
                {related.results.map((r) => (
                  <li key={`${r.paper_id}`}>
                    <button
                      onClick={() => onSelectPaper(r.paper_id)}
                      className="text-left text-sm text-blue-600 hover:underline dark:text-blue-400"
                    >
                      {r.title}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {paper.references.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-400">
                参考文献（{paper.references.length}）
              </h3>
              <ul className="max-h-56 space-y-1 overflow-auto">
                {paper.references.map((r) => (
                  <li key={r.ref_bibcode} className="truncate text-xs">
                    {r.local_paper_id ? (
                      <button
                        onClick={() => onSelectPaper(r.local_paper_id!)}
                        className="text-blue-600 hover:underline dark:text-blue-400"
                        title={r.title}
                      >
                        {r.title}
                      </button>
                    ) : (
                      <span className="text-neutral-400">{r.title}</span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {paper.cited_by.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-400">
                被引（库内 {paper.cited_by.length}）
              </h3>
              <ul className="space-y-1">
                {paper.cited_by.map((c) => (
                  <li key={c.paper_id}>
                    <button
                      onClick={() => onSelectPaper(c.paper_id)}
                      className="text-left text-xs text-blue-600 hover:underline dark:text-blue-400"
                    >
                      {c.title}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </aside>
  )
}

type MetadataForm = {
  title: string
  authorsText: string
  abstract: string
  published: string
  doi: string
  arxiv_id: string
  bibcode: string
}

function paperToForm(paper: PaperDetail): MetadataForm {
  return {
    title: paper.title || '',
    authorsText: paper.authors.join('; '),
    abstract: paper.abstract || '',
    published: paper.published || '',
    doi: paper.doi || '',
    arxiv_id: paper.arxiv_id || '',
    bibcode: paper.bibcode || '',
  }
}

function parseAuthors(text: string): string[] {
  return text
    .split(/[;\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function MetadataSection({ paper }: { paper: PaperDetail }) {
  const [editing, setEditing] = useState(false)
  const [form, setForm] = useState<MetadataForm>(() => paperToForm(paper))
  const qc = useQueryClient()

  useEffect(() => {
    if (!editing) setForm(paperToForm(paper))
  }, [paper, editing])

  const save = useMutation({
    mutationFn: () =>
      api.patchPaper(paper.id, {
        title: form.title.trim(),
        authors: parseAuthors(form.authorsText),
        abstract: form.abstract,
        published: form.published.trim() || null,
        doi: form.doi.trim() || null,
        arxiv_id: form.arxiv_id.trim() || null,
        bibcode: form.bibcode.trim() || null,
      }),
    onSuccess: () => {
      setEditing(false)
      qc.invalidateQueries({ queryKey: ['paper', paper.id] })
      qc.invalidateQueries({ queryKey: ['papers'] })
      qc.invalidateQueries({ queryKey: ['pdf-match', paper.id] })
    },
  })

  const fieldClass =
    'w-full rounded-md border border-neutral-300 px-2 py-1.5 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800'

  if (editing) {
    return (
      <section className="space-y-2 rounded-lg border border-neutral-200 p-3 dark:border-neutral-700">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-400">编辑元数据</h3>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => save.mutate()}
              disabled={save.isPending || !form.title.trim()}
              className="text-xs text-blue-600 hover:underline disabled:opacity-50"
            >
              {save.isPending ? '保存中…' : '保存'}
            </button>
            <button
              type="button"
              onClick={() => {
                setForm(paperToForm(paper))
                setEditing(false)
              }}
              className="text-xs text-neutral-400"
            >
              取消
            </button>
          </div>
        </div>
        <label className="block text-xs text-neutral-500">
          标题
          <input
            value={form.title}
            onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
            className={`${fieldClass} mt-0.5`}
          />
        </label>
        <label className="block text-xs text-neutral-500">
          作者（分号或换行分隔）
          <textarea
            value={form.authorsText}
            onChange={(e) => setForm((f) => ({ ...f, authorsText: e.target.value }))}
            rows={2}
            className={`${fieldClass} mt-0.5`}
          />
        </label>
        <label className="block text-xs text-neutral-500">
          发表日期
          <input
            value={form.published}
            onChange={(e) => setForm((f) => ({ ...f, published: e.target.value }))}
            placeholder="YYYY-MM-DD"
            className={`${fieldClass} mt-0.5`}
          />
        </label>
        <div className="grid grid-cols-1 gap-2">
          <label className="block text-xs text-neutral-500">
            Bibcode
            <input
              value={form.bibcode}
              onChange={(e) => setForm((f) => ({ ...f, bibcode: e.target.value }))}
              className={`${fieldClass} mt-0.5 font-mono text-xs`}
            />
          </label>
          <label className="block text-xs text-neutral-500">
            DOI
            <input
              value={form.doi}
              onChange={(e) => setForm((f) => ({ ...f, doi: e.target.value }))}
              className={`${fieldClass} mt-0.5 font-mono text-xs`}
            />
          </label>
          <label className="block text-xs text-neutral-500">
            arXiv ID
            <input
              value={form.arxiv_id}
              onChange={(e) => setForm((f) => ({ ...f, arxiv_id: e.target.value }))}
              className={`${fieldClass} mt-0.5 font-mono text-xs`}
            />
          </label>
        </div>
        <label className="block text-xs text-neutral-500">
          摘要
          <textarea
            value={form.abstract}
            onChange={(e) => setForm((f) => ({ ...f, abstract: e.target.value }))}
            rows={5}
            className={`${fieldClass} mt-0.5`}
          />
        </label>
        {save.isError && <p className="text-xs text-red-500">保存失败：{String(save.error)}</p>}
      </section>
    )
  }

  return (
    <section>
      <div className="mb-1 flex items-start justify-between gap-2">
        <h2 className="text-base font-bold leading-snug">{paper.title || '(无标题)'}</h2>
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="shrink-0 text-xs text-neutral-400 hover:text-blue-500"
        >
          编辑
        </button>
      </div>

      {paper.authors.length > 0 && (
        <div className="text-xs leading-relaxed text-neutral-500">{paper.authors.join('; ')}</div>
      )}

      <div className="mt-1.5 flex flex-wrap gap-1.5 text-xs">
        {paper.published && <Badge>{paper.published.slice(0, 10)}</Badge>}
        {paper.bibcode && (
          <button
            type="button"
            onClick={() =>
              openExternalUrl(
                `https://ui.adsabs.harvard.edu/abs/${encodeURIComponent(paper.bibcode!)}/abstract`,
              )
            }
          >
            <Badge className="hover:bg-blue-100 dark:hover:bg-blue-900">ADS: {paper.bibcode}</Badge>
          </button>
        )}
        {paper.arxiv_id && (
          <button type="button" onClick={() => openExternalUrl(`https://arxiv.org/abs/${paper.arxiv_id}`)}>
            <Badge className="hover:bg-blue-100 dark:hover:bg-blue-900">arXiv: {paper.arxiv_id}</Badge>
          </button>
        )}
        {paper.doi && <Badge>DOI: {paper.doi}</Badge>}
        {paper.zotero && <Badge>Zotero ✓</Badge>}
      </div>

      {paper.abstract && (
        <p className="mt-3 text-sm leading-relaxed text-neutral-700 dark:text-neutral-300">{paper.abstract}</p>
      )}
    </section>
  )
}

function NotesSection({ paperId, initial }: { paperId: number; initial: string }) {
  const [notes, setNotes] = useState(initial)
  const [editing, setEditing] = useState(false)
  const qc = useQueryClient()
  const save = useMutation({
    mutationFn: () => api.patchPaper(paperId, { notes }),
    onSuccess: () => {
      setEditing(false)
      qc.invalidateQueries({ queryKey: ['paper', paperId] })
    },
  })

  if (!editing && !notes.trim()) {
    return (
      <button onClick={() => setEditing(true)} className="text-xs text-neutral-400 hover:text-blue-500">
        + 添加笔记
      </button>
    )
  }
  return (
    <section>
      <div className="mb-1 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-neutral-400">笔记</h3>
        {editing ? (
          <div className="flex gap-2">
            <button onClick={() => save.mutate()} disabled={save.isPending} className="text-xs text-blue-600 hover:underline">
              {save.isPending ? '保存中…' : '保存'}
            </button>
            <button onClick={() => { setNotes(initial); setEditing(false) }} className="text-xs text-neutral-400">
              取消
            </button>
          </div>
        ) : (
          <button onClick={() => setEditing(true)} className="text-xs text-neutral-400 hover:text-blue-500">
            编辑
          </button>
        )}
      </div>
      {editing ? (
        <textarea
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={5}
          className="w-full rounded-md border border-neutral-300 px-2 py-1.5 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
          placeholder="Markdown 笔记…"
        />
      ) : (
        <article className="prose prose-sm max-w-none text-sm dark:prose-invert">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{notes}</ReactMarkdown>
        </article>
      )}
    </section>
  )
}

function TagsSection({ paperId, tags }: { paperId: number; tags: { tag_key: string }[] }) {
  const [input, setInput] = useState('')
  const qc = useQueryClient()
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['paper', paperId] })
    qc.invalidateQueries({ queryKey: ['papers'] })
  }
  const add = useMutation({
    mutationFn: (key: string) => api.addTag(paperId, key),
    onSuccess: () => { setInput(''); invalidate() },
  })
  const remove = useMutation({
    mutationFn: (key: string) => api.removeTag(paperId, key),
    onSuccess: invalidate,
  })

  return (
    <section>
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-neutral-400">标签</h3>
      <div className="flex flex-wrap items-center gap-1">
        {tags.map((t) => (
          <span
            key={t.tag_key}
            className="inline-flex items-center gap-1 rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
          >
            {t.tag_key}
            <button onClick={() => remove.mutate(t.tag_key)} className="text-neutral-400 hover:text-red-500">
              ✕
            </button>
          </span>
        ))}
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && input.trim()) add.mutate(input.trim())
          }}
          placeholder="+ 标签"
          className="w-20 rounded border border-transparent px-1 py-0.5 text-xs outline-none focus:border-neutral-300 dark:bg-transparent dark:focus:border-neutral-700"
        />
      </div>
    </section>
  )
}

function Badge({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={`inline-block rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300 ${className}`}
    >
      {children}
    </span>
  )
}
