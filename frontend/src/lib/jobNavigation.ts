import type { Job } from './api'

const REPORT_KINDS = new Set(['semantic_report', 'topic_dossier', 'reference_chain'])

export function jobHasReport(kind: string) {
  return REPORT_KINDS.has(kind)
}

export function jobReportHref(job: Job): string | null {
  if (job.status !== 'done' || !jobHasReport(job.kind)) return null
  if (job.kind === 'reference_chain') return `/?job=${job.id}`
  if (job.kind === 'semantic_report' || job.kind === 'topic_dossier') return `/?job=${job.id}`
  return null
}
