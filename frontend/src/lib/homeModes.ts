export type HomeMode =
  | 'investigate'
  | 'search_semantic'
  | 'search_fts'
  | 'search_remote'
  | 'reference_chain'
  | 'import'

export type InvestigateSubMode = 'semantic_report' | 'topic_dossier'

export interface HomeModeConfig {
  id: HomeMode
  label: string
  placeholder: string
  hint?: string
  submitLabel: string
}

export const HOME_MODES: HomeModeConfig[] = [
  {
    id: 'investigate',
    label: '深入调查',
    placeholder: '输入调查主题，如：不同环境下球状星团的潮汐瓦解机制…',
    hint: '耗时约 1–3 分钟',
    submitLabel: '开始调查',
  },
  {
    id: 'search_semantic',
    label: '语义检索',
    placeholder: '输入检索式，如：tidal disruption of globular clusters…',
    submitLabel: '检索',
  },
  {
    id: 'search_fts',
    label: '全文 FTS',
    placeholder: '输入关键词或短语…',
    submitLabel: '检索',
  },
  {
    id: 'search_remote',
    label: 'ADS/arXiv 远程',
    placeholder: '输入标题、作者或 ADS 检索式…',
    submitLabel: '检索',
  },
  {
    id: 'reference_chain',
    label: '链式检索',
    placeholder: '要沿引文链追查的问题，如：这个方法的原始出处和后续改进？',
    hint: '耗时可达 5–15 分钟',
    submitLabel: '开始追踪',
  },
  {
    id: 'import',
    label: '导入文献',
    placeholder: '粘贴 DOI / arXiv id / bibcode / 参考文献行…',
    submitLabel: '解析并入库',
  },
]

export const INVESTIGATE_SUB_MODES: { id: InvestigateSubMode; label: string }[] = [
  { id: 'semantic_report', label: '带引用报告' },
  { id: 'topic_dossier', label: '主题综述' },
]

export function getHomeModeConfig(mode: HomeMode): HomeModeConfig {
  return HOME_MODES.find((m) => m.id === mode) ?? HOME_MODES[0]
}

export function parseHomeMode(raw: string | null): HomeMode | null {
  if (!raw) return null
  return HOME_MODES.some((m) => m.id === raw) ? (raw as HomeMode) : null
}

export function investigateSubLabel(sub: InvestigateSubMode): string {
  return INVESTIGATE_SUB_MODES.find((m) => m.id === sub)?.label ?? sub
}
