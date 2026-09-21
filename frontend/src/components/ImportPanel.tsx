import { useCallback, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ProgressBar, useJobRunner } from './JobRunner'

export default function ImportPanel({ runner }: { runner: ReturnType<typeof useJobRunner> }) {
  const [dragOver, setDragOver] = useState(false)
  const [uploadMsg, setUploadMsg] = useState('')
  const qc = useQueryClient()

  const handleFiles = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      for (const file of Array.from(files)) {
        if (!file.name.toLowerCase().endsWith('.pdf')) continue
        const fd = new FormData()
        fd.append('file', file)
        setUploadMsg(`上传 ${file.name}…`)
        try {
          const r = await fetch('/api/upload/pdf', { method: 'POST', body: fd })
          if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText)
          const body = await r.json()
          setUploadMsg(`已提交摄取任务 #${body.job_id}（${file.name}），进度见任务页`)
          qc.invalidateQueries({ queryKey: ['jobs'] })
        } catch (e) {
          setUploadMsg(`上传失败：${String((e as Error).message)}`)
        }
      }
    },
    [qc],
  )

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          handleFiles(e.dataTransfer.files)
        }}
        className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed py-10 transition-colors ${
          dragOver ? 'border-blue-500 bg-blue-50 dark:bg-blue-950' : 'border-neutral-300 dark:border-neutral-700'
        }`}
      >
        <p className="text-sm text-neutral-500">拖拽 PDF 到这里，或</p>
        <label className="mt-2 cursor-pointer rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700">
          选择文件
          <input type="file" accept=".pdf" multiple className="hidden" onChange={(e) => handleFiles(e.target.files)} />
        </label>
        <p className="mt-2 text-xs text-neutral-400">自动提取 DOI/arXiv → ADS 匹配 → 入库</p>
      </div>
      {uploadMsg && <p className="text-xs text-blue-600 dark:text-blue-400">{uploadMsg}</p>}

      {runner.status === 'running' && <ProgressBar progress={runner.progress} message={runner.message} />}
      {runner.status === 'done' && runner.result != null && (
        <pre className="max-h-40 overflow-auto rounded bg-neutral-50 p-2 text-xs text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300">
          {JSON.stringify(runner.result, null, 2)}
        </pre>
      )}
      {runner.status === 'error' && <p className="text-xs text-red-500">{runner.error}</p>}
    </div>
  )
}
