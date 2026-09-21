import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, jobEvents, type Job } from '../lib/api'

/** Shared hook: submit a job, follow SSE progress, deliver the final result. */
export function useJobRunner() {
  const qc = useQueryClient()
  const [jobId, setJobId] = useState<number | null>(null)
  const [progress, setProgress] = useState(0)
  const [message, setMessage] = useState('')
  const [status, setStatus] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [result, setResult] = useState<unknown>(null)
  const [error, setError] = useState<string | null>(null)

  const start = async (kind: string, params: Record<string, unknown>) => {
    setStatus('running')
    setProgress(0)
    setMessage('提交任务…')
    setResult(null)
    setError(null)
    try {
      const { job_id } = await api.createJob(kind, params)
      setJobId(job_id)
      qc.invalidateQueries({ queryKey: ['jobs'] })
    } catch (e) {
      setStatus('error')
      setError(String((e as Error).message))
    }
  }

  useEffect(() => {
    if (jobId === null) return
    let cancelled = false
    let pollTimer: number | null = null

    const finishFromJob = async () => {
      try {
        const job: Job = await api.job(jobId)
        if (cancelled) return
        if (job.status === 'done') {
          setResult(job.result)
          setStatus('done')
          setProgress(100)
          qc.invalidateQueries({ queryKey: ['jobs'] })
          return
        }
        if (job.status === 'error') {
          setError(job.error ?? '任务失败')
          setStatus('error')
          qc.invalidateQueries({ queryKey: ['jobs'] })
          return
        }
        // Still queued/running — keep polling (SSE may have dropped early)
        if (typeof job.progress === 'number') setProgress(job.progress)
        if (typeof job.message === 'string') setMessage(job.message)
        pollTimer = window.setTimeout(finishFromJob, 1500)
      } catch (e) {
        if (cancelled) return
        pollTimer = window.setTimeout(finishFromJob, 2000)
      }
    }

    const close = jobEvents(
      jobId,
      (j) => {
        if (typeof j.progress === 'number') setProgress(j.progress)
        if (typeof j.message === 'string') setMessage(j.message)
      },
      () => {
        void finishFromJob()
      },
    )
    return () => {
      cancelled = true
      if (pollTimer != null) window.clearTimeout(pollTimer)
      close()
    }
  }, [jobId, qc])

  const loadFromJob = async (job: Job) => {
    setJobId(job.id)
    if (job.status === 'done') {
      try {
        const full = await api.job(job.id)
        setResult(full.result)
        setStatus('done')
      } catch (e) {
        setError(String((e as Error).message))
        setStatus('error')
      }
    } else if (job.status === 'error') {
      setError(job.error)
      setStatus('error')
    } else {
      setStatus('running')
    }
  }

  return { start, loadFromJob, jobId, progress, message, status, result, error }
}

export function ProgressBar({ progress, message }: { progress: number; message: string }) {
  return (
    <div className="rounded-lg border border-blue-200 bg-blue-50 p-3 dark:border-blue-900 dark:bg-blue-950">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-blue-100 dark:bg-blue-900">
        <div className="h-full animate-pulse rounded-full bg-blue-500 transition-all" style={{ width: `${Math.max(progress, 4)}%` }} />
      </div>
      <p className="mt-1.5 text-xs text-blue-700 dark:text-blue-300">{message || '运行中…'}</p>
    </div>
  )
}
