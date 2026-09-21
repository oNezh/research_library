import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ProgressBar, useJobRunner } from './JobRunner'

interface ZoteroStatus {
  configured: boolean
  library_id: string | null
  local: Record<string, number>
  unknown_authors: number
  last_pull_version: number
}

async function fetchZoteroStatus(): Promise<ZoteroStatus> {
  const r = await fetch('/api/zotero/status')
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs text-neutral-400">{label}</dt>
      <dd className="font-mono text-sm">{value}</dd>
    </div>
  )
}

export default function ZoteroSyncSection() {
  const { data, error, refetch } = useQuery({ queryKey: ['zotero-status'], queryFn: fetchZoteroStatus })
  const syncRunner = useJobRunner()
  const backfillRunner = useJobRunner()

  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <h2 className="mb-3 text-sm font-bold">Zotero 同步</h2>
      {error && <p className="text-xs text-red-500">{String(error)}</p>}
      {data && (
        <>
          {!data.configured ? (
            <p className="text-sm text-neutral-400">
              未配置。请前往 <Link to="/settings" className="text-blue-600 hover:underline dark:text-blue-400">设置</Link> 填写 Zotero 凭证。
            </p>
          ) : (
            <>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
                <Stat label="Zotero 库 ID" value={data.library_id ?? '—'} />
                {Object.entries(data.local).map(([k, v]) => (
                  <Stat key={k} label={k} value={String(v)} />
                ))}
                <Stat label="Unknown 作者" value={String(data.unknown_authors)} />
              </dl>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  onClick={() => syncRunner.start('zotero_sync', { full: false, with_pdf: false, do_index: false })}
                  disabled={syncRunner.status === 'running'}
                  className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  {syncRunner.status === 'running' ? '同步中…' : '增量同步'}
                </button>
                <button
                  onClick={() => backfillRunner.start('zotero_backfill', { delay: 2 })}
                  disabled={backfillRunner.status === 'running' || data.unknown_authors === 0}
                  className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 disabled:opacity-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
                >
                  回填 Unknown 作者（ADS）
                </button>
                <button
                  onClick={() => refetch()}
                  className="rounded-md px-3 py-1.5 text-sm text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
                >
                  刷新状态
                </button>
              </div>
              {syncRunner.status === 'running' && (
                <div className="mt-2">
                  <ProgressBar progress={syncRunner.progress} message={syncRunner.message || 'Zotero 同步中…'} />
                </div>
              )}
              {backfillRunner.status === 'running' && (
                <div className="mt-2">
                  <ProgressBar progress={backfillRunner.progress} message={backfillRunner.message || 'ADS 回填中…'} />
                </div>
              )}
              {(syncRunner.status === 'done' || backfillRunner.status === 'done') && (
                <p className="mt-2 text-xs text-green-600">完成。详细结果见任务页。</p>
              )}
              {(syncRunner.error || backfillRunner.error) && (
                <p className="mt-2 text-xs text-red-500">{syncRunner.error || backfillRunner.error}</p>
              )}
            </>
          )}
        </>
      )}
    </section>
  )
}
