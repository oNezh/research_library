import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, type ChatTurn } from '../lib/api'
import { ProgressBar } from './JobRunner'
import { useBackgroundJob } from './useBackgroundJob'

interface ChainResult {
  markdown_report?: string
  question?: string
}

type Message =
  | { id: string; kind: 'qa'; role: 'user' | 'assistant'; content: string; retrieval_source?: string }
  | { id: string; kind: 'chain'; jobId: number; question: string; status: 'running' | 'done' | 'error'; progress: number; message: string; content?: string; error?: string }

function chainStorageKey(paperId: number) {
  return `pdf-chain-jobs-${paperId}`
}

export default function PaperChatPanel({
  paperId,
  paperTitle,
  hasPdf,
  chunks,
}: {
  paperId: number
  paperTitle?: string
  hasPdf: boolean
  chunks: number
}) {
  const [input, setInput] = useState('')
  const [chainMode, setChainMode] = useState(false)
  const [maxHops, setMaxHops] = useState(2)
  const [messages, setMessages] = useState<Message[]>([])
  const [pending, setPending] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const [selection, setSelection] = useState('')
  const listRef = useRef<HTMLDivElement>(null)
  const qc = useQueryClient()
  const bg = useBackgroundJob()
  const chainQuestions = useRef<Map<number, string>>(new Map())

  const { data: paper } = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => api.paper(paperId),
    retry: (failureCount, error) =>
      failureCount < 5 &&
      (String(error).includes('Internal Server Error') ||
        String(error).includes('Failed to fetch') ||
        String(error).includes('503')),
    retryDelay: (attempt) => Math.min(400 * 2 ** attempt, 5000),
  })

  useEffect(() => {
    const raw = localStorage.getItem(chainStorageKey(paperId))
    if (!raw) return
    try {
      const ids: number[] = JSON.parse(raw)
      ids.forEach((id) => bg.watch(id))
    } catch {
      /* ignore */
    }
  }, [paperId, bg])

  useEffect(() => {
    setMessages((prev) => {
      const qa = prev.filter((m) => m.kind === 'qa')
      const chainMsgs: Message[] = bg.jobs
        .filter((j) => j.kind === 'reference_chain')
        .map((j) => ({
          id: `chain-${j.jobId}`,
          kind: 'chain' as const,
          jobId: j.jobId,
          question: chainQuestions.current.get(j.jobId) ?? (j.result as ChainResult | null)?.question ?? '',
          status: j.status,
          progress: j.progress,
          message: j.message,
          content: j.status === 'done' ? (j.result as ChainResult)?.markdown_report : undefined,
          error: j.error ?? undefined,
        }))
      const merged = [...qa, ...chainMsgs]
      return merged.sort((a, b) => {
        if (a.kind === 'chain' && b.kind === 'chain') return a.jobId - b.jobId
        return 0
      })
    })
  }, [bg.jobs])

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, pending])

  const appendToNotes = useMutation({
    mutationFn: async (text: string) => {
      const current = paper?.notes ?? ''
      const block = current.trim() ? `${current.trim()}\n\n> ${text.trim()}\n` : `> ${text.trim()}\n`
      return api.patchPaper(paperId, { notes: block })
    },
    onSuccess: () => {
      setToast('已加入笔记')
      setSelection('')
      qc.invalidateQueries({ queryKey: ['paper', paperId] })
      setTimeout(() => setToast(null), 2000)
    },
  })

  const send = async () => {
    const q = input.trim()
    if (!q) return

    if (chainMode) {
      if (!hasPdf) return
      setInput('')
      const userMsg: Message = { id: `u-${Date.now()}`, kind: 'qa', role: 'user', content: `[链式搜索] ${q}` }
      setMessages((prev) => [...prev, userMsg])
      try {
        const jobId = await bg.submit('reference_chain', {
          paper_id: paperId,
          question: q,
          max_hops: maxHops,
        })
        chainQuestions.current.set(jobId, q)
        const stored = JSON.parse(localStorage.getItem(chainStorageKey(paperId)) || '[]') as number[]
        if (!stored.includes(jobId)) {
          localStorage.setItem(chainStorageKey(paperId), JSON.stringify([...stored, jobId]))
        }
        setMessages((prev) => [
          ...prev,
          {
            id: `chain-${jobId}`,
            kind: 'chain',
            jobId,
            question: q,
            status: 'running',
            progress: 0,
            message: '链式搜索已提交…',
          },
        ])
      } catch (e) {
        setMessages((prev) => [
          ...prev,
          { id: `err-${Date.now()}`, kind: 'qa', role: 'assistant', content: String((e as Error).message) },
        ])
      }
      return
    }

    if (chunks <= 0) return
    const history: ChatTurn[] = messages
      .filter((m): m is Extract<Message, { kind: 'qa' }> => m.kind === 'qa')
      .map((m) => ({ role: m.role, content: m.content }))
    setMessages((prev) => [...prev, { id: `u-${Date.now()}`, kind: 'qa', role: 'user', content: q }])
    setInput('')
    setPending(true)
    try {
      const res = await api.askPaper(paperId, q, history)
      setMessages((prev) => [
        ...prev,
        {
          id: `a-${Date.now()}`,
          kind: 'qa',
          role: 'assistant',
          content: res.answer,
          retrieval_source: res.retrieval_source,
        },
      ])
    } catch (e) {
      setMessages((prev) => [
        ...prev,
        { id: `err-${Date.now()}`, kind: 'qa', role: 'assistant', content: String((e as Error).message) },
      ])
    } finally {
      setPending(false)
    }
  }

  const onMouseUp = () => {
    const sel = window.getSelection()?.toString().trim()
    setSelection(sel || '')
  }

  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
      <div className="border-b border-neutral-200 px-3 py-2 dark:border-neutral-800">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold">对话</span>
          <label className="flex items-center gap-1.5 text-xs text-neutral-500">
            <input type="checkbox" checked={chainMode} onChange={(e) => setChainMode(e.target.checked)} />
            链式搜索
          </label>
        </div>
        {chainMode && (
          <p className="mt-1 truncate text-xs text-blue-600 dark:text-blue-400">起点：{paperTitle ?? `论文 #${paperId}`}</p>
        )}
      </div>

      <div ref={listRef} className="min-h-0 flex-1 space-y-3 overflow-auto p-3" onMouseUp={onMouseUp}>
        {messages.length === 0 && !chainMode && chunks <= 0 && (
          <p className="text-xs text-neutral-400">需先运行 semantic_index 才能提问。</p>
        )}
        {messages.length === 0 && chainMode && !hasPdf && (
          <p className="text-xs text-neutral-400">当前论文无 PDF，无法链式搜索。</p>
        )}
        {messages.map((m) =>
          m.kind === 'chain' ? (
            <div key={m.id} className="rounded-lg border border-amber-200 bg-amber-50/50 p-2.5 dark:border-amber-900 dark:bg-amber-950/30">
              <p className="text-xs font-medium text-amber-700 dark:text-amber-400">链式搜索 · #{m.jobId}</p>
              <p className="mt-1 text-sm">{m.question}</p>
              {m.status === 'running' && <ProgressBar progress={m.progress} message={m.message} />}
              {m.status === 'error' && <p className="mt-1 text-xs text-red-500">{m.error}</p>}
              {m.status === 'done' && m.content && (
                <SelectableMarkdown content={m.content} onAddNotes={(t) => appendToNotes.mutate(t)} />
              )}
            </div>
          ) : (
            <div
              key={m.id}
              className={`rounded-lg px-2.5 py-2 text-sm ${
                m.role === 'user'
                  ? 'ml-6 bg-blue-600 text-white'
                  : 'mr-6 border border-neutral-200 bg-neutral-50 dark:border-neutral-700 dark:bg-neutral-800'
              }`}
            >
              {m.role === 'assistant' ? (
                <SelectableMarkdown content={m.content} onAddNotes={(t) => appendToNotes.mutate(t)} />
              ) : (
                m.content
              )}
              {m.retrieval_source && (
                <p className="mt-1 text-xs opacity-60">检索：{m.retrieval_source}</p>
              )}
            </div>
          ),
        )}
        {pending && <p className="text-xs text-neutral-400">检索原文并生成回答（约 30–60 秒）…</p>}
      </div>

      {selection && (
        <div className="border-t border-neutral-200 px-3 py-2 dark:border-neutral-800">
          <button
            onClick={() => appendToNotes.mutate(selection)}
            disabled={appendToNotes.isPending}
            className="w-full rounded-md bg-green-600 px-3 py-1.5 text-xs text-white hover:bg-green-700 disabled:opacity-50"
          >
            将选中内容加入笔记
          </button>
        </div>
      )}

      {toast && <div className="px-3 py-1 text-center text-xs text-green-600">{toast}</div>}

      <div className="border-t border-neutral-200 p-3 dark:border-neutral-800">
        <div className="flex gap-1.5">
          {chainMode && (
            <select
              value={maxHops}
              onChange={(e) => setMaxHops(Number(e.target.value))}
              className="rounded-md border border-neutral-300 px-2 py-1.5 text-sm dark:border-neutral-700 dark:bg-neutral-800"
            >
              {[1, 2, 3].map((h) => (
                <option key={h} value={h}>
                  {h} 跳
                </option>
              ))}
            </select>
          )}
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && !pending && send()}
            disabled={pending || (chainMode ? !hasPdf : chunks <= 0)}
            placeholder={
              chainMode
                ? '沿引文链追查的问题…'
                : '向这篇论文提问…'
            }
            className="min-w-0 flex-1 rounded-md border border-neutral-300 px-2 py-1.5 text-sm outline-none focus:border-blue-500 dark:border-neutral-700 dark:bg-neutral-800"
          />
          <button
            onClick={send}
            disabled={pending || !input.trim() || (chainMode ? !hasPdf : chunks <= 0)}
            className="rounded-md bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-700 disabled:opacity-50"
          >
            发送
          </button>
        </div>
        {chainMode && (
          <p className="mt-1 text-xs text-neutral-400">后台运行 5–15 分钟，可继续阅读 PDF。</p>
        )}
      </div>
    </aside>
  )
}

function SelectableMarkdown({ content, onAddNotes }: { content: string; onAddNotes: (t: string) => void }) {
  const onContextMenu = (e: React.MouseEvent) => {
    const sel = window.getSelection()?.toString().trim()
    if (sel) {
      e.preventDefault()
      onAddNotes(sel)
    }
  }
  return (
    <article className="prose prose-sm max-w-none dark:prose-invert" onContextMenu={onContextMenu}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </article>
  )
}
