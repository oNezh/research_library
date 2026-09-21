import { useEffect, useRef, useState, useCallback } from 'react'
import { useQuery } from '@tanstack/react-query'
import cytoscape from 'cytoscape'
import PaperDetailPanel from '../components/PaperDetailPanel'
import { useJobRunner, ProgressBar } from '../components/JobRunner'

interface GraphNode {
  id: string
  kind: 'paper' | 'missing_hub'
  paper_id?: number
  bibcode?: string | null
  label: string
  citing_papers?: number
}

interface GraphEdge {
  from_paper_id: number
  to_paper_id: number | null
  ref_bibcode: string
  in_library: boolean
}

interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  missing_hubs: { bibcode: string; citing_papers: number }[]
  stats: {
    papers: number
    edges: number
    internal_edges: number
    nodes_returned?: number
    truncated?: boolean
  }
}

async function fetchGraph(onlyConnected: boolean): Promise<GraphData> {
  const qs = new URLSearchParams({
    only_connected: String(onlyConnected),
    max_hubs: '100',
    max_papers: '800',
  })
  const r = await fetch(`/api/graph?${qs}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

function buildElements(data: GraphData, onlyConnected: boolean) {
  const connectedPaperIds = new Set<number>()
  for (const e of data.edges) {
    if (e.in_library && e.to_paper_id !== null) {
      connectedPaperIds.add(e.from_paper_id)
      connectedPaperIds.add(e.to_paper_id)
    }
  }

  const nodes = data.nodes
    .filter((n) => {
      if (n.kind === 'missing_hub') return (n.citing_papers ?? 0) >= 3
      if (!onlyConnected) return true
      return connectedPaperIds.has(n.paper_id!)
    })
    .map((n) => ({
      data: {
        id: n.id,
        label: n.label.length > 42 ? `${n.label.slice(0, 40)}…` : n.label,
        kind: n.kind,
        paper_id: n.paper_id,
        bibcode: n.bibcode,
      },
    }))
  const nodeIds = new Set(nodes.map((n) => n.data.id))

  const edges: cytoscape.ElementDefinition[] = data.edges
    .filter((e) => e.in_library && e.to_paper_id !== null)
    .map((e) => ({
      data: {
        id: `e-${e.from_paper_id}-${e.to_paper_id}`,
        source: `paper:${e.from_paper_id}`,
        target: `paper:${e.to_paper_id}`,
      },
    }))
    .filter((e) => nodeIds.has(e.data.source) && nodeIds.has(e.data.target))

  for (const e of data.edges) {
    if (!e.in_library && nodeIds.has(`ext:${e.ref_bibcode}`) && nodeIds.has(`paper:${e.from_paper_id}`)) {
      edges.push({
        data: {
          id: `x-${e.from_paper_id}-${e.ref_bibcode}`,
          source: `paper:${e.from_paper_id}`,
          target: `ext:${e.ref_bibcode}`,
        },
      })
    }
  }

  return [...nodes, ...edges]
}

export default function GraphPage() {
  const containerRef = useRef<HTMLDivElement>(null)
  const cyRef = useRef<cytoscape.Core | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [onlyConnected, setOnlyConnected] = useState(true)
  const [layouting, setLayouting] = useState(false)
  const runner = useJobRunner()

  const { data, isFetching, error, refetch } = useQuery({
    queryKey: ['graph', onlyConnected],
    queryFn: () => fetchGraph(onlyConnected),
    staleTime: 5 * 60_000,
  })

  const runLayout = useCallback((cy: cytoscape.Core) => {
    setLayouting(true)
    cy.layout({
      name: 'circle',
      animate: false,
    } as cytoscape.LayoutOptions).run()
    requestAnimationFrame(() => {
      cy.layout({
        name: 'cose',
        animate: false,
        nodeOverlap: 8,
        idealEdgeLength: 60,
        numIter: 150,
      } as cytoscape.LayoutOptions).run()
      setLayouting(false)
    })
  }, [])

  useEffect(() => {
    if (!data || !containerRef.current) return

    const elements = buildElements(data, onlyConnected)
    const cy = cyRef.current

    if (cy) {
      cy.elements().remove()
      cy.add(elements)
      runLayout(cy)
    } else {
      const instance = cytoscape({
        container: containerRef.current,
        elements,
        style: [
          {
            selector: 'node[kind="paper"]',
            style: {
              'background-color': '#3b82f6',
              label: '',
              width: 14,
              height: 14,
            },
          },
          {
            selector: 'node[kind="missing_hub"]',
            style: {
              'background-color': '#f59e0b',
              shape: 'diamond',
              label: '',
              width: 16,
              height: 16,
            },
          },
          {
            selector: 'node:selected, node.hover',
            style: {
              label: 'data(label)',
              'font-size': '8px',
              color: '#374151',
              'text-wrap': 'ellipsis',
              'text-max-width': '120px',
            },
          },
          {
            selector: 'edge',
            style: {
              width: 0.7,
              'line-color': '#d1d5db',
              'curve-style': 'haystack',
              opacity: 0.6,
            },
          },
          {
            selector: 'node:selected',
            style: { 'background-color': '#dc2626', width: 22, height: 22 },
          },
        ],
        minZoom: 0.1,
        maxZoom: 4,
        wheelSensitivity: 0.2,
      })

      instance.on('tap', 'node[kind="paper"]', (ev) => {
        const pid = ev.target.data('paper_id')
        if (typeof pid === 'number') setSelectedId(pid)
      })
      instance.on('mouseover', 'node', (ev) => ev.target.addClass('hover'))
      instance.on('mouseout', 'node', (ev) => ev.target.removeClass('hover'))

      cyRef.current = instance
      runLayout(instance)
    }
  }, [data, onlyConnected, runLayout])

  useEffect(() => {
    return () => {
      cyRef.current?.destroy()
      cyRef.current = null
    }
  }, [])

  return (
    <div className="flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex flex-wrap items-center gap-3 border-b border-neutral-200 bg-white px-4 py-2.5 dark:border-neutral-800 dark:bg-neutral-900">
          <span className="text-sm font-semibold">引文图谱</span>
          {data && (
            <span className="text-xs text-neutral-400">
              {data.stats.nodes_returned ?? data.nodes.length} 节点 · {data.stats.internal_edges} 库内边
              {data.stats.truncated && ' · 已截断'}
            </span>
          )}
          <label className="ml-auto flex items-center gap-1.5 text-xs text-neutral-500">
            <input
              type="checkbox"
              checked={onlyConnected}
              onChange={(e) => setOnlyConnected(e.target.checked)}
            />
            只显示有连接的节点
          </label>
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="rounded-md border border-neutral-300 px-2 py-1 text-xs hover:bg-neutral-50 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            刷新
          </button>
          <button
            onClick={() => runner.start('citation_sync', { missing_only: true })}
            disabled={runner.status === 'running'}
            className="rounded-md bg-neutral-800 px-2 py-1 text-xs text-white hover:bg-neutral-700 disabled:opacity-50 dark:bg-neutral-200 dark:text-neutral-900"
          >
            同步引用
          </button>
        </div>

        {data?.stats.truncated && (
          <p className="border-b border-amber-200 bg-amber-50 px-4 py-1 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
            图谱已截断至 800 篇论文，可在设置中同步更多引用或缩小范围。
          </p>
        )}

        {runner.status === 'running' && (
          <div className="px-4 pt-2">
            <ProgressBar progress={runner.progress} message={runner.message} />
          </div>
        )}

        {error && <p className="p-4 text-sm text-red-500">{String(error)}</p>}

        <div className="relative min-h-0 flex-1">
          <div ref={containerRef} className="h-full w-full" />
          {(isFetching || layouting) && (
            <div className="absolute inset-0 flex items-center justify-center bg-white/60 text-sm text-neutral-500 dark:bg-neutral-950/60">
              {layouting ? '布局中…' : '加载中…'}
            </div>
          )}
        </div>
      </div>

      {selectedId !== null && (
        <PaperDetailPanel paperId={selectedId} onClose={() => setSelectedId(null)} onSelectPaper={setSelectedId} />
      )}
    </div>
  )
}
