import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { api } from './lib/api'
import AppStatusBar from './components/AppStatusBar'
import WindowControls from './components/WindowControls'
import LibraryPage from './pages/LibraryPage'
import JobsPage from './pages/JobsPage'
import PdfPage from './pages/PdfPage'
import InvestigatePage from './pages/InvestigatePage'
import GraphPage from './pages/GraphPage'
import DashboardPage from './pages/DashboardPage'
import SettingsPage from './pages/SettingsPage'

const NAV = [
  { to: '/', label: '首页', icon: '🏠' },
  { to: '/library', label: '文献库', icon: '📚' },
  { to: '/graph', label: '引文图谱', icon: '🕸️' },
  { to: '/dashboard', label: '仪表盘', icon: '📊' },
  { to: '/settings', label: '设置', icon: '⚙️' },
  { to: '/jobs', label: '任务', icon: '📋' },
]

const SIDEBAR_W = '13.5rem'
const TITLEBAR_H = '2.25rem'

export default function App() {
  const [inTauri, setInTauri] = useState(false)
  const { data: health, isPending, isFetching } = useQuery({
    queryKey: ['health'],
    queryFn: api.health,
    retry: 12,
    retryDelay: (attempt) => Math.min(500 * 2 ** attempt, 8000),
    refetchInterval: (query) => (query.state.data ? false : 2000),
  })

  useEffect(() => {
    import('@tauri-apps/api/core')
      .then(({ isTauri }) => setInTauri(isTauri()))
      .catch(() => setInTauri(false))
  }, [])

  return (
    <div className="flex h-full flex-col overflow-hidden bg-gradient-to-br from-slate-100 via-neutral-50 to-sky-50 text-neutral-900 dark:from-neutral-950 dark:via-neutral-950 dark:to-slate-950 dark:text-neutral-100">
      <header
        className="flex shrink-0 items-center gap-3 px-3"
        style={{ height: TITLEBAR_H }}
      >
        {inTauri && <WindowControls />}
        {inTauri ? (
          <div data-tauri-drag-region className="min-w-0 flex-1 self-stretch" aria-hidden />
        ) : (
          <div className="min-w-0 flex-1" />
        )}
        <AppStatusBar health={health} isPending={isPending} isFetching={isFetching} />
      </header>
      <div className="flex min-h-0 flex-1 gap-3 p-3 pt-0">
        <aside
          className="flex shrink-0 flex-col overflow-hidden rounded-2xl border border-white/40 bg-white/55 shadow-sm backdrop-blur-xl saturate-[1.4] dark:border-white/5 dark:bg-neutral-900/38 dark:shadow-none dark:saturate-100"
          style={{ width: SIDEBAR_W }}
        >
          <div className="shrink-0 px-4 py-4">
            <h1 className="text-lg font-bold tracking-tight">一问</h1>
            <p className="text-xs text-neutral-500 dark:text-neutral-400">检索 · 调查 · 导入</p>
          </div>
          <nav className="flex flex-col gap-0.5 overflow-y-auto px-2 pb-3">
            {NAV.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === '/'}
                className={({ isActive }) =>
                  `rounded-xl px-3 py-2 text-sm font-medium transition-all ${
                    isActive
                      ? 'glass-nav-active text-white'
                      : 'text-neutral-600 hover:bg-white/50 dark:text-neutral-300 dark:hover:bg-white/5'
                  }`
                }
              >
                <span className="mr-2">{n.icon}</span>
                {n.label}
              </NavLink>
            ))}
          </nav>
        </aside>
        <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <div className="h-full overflow-hidden rounded-2xl border border-white/40 bg-white/40 shadow-sm backdrop-blur-sm dark:border-white/5 dark:bg-neutral-900/40">
            <Routes>
              <Route path="/" element={<InvestigatePage />} />
              <Route path="/library" element={<LibraryPage />} />
              <Route path="/search" element={<Navigate to="/?mode=search_semantic" replace />} />
              <Route path="/investigate" element={<Navigate to="/" replace />} />
              <Route path="/chain" element={<Navigate to="/?mode=reference_chain" replace />} />
              <Route path="/graph" element={<GraphPage />} />
              <Route path="/manage" element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/jobs" element={<JobsPage />} />
              <Route path="/pdf/:paperId" element={<PdfPage />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  )
}
