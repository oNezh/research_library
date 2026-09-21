import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { saveLastPdf } from '../lib/session'
import PaperChatPanel from '../components/PaperChatPanel'
import PdfReferencesDock from '../components/PdfReferencesDock'
import PdfViewer from '../components/PdfViewer'

const paperRetry = (failureCount: number, error: Error) => {
  if (failureCount >= 5) return false
  const msg = String(error)
  return msg.includes('Internal Server Error') || msg.includes('Failed to fetch') || msg.includes('503')
}

export default function PdfPage() {
  const { paperId } = useParams()
  const navigate = useNavigate()
  const id = Number(paperId)
  const [refsExpanded, setRefsExpanded] = useState(false)

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    retry: 8,
    retryDelay: (attempt) => Math.min(400 * 2 ** attempt, 5000),
  })

  const { data: paper } = useQuery({
    queryKey: ['paper', id],
    queryFn: () => api.paper(id),
    enabled: Number.isFinite(id) && Boolean(health?.ok),
    staleTime: 60_000,
    retry: paperRetry,
    retryDelay: (attempt) => Math.min(400 * 2 ** attempt, 5000),
  })

  const { data: enriched } = useQuery({
    queryKey: ['paper', id, 'ads'],
    queryFn: () => api.paper(id, true),
    enabled: refsExpanded && Number.isFinite(id) && Boolean(paper?.references.length),
    staleTime: 60 * 60 * 1000,
    retry: paperRetry,
    retryDelay: (attempt) => Math.min(400 * 2 ** attempt, 5000),
  })

  const references = enriched?.references ?? paper?.references ?? []

  useEffect(() => {
    if (paper && Number.isFinite(id)) {
      saveLastPdf(id, paper.title || `论文 #${id}`)
    }
  }, [paper, id])

  const hasPdf = Boolean(paper?.has_pdf || paper?.pdf_relpath)
  const pdfReady = Number.isFinite(id) && Boolean(health?.ok)

  return (
    <div className="flex h-full">
      <div className="relative flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-3 border-b border-neutral-200/60 bg-white/60 px-4 py-2 backdrop-blur-sm dark:border-neutral-800/60 dark:bg-neutral-900/60">
          <button
            onClick={() => navigate('/library')}
            className="rounded-lg px-2 py-1 text-sm text-neutral-500 hover:bg-white/60 dark:hover:bg-neutral-800"
          >
            ← 文献库
          </button>
          <span className="truncate text-sm font-medium">{paper?.title ?? `论文 #${paperId}`}</span>
        </div>
        <div className="relative min-h-0 flex-1">
          {pdfReady ? (
            <PdfViewer paperId={id} />
          ) : (
            <div className="flex h-full items-center justify-center bg-neutral-200 text-sm text-neutral-500 dark:bg-neutral-800">
              连接后端…
            </div>
          )}
          {references.length > 0 && (
            <PdfReferencesDock
              references={references}
              onExpand={() => setRefsExpanded(true)}
            />
          )}
        </div>
      </div>
      {Number.isFinite(id) && (
        <PaperChatPanel
          paperId={id}
          paperTitle={paper?.title}
          hasPdf={hasPdf}
          chunks={paper?.chunks ?? 0}
        />
      )}
    </div>
  )
}
