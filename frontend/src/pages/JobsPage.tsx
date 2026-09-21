import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Job } from '../lib/api'
import { jobHasReport, jobReportHref } from '../lib/jobNavigation'

const STATUS_STYLE: Record<Job['status'], string> = {
  queued: 'bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300',
  running: 'bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300',
  done: 'bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300',
  error: 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300',
}

export default function JobsPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { data, isFetching } = useQuery({
    queryKey: ['jobs'],
    queryFn: () => api.jobs(),
    refetchInterval: (q) =>
      q.state.data?.items.some((j) => j.status === 'running' || j.status === 'queued') ? 1500 : 8000,
  })

  const openJob = (j: Job) => {
    const href = jobReportHref(j)
    if (href) navigate(href)
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-neutral-200 bg-white px-4 py-3 dark:border-neutral-800 dark:bg-neutral-900">
        <h2 className="text-sm font-semibold">后台任务</h2>
        <button
          onClick={() => qc.invalidateQueries({ queryKey: ['jobs'] })}
          className="rounded px-2 py-1 text-xs text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
        >
          {isFetching ? '刷新中…' : '刷新'}
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {!data || data.items.length === 0 ? (
          <p className="text-sm text-neutral-400">暂无任务。深入调查、链式搜索、PDF 摄取等长任务会显示在这里。</p>
        ) : (
          <ul className="space-y-2">
            {data.items.map((j) => {
              const clickable = jobReportHref(j) !== null
              return (
                <li key={j.id}>
                  <button
                    type="button"
                    onClick={() => clickable && openJob(j)}
                    disabled={!clickable}
                    className={`w-full rounded-lg border border-neutral-200 bg-white p-3 text-left dark:border-neutral-800 dark:bg-neutral-900 ${
                      clickable ? 'cursor-pointer hover:border-blue-400 hover:shadow-sm' : 'cursor-default'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-neutral-400">#{j.id}</span>
                      <span className="text-sm font-medium">{j.kind}</span>
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[j.status]}`}>
                        {j.status}
                      </span>
                      {clickable && (
                        <span className="text-xs text-blue-500">查看报告 →</span>
                      )}
                      <span className="ml-auto text-xs text-neutral-400">
                        {j.updated_at?.slice(0, 19).replace('T', ' ')}
                      </span>
                    </div>
                    {j.status === 'running' && (
                      <div className="mt-2">
                        <div className="h-1.5 w-full overflow-hidden rounded-full bg-neutral-100 dark:bg-neutral-800">
                          <div
                            className="h-full rounded-full bg-blue-500 transition-all"
                            style={{ width: `${j.progress}%` }}
                          />
                        </div>
                        {j.message && <p className="mt-1 text-xs text-neutral-500">{j.message}</p>}
                      </div>
                    )}
                    {j.status === 'error' && j.error && (
                      <pre className="mt-2 max-h-32 overflow-auto rounded bg-red-50 p-2 text-xs text-red-600 dark:bg-red-950 dark:text-red-300">
                        {j.error}
                      </pre>
                    )}
                    {j.status === 'done' && !jobHasReport(j.kind) && (
                      <p className="mt-1 text-xs text-neutral-400">任务已完成（无报告页）</p>
                    )}
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
