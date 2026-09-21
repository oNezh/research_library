import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { ProgressBar, useJobRunner } from './JobRunner'

export default function ArxivKeywordsSection() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['arxiv-keywords'], queryFn: api.arxivKeywords })
  const [text, setText] = useState('')
  const [saved, setSaved] = useState(false)
  const runner = useJobRunner()

  useEffect(() => {
    if (data?.text != null) setText(data.text)
  }, [data?.text])

  const save = useMutation({
    mutationFn: () => api.patchArxivKeywords(text),
    onSuccess: (res) => {
      setText(res.text)
      setSaved(true)
      qc.invalidateQueries({ queryKey: ['arxiv-keywords'] })
      setTimeout(() => setSaved(false), 2000)
    },
  })

  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-bold">arXiv 关键词监控</h2>
        <button
          onClick={() => runner.start('arxiv_scan', { days_back: 7 })}
          disabled={runner.status === 'running'}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {runner.status === 'running' ? '扫描中…' : '扫描近 7 天'}
        </button>
      </div>
      <p className="mb-2 text-xs text-neutral-400">每行一个关键词，匹配论文标题与摘要（不区分大小写）。</p>
      {isLoading ? (
        <p className="text-xs text-neutral-400">加载关键词…</p>
      ) : (
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          className="w-full rounded-md border border-neutral-300 px-3 py-2 font-mono text-xs outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
          placeholder="globular cluster&#10;dwarf galaxy&#10;球状星团"
        />
      )}
      <div className="mt-2 flex items-center gap-2">
        <button
          onClick={() => save.mutate()}
          disabled={save.isPending || isLoading}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          {save.isPending ? '保存中…' : '保存关键词'}
        </button>
        {saved && <span className="text-xs text-green-600">已保存</span>}
        {save.isError && <span className="text-xs text-red-500">{String(save.error)}</span>}
      </div>
      {runner.status === 'running' && (
        <div className="mt-2">
          <ProgressBar progress={runner.progress} message={runner.message || '抓取 arXiv 中…'} />
        </div>
      )}
      {runner.status === 'done' && (
        <p className="mt-2 text-xs text-green-600">
          完成，新增 {(runner.result as { added?: number })?.added ?? 0} 篇。
        </p>
      )}
      {runner.error && <p className="mt-2 text-xs text-red-500">{runner.error}</p>}
    </section>
  )
}
