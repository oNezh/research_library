const BASE = ''

export interface Paper {
  id: number
  arxiv_id: string | null
  bibcode: string | null
  doi: string | null
  title: string
  abstract: string
  authors: string[]
  published: string | null
  source: string
  pdf_relpath: string | null
  has_pdf?: boolean
  metadata_synced?: boolean
  pending_metadata?: boolean
  created_at: string
  updated_at: string
}

export interface PaperDetail extends Paper {
  references: {
    ref_bibcode: string
    authors: string[]
    year: string | null
    journal: string | null
    title: string
    local_paper_id: number | null
    local_has_pdf?: number
    arxiv_id?: string | null
  }[]
  cited_by: { paper_id: number; title: string }[]
  chunks: number
  tags: { tag_key: string; tag_value: string }[]
  zotero: { zotero_key: string; zotero_version: number; last_push_at: string | null } | null
  notes?: string
}

export interface PapersPage {
  total: number
  limit: number
  offset: number
  items: Paper[]
}

export interface SearchHit {
  chunk_id?: number
  paper_id: number
  bibcode: string | null
  arxiv_id?: string | null
  title: string
  snippet?: string
  distance?: number
  abstract?: string
  [k: string]: unknown
}

export interface Job {
  id: number
  kind: string
  status: 'queued' | 'running' | 'done' | 'error'
  progress: number
  message: string
  error: string | null
  params?: Record<string, unknown>
  result?: unknown
  created_at: string
  updated_at: string
}

export interface Health {
  ok: boolean
  papers: number
  papers_with_pdf: number
  chunks: number
  data_dir: string
  semantic: { backend: string; populated: boolean }
  tokens: { ads: boolean; zotero: boolean; llm: boolean; embedding?: boolean }
  llm_provider?: string
  embedding_provider?: string
}

export interface AppSettings {
  ads: { api_token: string }
  llm: { provider: string; api_key: string; base_url: string; model: string }
  embedding: {
    provider: string
    api_key: string
    base_url: string
    model: string
    local_model: string
    device: string
    hf_home: string
    hf_offline: string
  }
  zotero: { library_id: string; api_key: string; library_type: string }
}

export interface SettingsResponse {
  settings: AppSettings
  configured: { ads: boolean; llm: boolean; embedding: boolean; zotero: boolean }
}

export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
}

export interface AskResponse {
  paper_id: number
  question: string
  answer: string
  retrieval_source: string
}

const RETRYABLE_STATUS = new Set([408, 429, 500, 502, 503, 504])

function retryDelay(attempt: number) {
  return Math.min(400 * 2 ** attempt, 5000)
}

async function sleep(ms: number) {
  await new Promise((resolve) => setTimeout(resolve, ms))
}

