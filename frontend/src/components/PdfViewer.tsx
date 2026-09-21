import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'

export default function PdfViewer({ paperId }: { paperId: number }) {
  const [src, setSrc] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  const retry = useCallback(() => setReloadKey((k) => k + 1), [])

  useEffect(() => {
    let active = true
    setLoading(true)
    setFailed(false)
    setSrc((prev) => {
      if (prev) URL.revokeObjectURL(prev)
      return null
    })
    ;(async () => {
      try {
        const blob = await api.paperPdfBlob(paperId)
        if (!active) return
        setSrc(URL.createObjectURL(blob))
        setLoading(false)
      } catch {
        if (!active) return
        setFailed(true)
        setLoading(false)
      }
    })()
    return () => {
      active = false
      setSrc((prev) => {
        if (prev) URL.revokeObjectURL(prev)
        return null
      })
    }
  }, [paperId, reloadKey])

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center bg-neutral-200 text-sm text-neutral-500 dark:bg-neutral-800">
        加载 PDF…
      </div>
    )
  }

  if (failed || !src) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 bg-neutral-200 text-sm text-neutral-500 dark:bg-neutral-800">
        <span>PDF 暂时无法加载</span>
        <button
          type="button"
          onClick={retry}
          className="rounded-md bg-blue-600 px-3 py-1.5 text-white hover:bg-blue-700"
        >
          重试
        </button>
      </div>
    )
  }

  return (
    <iframe
      title="PDF"
      src={src}
      className="h-full w-full border-0 bg-neutral-200 dark:bg-neutral-800"
    />
  )
}
