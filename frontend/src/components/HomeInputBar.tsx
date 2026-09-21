import ActionModeMenu from './ActionModeMenu'
import InvestigateSubPicker from './InvestigateSubPicker'
import PaperSeedPicker from './PaperSeedPicker'
import {
  getHomeModeConfig,
  type HomeMode,
  type InvestigateSubMode,
} from '../lib/homeModes'
import type { Paper } from '../lib/api'

export default function HomeInputBar({
  mode,
  onModeChange,
  investigateSub,
  onInvestigateSubChange,
  seed,
  onSeedSelect,
  onSeedClear,
  maxHops,
  onMaxHopsChange,
  input,
  onInputChange,
  onSubmit,
  disabled,
  running,
  className = '',
}: {
  mode: HomeMode
  onModeChange: (m: HomeMode) => void
  investigateSub: InvestigateSubMode
  onInvestigateSubChange: (m: InvestigateSubMode) => void
  seed: Paper | null
  onSeedSelect: (p: Paper) => void
  onSeedClear: () => void
  maxHops: number
  onMaxHopsChange: (h: number) => void
  input: string
  onInputChange: (v: string) => void
  onSubmit: () => void
  disabled?: boolean
  running?: boolean
  className?: string
}) {
  const cfg = getHomeModeConfig(mode)
  const submitDisabled =
    disabled ||
    running ||
    (mode === 'reference_chain' && !seed) ||
    (mode !== 'import' && !input.trim())

  const runningLabel =
    mode === 'investigate'
      ? '调查中…'
      : mode === 'reference_chain'
        ? '追踪中…'
        : mode === 'import'
          ? '入库中…'
          : '处理中…'

  return (
    <div className={`relative rounded-xl border border-neutral-200 bg-white px-2 py-1.5 shadow-sm dark:border-neutral-700 dark:bg-neutral-900 ${className}`}>
      <div className="flex flex-wrap items-center gap-1.5">
        <ActionModeMenu mode={mode} onChange={onModeChange} />
        {mode === 'investigate' && (
          <InvestigateSubPicker value={investigateSub} onChange={onInvestigateSubChange} />
        )}
        {mode === 'reference_chain' && (
          <PaperSeedPicker seed={seed} onSelect={onSeedSelect} onClear={onSeedClear} />
        )}
        <input
          value={input}
          onChange={(e) => onInputChange(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && !submitDisabled && onSubmit()}
          placeholder={cfg.placeholder}
          className="min-w-0 flex-1 bg-transparent px-1 py-1 text-sm outline-none"
        />
        {mode === 'reference_chain' && (
          <select
            value={maxHops}
            onChange={(e) => onMaxHopsChange(Number(e.target.value))}
            className="shrink-0 rounded-lg border border-neutral-200 px-1.5 py-1 text-xs dark:border-neutral-700 dark:bg-neutral-800"
          >
            {[1, 2, 3].map((h) => (
              <option key={h} value={h}>
                {h} 跳
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          onClick={onSubmit}
          disabled={submitDisabled}
          className="shrink-0 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {running ? runningLabel : cfg.submitLabel}
        </button>
      </div>
    </div>
  )
}
