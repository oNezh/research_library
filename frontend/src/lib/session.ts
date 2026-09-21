export interface LibrarySession {
  q: string
  qInput: string
  yearFrom: string
  yearTo: string
  hasPdf: '' | 'true' | 'false'
  missingMetadataSync?: boolean
  onlyPendingMetadata?: boolean
  sort: string
  order: 'asc' | 'desc'
  offset: number
  selectedId: number | null
  scrollTop: number
}

export interface LastPdfSession {
  paperId: number
  title: string
}

const LIBRARY_KEY = 'rc-library-session'
const PDF_KEY = 'rc-last-pdf'

export function loadLibrarySession(): Partial<LibrarySession> | null {
  try {
    const raw = sessionStorage.getItem(LIBRARY_KEY)
    return raw ? (JSON.parse(raw) as Partial<LibrarySession>) : null
  } catch {
    return null
  }
}

export function saveLibrarySession(session: LibrarySession) {
  try {
    sessionStorage.setItem(LIBRARY_KEY, JSON.stringify(session))
  } catch {
    /* ignore */
  }
}

export function saveLastPdf(paperId: number, title: string) {
  try {
    sessionStorage.setItem(PDF_KEY, JSON.stringify({ paperId, title } satisfies LastPdfSession))
  } catch {
    /* ignore */
  }
}

export function openPaperInLibrary(paperId: number) {
  const prev = loadLibrarySession()
  saveLibrarySession({
    q: prev?.q ?? '',
    qInput: prev?.qInput ?? '',
    yearFrom: prev?.yearFrom ?? '',
    yearTo: prev?.yearTo ?? '',
    hasPdf: (prev?.hasPdf ?? '') as '' | 'true' | 'false',
    missingMetadataSync: prev?.missingMetadataSync ?? false,
    onlyPendingMetadata: prev?.onlyPendingMetadata ?? false,
    sort: prev?.sort ?? 'updated_at',
    order: (prev?.order ?? 'desc') as 'asc' | 'desc',
    offset: prev?.offset ?? 0,
    selectedId: paperId,
    scrollTop: prev?.scrollTop ?? 0,
  })
}

export function loadLastPdf(): LastPdfSession | null {
  try {
    const raw = sessionStorage.getItem(PDF_KEY)
    return raw ? (JSON.parse(raw) as LastPdfSession) : null
  } catch {
    return null
  }
}