export async function fetchWithRetry(path: string, init?: RequestInit, retries = 5): Promise<Response> {
  let lastErr: unknown
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const r = await fetch(`${BASE}${path}`, init)
      if (r.ok || !RETRYABLE_STATUS.has(r.status) || attempt === retries) return r
      lastErr = new Error(r.statusText || `HTTP ${r.status}`)
    } catch (e) {
      lastErr = e
      if (attempt === retries) throw e
    }
    await sleep(retryDelay(attempt))
  }
  throw lastErr instanceof Error ? lastErr : new Error('request failed')
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetchWithRetry(path, init)
  if (!r.ok) {
    let detail = r.statusText
    try {
      const body = await r.json()
      detail = body.detail ?? JSON.stringify(body)
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return r.json() as Promise<T>
}

export const api = {
  health: () => req<Health>('/api/health'),

  papers: (params: Record<string, string | number | boolean | undefined>) => {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== '') qs.set(k, String(v))
    }
    return req<PapersPage>(`/api/papers?${qs}`)
  },

  paper: (id: number, enrichAds = false) =>
    req<PaperDetail>(`/api/papers/${id}${enrichAds ? '?enrich_ads=true' : ''}`),

  paperPdfBlob: async (id: number) => {
    const r = await fetchWithRetry(`/api/papers/${id}/pdf`)
    if (!r.ok) {
      let detail = r.statusText
      try {
        const body = await r.json()
        detail = body.detail ?? JSON.stringify(body)
      } catch {
        /* ignore */
      }
      throw new Error(detail)
    }
    return r.blob()
  },

  facets: () =>
    req<{
      years: { year: string; count: number }[]
      sources: { source: string; count: number }[]
      tags: { tag: string; count: number }[]
      pending_metadata_sync: number
      metadata_synced_tag: string
      pending_metadata: number
      pending_metadata_tag: string
    }>('/api/papers/facets'),

  deletePaper: (id: number) => req(`/api/papers/${id}`, { method: 'DELETE' }),

  patchPaper: (id: number, patch: Record<string, unknown>) =>
    req(`/api/papers/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),

  refreshPaperMetadata: (id: number) =>
    req<{
      ok: boolean
      match_method: string
      updated: boolean
      changed: Record<string, boolean>
      paper: PaperDetail
    }>(`/api/papers/${id}/refresh-metadata`, { method: 'POST' }),

  confirmPaperMetadata: (id: number, refreshFromAds = true) =>
    req<{
      ok: boolean
      paper_id: number
      was_pending: boolean
      source_fetch?: unknown
      semantic_index?: unknown
      paper?: PaperDetail
    }>(
      `/api/papers/${id}/confirm-metadata?refresh_from_ads=${refreshFromAds}`,
      { method: 'POST' },
    ),

  pdfMatch: (id: number) =>
    req<{
      ok: boolean
      has_pdf: boolean
      matched: boolean
      mismatch: boolean
      junk_metadata: boolean
      title_similarity: number
      stored_title: string
      pdf_title: string | null
      pdf_ads_title: string | null
      pdf_bibcode: string | null
      stored_bibcode: string | null
    }>(`/api/papers/${id}/pdf-match`),

  splitPdf: (id: number, force = false) =>
    req<{
      ok: boolean
      metadata_paper_id: number
      pdf_paper_id: number
      mode: string
      metadata_paper: PaperDetail
      pdf_paper: PaperDetail
    }>(`/api/papers/${id}/split-pdf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force }),
    }),

  exportEligibility: (ids: number[], checkPdfMatch = true) =>
    req<{
      ok: boolean
      eligible: number[]
      skipped: { id: number; reason: string; title?: string | null }[]
      eligible_count: number
      skipped_count: number
    }>(
      `/api/export/eligibility?ids=${ids.join(',')}&check_pdf_match=${checkPdfMatch}`,
    ),

  search: (q: string, mode: 'fts' | 'semantic', limit = 20) =>
    req<{ mode: string; query: string; results: SearchHit[] }>(
      `/api/search?q=${encodeURIComponent(q)}&mode=${mode}&limit=${limit}`,
    ),

  related: (paperId: number, limit = 8) =>
    req<{ paper_id: number; results: SearchHit[] }>(
      `/api/papers/${paperId}/related?limit=${limit}`,
    ),

  searchRemote: (q: string, mode: 'title' | 'query' | 'ref' = 'query') =>
    req<{ results: RemoteCandidate[] }>(
      `/api/search/remote?q=${encodeURIComponent(q)}&mode=${mode}`,
    ),

  jobs: (status?: string) =>
    req<{ total: number; items: Job[] }>(`/api/jobs${status ? `?status=${status}` : ''}`),

  job: (id: number) => req<Job>(`/api/jobs/${id}`),

  createJob: (kind: string, params: Record<string, unknown>) =>
    req<{ ok: boolean; job_id: number }>('/api/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, params }),
    }),

  addTag: (paperId: number, tagKey: string) =>
    req(`/api/papers/${paperId}/tags`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag_key: tagKey }),
    }),

  removeTag: (paperId: number, tagKey: string) =>
    req(`/api/papers/${paperId}/tags/${encodeURIComponent(tagKey)}`, { method: 'DELETE' }),

  settings: () => req<SettingsResponse>('/api/settings'),

  patchSettings: (patch: Partial<AppSettings>) =>
    req<SettingsResponse>('/api/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),

  testSettings: () =>
    req<{ results: Record<string, string> }>('/api/settings/test', { method: 'POST' }),

  arxivKeywords: () => req<{ phrases: string[]; text: string }>('/api/arxiv/keywords'),

  patchArxivKeywords: (text: string) =>
    req<{ phrases: string[]; text: string }>('/api/arxiv/keywords', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }),

  askPaper: (paperId: number, question: string, history: ChatTurn[] = [], maxChunks = 16) =>
    req<AskResponse>(`/api/papers/${paperId}/ask`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, history, max_chunks: maxChunks }),
    }),
}

export interface RemoteCandidate {
  title: string
  authors: string[]
  year: string | null
  journal: string | null
  bibcode: string | null
  doi: string | null
  arxiv_url: string | null
  ads_url: string | null
  score: number
  source: string
}

export function jobEvents(jobId: number, onProgress: (j: Partial<Job>) => void, onEnd: () => void) {
  const es = new EventSource(`/api/jobs/${jobId}/events`)
  es.addEventListener('progress', (e) => {
    onProgress(JSON.parse((e as MessageEvent).data))
  })
  es.addEventListener('end', () => {
    es.close()
    onEnd()
  })
  es.onerror = () => {
    es.close()
    onEnd()
  }
  return () => es.close()
}
