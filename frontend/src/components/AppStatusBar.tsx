import { Link } from 'react-router-dom'
import type { Health } from '../lib/api'

type Props = {
  health: Health | undefined
  isPending: boolean
  isFetching: boolean
}

export default function AppStatusBar({ health, isPending, isFetching }: Props) {
  return (
    <div className="flex shrink-0 items-center gap-x-3 whitespace-nowrap text-xs text-neutral-500 dark:text-neutral-400">
      {health ? (
        <>
          <span>{health.papers} 篇论文</span>
          <span>{health.papers_with_pdf} 篇有 PDF</span>
          <span>{health.chunks.toLocaleString()} 个索引块</span>
          <Link to="/settings" className="flex items-center gap-2 hover:opacity-80">
            <Dot ok={health.tokens.ads} label="ADS" />
            <Dot ok={health.tokens.llm} label="LLM" />
            <Dot ok={health.tokens.embedding ?? false} label="Emb" />
            <Dot ok={health.tokens.zotero} label="Zotero" />
          </Link>
        </>
      ) : isPending || isFetching ? (
        <span className="text-neutral-400">连接中…</span>
      ) : (
        <span className="text-red-400">后端未连接</span>
      )}
    </div>
  )
}

function Dot({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span className={`h-2 w-2 rounded-full ${ok ? 'bg-green-500' : 'bg-red-400'}`} />
      {label}
    </span>
  )
}
