import { useCallback, useEffect, useRef, useState } from 'react'
import { api, jobEvents, type Job } from '../lib/api'

export interface BackgroundJobState {
  jobId: number
  kind: string
  status: 'running' | 'done' | 'error'
  progress: number
  message: string
  result: unknown
  error: string | null
}

export function useBackgroundJob() {
  const watchers = useRef<Map<number, () => void>>(new Map())
  const [jobs, setJobs] = useState<BackgroundJobState[]>([])

  const updateJob = useCallback((jobId: number, patch: Partial<BackgroundJobState>) => {
    setJobs((prev) => prev.map((j) => (j.jobId === jobId ? { ...j, ...patch } : j)))
  }, [])

  const watch = useCallback(
    (jobId: number) => {
      watchers.current.get(jobId)?.()
      const close = jobEvents(
        jobId,
        (partial) => {
          updateJob(jobId, {
            progress: typeof partial.progress === 'number' ? partial.progress : undefined,
            message: typeof partial.message === 'string' ? partial.message : undefined,
          } as Partial<BackgroundJobState>)
        },
        async () => {
          try {
            const job: Job = await api.job(jobId)
            if (job.status === 'done') {
              updateJob(jobId, { status: 'done', result: job.result, progress: 100 })
            } else {
              updateJob(jobId, { status: 'error', error: job.error ?? '任务失败' })
            }
          } catch (e) {
            updateJob(jobId, { status: 'error', error: String((e as Error).message) })
          }
        },
      )
      watchers.current.set(jobId, close)
    },
    [updateJob],
  )

  const submit = useCallback(
    async (kind: string, params: Record<string, unknown>) => {
      const { job_id } = await api.createJob(kind, params)
      const entry: BackgroundJobState = {
        jobId: job_id,
        kind,
        status: 'running',
        progress: 0,
        message: '已提交…',
        result: null,
        error: null,
      }
      setJobs((prev) => [...prev, entry])
      watch(job_id)
      return job_id
    },
    [watch],
  )

  const remove = useCallback((jobId: number) => {
    watchers.current.get(jobId)?.()
    watchers.current.delete(jobId)
    setJobs((prev) => prev.filter((j) => j.jobId !== jobId))
  }, [])

  useEffect(() => {
    const w = watchers.current
    return () => {
      w.forEach((close) => close())
      w.clear()
    }
  }, [])

  return { jobs, submit, remove, watch }
}
