import { useEffect, useRef, useState } from 'react'
import { INVESTIGATE_SUB_MODES, investigateSubLabel, type InvestigateSubMode } from '../lib/homeModes'

const pickerBtnCls =
  'flex max-w-[7rem] items-center gap-1 rounded-lg border border-neutral-200 bg-neutral-50 px-2 py-1 text-xs font-medium text-neutral-700 hover:bg-neutral-100 dark:border-neutral-600 dark:bg-neutral-800 dark:text-neutral-200 dark:hover:bg-neutral-700'

export default function InvestigateSubPicker({
  value,
  onChange,
}: {
  value: InvestigateSubMode
  onChange: (m: InvestigateSubMode) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  return (
    <div ref={ref} className="relative shrink-0">
      <button type="button" onClick={() => setOpen((v) => !v)} className={pickerBtnCls}>
        <span className="truncate">{investigateSubLabel(value)}</span>
        <span className="text-[10px] text-neutral-400">▾</span>
      </button>
      {open && (
        <ul className="absolute bottom-full left-0 z-50 mb-1 min-w-[9rem] overflow-hidden rounded-lg border border-neutral-200 bg-white py-1 shadow-lg dark:border-neutral-700 dark:bg-neutral-800">
          {INVESTIGATE_SUB_MODES.map((m) => (
            <li key={m.id}>
              <button
                type="button"
                onClick={() => {
                  onChange(m.id)
                  setOpen(false)
                }}
                className={`w-full px-3 py-2 text-left text-xs hover:bg-neutral-100 dark:hover:bg-neutral-700 ${
                  m.id === value ? 'font-semibold text-blue-600 dark:text-blue-400' : 'text-neutral-700 dark:text-neutral-200'
                }`}
              >
                {m.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
