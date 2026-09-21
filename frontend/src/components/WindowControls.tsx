import type { CSSProperties } from 'react'
import { getCurrentWindow } from '@tauri-apps/api/window'

const win = getCurrentWindow()
const noDrag = { WebkitAppRegion: 'no-drag' } as CSSProperties

export default function WindowControls() {
  return (
    <div className="relative z-10 flex shrink-0 items-center gap-2" style={noDrag}>
      <button
        type="button"
        aria-label="关闭"
        onClick={() => void win.close()}
        className="group flex h-3 w-3 items-center justify-center rounded-full bg-[#ff5f57] hover:brightness-95"
      >
        <span className="pointer-events-none text-[9px] font-bold leading-none text-[#4a0002] opacity-0 group-hover:opacity-100">
          ×
        </span>
      </button>
      <button
        type="button"
        aria-label="最小化"
        onClick={() => void win.minimize()}
        className="group flex h-3 w-3 items-center justify-center rounded-full bg-[#febc2e] hover:brightness-95"
      >
        <span className="pointer-events-none text-[9px] font-bold leading-none text-[#5a4200] opacity-0 group-hover:opacity-100">
          −
        </span>
      </button>
      <button
        type="button"
        aria-label="最大化"
        onClick={() => void win.toggleMaximize()}
        className="group flex h-3 w-3 items-center justify-center rounded-full bg-[#28c840] hover:brightness-95"
      >
        <span className="pointer-events-none text-[8px] font-bold leading-none text-[#003a0d] opacity-0 group-hover:opacity-100">
          ⤢
        </span>
      </button>
    </div>
  )
}
