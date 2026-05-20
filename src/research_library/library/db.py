"""Local SQLite + FTS5 index for paper metadata under index/library.db."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from research_library.config import get_data_dir, get_index_dir


def db_path() -> Path:
    return get_index_dir() / "library.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    for pragma in (
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA busy_timeout=5000",
        "PRAGMA temp_store=MEMORY",
    ):
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError:
            pass
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            arxiv_id TEXT,
            bibcode TEXT,
            title TEXT NOT NULL DEFAULT '',
            abstract TEXT NOT NULL DEFAULT '',
            authors_json TEXT NOT NULL DEFAULT '[]',
            categories_json TEXT NOT NULL DEFAULT '[]',
            matched_keywords_json TEXT NOT NULL DEFAULT '[]',
            published TEXT,
            source TEXT NOT NULL DEFAULT '',
            pdf_relpath TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(arxiv_id)
        );

        CREATE INDEX IF NOT EXISTS idx_papers_bibcode ON papers(bibcode);
        CREATE INDEX IF NOT EXISTS idx_papers_published ON papers(published);

        CREATE TABLE IF NOT EXISTS paper_references (
            from_paper_id INTEGER NOT NULL,
            ref_bibcode TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (from_paper_id, ref_bibcode),
            FOREIGN KEY (from_paper_id) REFERENCES papers(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_refs_ref ON paper_references(ref_bibcode);

        CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
            paper_id UNINDEXED,
            title,
            abstract,
            tokenize = 'porter unicode61'
        );
        """
    )
    conn.commit()


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='papers'"
    )
    if cur.fetchone() is None:
        init_schema(conn)
    _ensure_paper_references_table(conn)
    _ensure_bibcode_unique_index(conn)
    _ensure_pdf_relpath_index(conn)
    _ensure_papers_source_columns(conn)
    _ensure_semantic_tables(conn)


def _ensure_pdf_relpath_index(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_pdf_relpath ON papers(pdf_relpath)"
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()


_PAPERS_SOURCE_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("pub_version", "TEXT"),
    ("pdf_fetched_at", "TEXT"),
    ("source_text_relpath", "TEXT"),
    ("source_kind", "TEXT"),
    ("source_backend", "TEXT"),
    ("arxiv_source_relpath", "TEXT"),
    ("source_fetched_at", "TEXT"),
)


def _ensure_papers_source_columns(conn: sqlite3.Connection) -> None:
    """Add source / version columns to ``papers`` for the TeX-source embedding upgrade."""
    try:
        existing = {r[1] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    except sqlite3.OperationalError:
        return
    altered = False
    for name, decl in _PAPERS_SOURCE_COLUMNS:
        if name in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {decl}")
            altered = True
        except sqlite3.OperationalError:
            continue
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_source_kind ON papers(source_kind)"
        )
    except sqlite3.OperationalError:
        pass
    if altered:
        conn.commit()


def _ensure_paper_references_table(conn: sqlite3.Connection) -> None:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='paper_references'"
    )
    if cur.fetchone() is None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_references (
                from_paper_id INTEGER NOT NULL,
                ref_bibcode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (from_paper_id, ref_bibcode),
                FOREIGN KEY (from_paper_id) REFERENCES papers(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_paper_refs_ref ON paper_references(ref_bibcode);
            """
        )
        conn.commit()


def _ensure_bibcode_unique_index(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_bibcode
            ON papers(bibcode) WHERE bibcode IS NOT NULL AND bibcode != ''
            """
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()


def _ensure_semantic_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL,
            chunk_ord INTEGER NOT NULL,
            char_start INTEGER NOT NULL,
            char_end INTEGER NOT NULL,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_chunks_paper ON paper_chunks(paper_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS paper_chunks_fts USING fts5(
            chunk_id UNINDEXED,
            paper_id UNINDEXED,
            chunk_ord UNINDEXED,
            text,
            tokenize = 'porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS paper_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER NOT NULL,
            tag_key TEXT NOT NULL,
            tag_value TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_paper_tags_paper ON paper_tags(paper_id);

        CREATE TABLE IF NOT EXISTS extraction_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL DEFAULT '',
            schema_json TEXT NOT NULL DEFAULT '',
            result_json TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    _maybe_backfill_chunks_fts(conn)


def _maybe_backfill_chunks_fts(conn: sqlite3.Connection) -> None:
    try:
        n_fts = conn.execute("SELECT COUNT(*) FROM paper_chunks_fts").fetchone()[0]
        n_ch = conn.execute("SELECT COUNT(*) FROM paper_chunks").fetchone()[0]
    except sqlite3.OperationalError:
        return
    if n_ch == 0 or int(n_fts) > 0:
        return
    for row in conn.execute(
        "SELECT id, paper_id, chunk_ord, text FROM paper_chunks ORDER BY id"
    ):
        conn.execute(
            """
            INSERT INTO paper_chunks_fts(chunk_id, paper_id, chunk_ord, text)
            VALUES (?, ?, ?, ?)
            """,
            (int(row[0]), int(row[1]), int(row[2]), str(row[3] or "")),
        )
    conn.commit()


def delete_chunks_for_paper(conn: sqlite3.Connection, paper_id: int) -> None:
    """Remove SQLite chunks for one paper (caller commits). Chroma delete is separate."""
    ensure_schema(conn)
    conn.execute("DELETE FROM paper_chunks_fts WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM paper_chunks WHERE paper_id = ?", (paper_id,))


def replace_paper_chunks(
    conn: sqlite3.Connection,
    paper_id: int,
    chunks: List[Dict[str, Any]],
) -> List[int]:
    """Delete existing chunks, insert new rows. Each chunk dict: chunk_ord, char_start, char_end, text, text_hash."""
    ensure_schema(conn)
    delete_chunks_for_paper(conn, paper_id)
    now = _now_iso()
    ids: List[int] = []
    for c in chunks:
        conn.execute(
            """
            INSERT INTO paper_chunks (
                paper_id, chunk_ord, char_start, char_end, text, text_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id,
                int(c["chunk_ord"]),
                int(c["char_start"]),
                int(c["char_end"]),
                str(c.get("text") or ""),
                str(c.get("text_hash") or ""),
                now,
            ),
        )
        last = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        ids.append(last)
        conn.execute(
            """
            INSERT INTO paper_chunks_fts(chunk_id, paper_id, chunk_ord, text)
            VALUES (?, ?, ?, ?)
            """,
            (last, paper_id, int(c["chunk_ord"]), str(c.get("text") or "")),
        )
    return ids


def search_chunks_fts(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
    *,
    paper_id: Optional[int] = None,
    exclude_paper_id: Optional[int] = None,
    fts_join: str = " AND ",
    fts_max_terms: int = 24,
) -> List[Dict[str, Any]]:
    """BM25-ranked PDF chunks (requires chunked papers and FTS sync)."""
    ensure_schema(conn)
    q = query.strip()
    if not q:
        return []
    safe = _fts_match_terms(q, max_terms=fts_max_terms, joiner=fts_join)
    if not safe:
        return []
    if paper_id is not None:
        sql = """
            SELECT chunk_id, paper_id, chunk_ord, text,
                   bm25(paper_chunks_fts) AS rank
            FROM paper_chunks_fts
            WHERE paper_chunks_fts MATCH ? AND paper_id = ?
            ORDER BY rank
            LIMIT ?
        """
        rows = conn.execute(sql, (safe, int(paper_id), limit)).fetchall()
    elif exclude_paper_id is not None:
        sql = """
            SELECT chunk_id, paper_id, chunk_ord, text,
                   bm25(paper_chunks_fts) AS rank
            FROM paper_chunks_fts
            WHERE paper_chunks_fts MATCH ? AND paper_id != ?
            ORDER BY rank
            LIMIT ?
        """
        rows = conn.execute(sql, (safe, int(exclude_paper_id), limit)).fetchall()
    else:
        sql = """
            SELECT chunk_id, paper_id, chunk_ord, text,
                   bm25(paper_chunks_fts) AS rank
            FROM paper_chunks_fts
            WHERE paper_chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        rows = conn.execute(sql, (safe, limit)).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "chunk_id": int(r[0]),
                "paper_id": int(r[1]),
                "chunk_ord": int(r[2]),
                "text": r[3],
                "rank": float(r[4]) if r[4] is not None else 0.0,
            }
        )
    return out


def get_chunk_row(conn: sqlite3.Connection, chunk_id: int) -> Optional[Dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM paper_chunks WHERE id = ?", (chunk_id,)
    ).fetchone()
    return dict(row) if row else None


def list_paper_ids_with_pdf(conn: sqlite3.Connection) -> List[int]:
    """Papers that have a non-empty pdf_relpath."""
    ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT id FROM papers
        WHERE pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''
        ORDER BY id
        """
    )
    return [int(r[0]) for r in cur.fetchall()]


def list_paper_ids_with_pdf_missing_chunks(conn: sqlite3.Connection) -> List[int]:
    """Papers with a local PDF but no rows in ``paper_chunks`` yet."""
    ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT p.id FROM papers p
        WHERE p.pdf_relpath IS NOT NULL AND TRIM(p.pdf_relpath) != ''
          AND NOT EXISTS (SELECT 1 FROM paper_chunks c WHERE c.paper_id = p.id)
        ORDER BY p.id
        """
    )
    return [int(r[0]) for r in cur.fetchall()]


def paper_id_for_absolute_pdf(conn: sqlite3.Connection, abs_pdf: str) -> Optional[int]:
    """Match a filesystem PDF path to ``papers.id`` via ``pdf_relpath`` under ``get_data_dir()``."""
    ensure_schema(conn)
    try:
        cand = Path(abs_pdf).resolve()
    except OSError:
        return None
    if not cand.is_file():
        return None
    root = get_data_dir().resolve()
    try:
        rel_candidate = str(cand.relative_to(root))
    except ValueError:
        rel_candidate = None
    if rel_candidate:
        row = conn.execute(
            "SELECT id FROM papers WHERE pdf_relpath = ? LIMIT 1",
            (rel_candidate,),
        ).fetchone()
        if row:
            return int(row[0])
        row = conn.execute(
            "SELECT id FROM papers WHERE TRIM(pdf_relpath) = ? LIMIT 1",
            (rel_candidate,),
        ).fetchone()
        if row:
            return int(row[0])
    cur = conn.execute(
        """
        SELECT id, pdf_relpath FROM papers
        WHERE pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''
        """
    )
    for row in cur.fetchall():
        rel = str(row[1]).strip()
        if not rel:
            continue
        try:
            p = (root / rel).resolve()
        except OSError:
            continue
        if p == cand:
            return int(row[0])
    return None


def paper_chunk_count(conn: sqlite3.Connection, paper_id: int) -> int:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM paper_chunks WHERE paper_id = ?", (int(paper_id),)
    ).fetchone()
    return int(row[0]) if row else 0


def update_paper_pdf_metadata(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    pdf_relpath: Optional[str] = None,
    pub_version: Optional[str] = None,
    pdf_fetched_at: Optional[str] = None,
    commit: bool = True,
) -> bool:
    """Refresh PDF location + version label after ``update-pdf``."""
    ensure_schema(conn)
    sets: List[str] = []
    vals: List[Any] = []
    if pdf_relpath is not None:
        sets.append("pdf_relpath = ?")
        vals.append(pdf_relpath)
    if pub_version is not None:
        sets.append("pub_version = ?")
        vals.append(pub_version)
    if pdf_fetched_at is not None:
        sets.append("pdf_fetched_at = ?")
        vals.append(pdf_fetched_at)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(_now_iso())
    vals.append(int(paper_id))
    cur = conn.execute(
        f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", vals
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def update_paper_source_metadata(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    source_text_relpath: Optional[str] = None,
    source_kind: Optional[str] = None,
    source_backend: Optional[str] = None,
    arxiv_source_relpath: Optional[str] = None,
    source_fetched_at: Optional[str] = None,
    clear_source_text_relpath: bool = False,
    clear_arxiv_source_relpath: bool = False,
    commit: bool = True,
) -> bool:
    """Refresh source-text fields after ``fetch-source``."""
    ensure_schema(conn)
    sets: List[str] = []
    vals: List[Any] = []
    if clear_source_text_relpath:
        sets.append("source_text_relpath = NULL")
    elif source_text_relpath is not None:
        sets.append("source_text_relpath = ?")
        vals.append(source_text_relpath)
    if source_kind is not None:
        sets.append("source_kind = ?")
        vals.append(source_kind)
    if source_backend is not None:
        sets.append("source_backend = ?")
        vals.append(source_backend)
    if clear_arxiv_source_relpath:
        sets.append("arxiv_source_relpath = NULL")
    elif arxiv_source_relpath is not None:
        sets.append("arxiv_source_relpath = ?")
        vals.append(arxiv_source_relpath)
    if source_fetched_at is not None:
        sets.append("source_fetched_at = ?")
        vals.append(source_fetched_at)
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals.append(_now_iso())
    vals.append(int(paper_id))
    cur = conn.execute(
        f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", vals
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def get_paper_row(conn: sqlite3.Connection, paper_id: int) -> Optional[Dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM papers WHERE id = ?", (int(paper_id),)
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_paper_ids_with_arxiv(conn: sqlite3.Connection) -> List[int]:
    """Papers with a non-empty ``arxiv_id`` (candidates for ar5iv / e-print fetch)."""
    ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT id FROM papers
        WHERE arxiv_id IS NOT NULL AND TRIM(arxiv_id) != ''
        ORDER BY id
        """
    )
    return [int(r[0]) for r in cur.fetchall()]


def library_pdf_relpath_from_abs(pdf_abs: str) -> str:
    """Relative path under ``get_data_dir()`` (POSIX separators); falls back to abs path."""
    data_root = get_data_dir().resolve()
    p = Path(pdf_abs).resolve()
    try:
        return str(p.relative_to(data_root)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def insert_extraction_run(
    conn: sqlite3.Connection,
    *,
    topic: str,
    schema_json: str,
    result_json: str,
) -> int:
    ensure_schema(conn)
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO extraction_runs (topic, schema_json, result_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (topic, schema_json, result_json, now),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fts_match_terms(
    raw: str, *, max_terms: int = 24, joiner: str = " AND "
) -> str:
    """Build token query for FTS5 (avoids reserved tokens and punctuation issues)."""
    reserved = frozenset(
        {
            "and",
            "or",
            "not",
            "near",
            "ne",
            "a",
            "an",
            "the",
            "is",
            "are",
            "to",
            "of",
            "in",
            "on",
            "for",
        }
    )
    terms = re.findall(r"[\w\u4e00-\u9fff]+", raw, flags=re.UNICODE)
    seen: set[str] = set()
    parts: List[str] = []
    for t in terms:
        if len(t) < 2:
            continue
        tl = t.lower()
        if tl in reserved or tl in seen:
            continue
        seen.add(tl)
        parts.append(t.replace('"', '""'))
        if len(parts) >= max_terms:
            break
    if not parts:
        return ""
    return joiner.join(parts)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for k in ("authors_json", "categories_json", "matched_keywords_json"):
        if k in d and isinstance(d[k], str):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k.replace("_json", "")] = []
        key = k.replace("_json", "")
        if key in d and k in d:
            del d[k]
    return d


def _arxiv_id_variant_list(arxiv_id: str) -> List[str]:
    aid = arxiv_id.strip()
    if aid.lower().startswith("arxiv:"):
        aid = aid[6:].strip()
    base = re.sub(r"v\d+$", "", aid, flags=re.IGNORECASE)
    out: List[str] = []
    seen: Set[str] = set()
    for v in (aid, base, f"{base}v1", base.upper()):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def fetch_paper_dicts_by_bibcode(
    conn: sqlite3.Connection, bibcode: Optional[str]
) -> List[Dict[str, Any]]:
    if not bibcode or not str(bibcode).strip():
        return []
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM papers WHERE bibcode = ?",
        (str(bibcode).strip(),),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def fetch_paper_dicts_by_arxiv_id(
    conn: sqlite3.Connection, arxiv_id: Optional[str]
) -> List[Dict[str, Any]]:
    if not arxiv_id or not str(arxiv_id).strip():
        return []
    ensure_schema(conn)
    out: List[Dict[str, Any]] = []
    seen_pid: Set[int] = set()
    for vid in _arxiv_id_variant_list(str(arxiv_id).strip()):
        for r in conn.execute("SELECT * FROM papers WHERE arxiv_id = ?", (vid,)).fetchall():
            d = _row_to_dict(r)
            pid = d.get("id")
            if pid is not None:
                if int(pid) in seen_pid:
                    continue
                seen_pid.add(int(pid))
            out.append(d)
    return out


def fetch_paper_dicts_by_author_year(
    conn: sqlite3.Connection,
    first_author: Optional[str],
    year: Optional[str],
    *,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    if not first_author or not year:
        return []
    ensure_schema(conn)
    pat = f"%{str(first_author).strip()}%"
    ypat = f"{str(year).strip()}%"
    rows = conn.execute(
        """
        SELECT * FROM papers
        WHERE authors_json LIKE ? AND published LIKE ?
        LIMIT ?
        """,
        (pat, ypat, int(limit)),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_local_pdf_path(
    conn: sqlite3.Connection,
    *,
    arxiv_id: Optional[str] = None,
    bibcode: Optional[str] = None,
) -> Optional[str]:
    """Return absolute path to PDF if ``papers.pdf_relpath`` exists under data dir."""
    ensure_schema(conn)
    root = get_data_dir().resolve()
    rel: Optional[str] = None

    variants: List[str] = []
    if arxiv_id:
        aid = arxiv_id.strip()
        if aid.lower().startswith("arxiv:"):
            aid = aid[6:].strip()
        base = re.sub(r"v\d+$", "", aid, flags=re.IGNORECASE)
        seen: Set[str] = set()
        for v in (aid, base, f"{base}v1"):
            if v and v not in seen:
                seen.add(v)
                variants.append(v)

    for v in variants:
        cur = conn.execute(
            """
            SELECT pdf_relpath FROM papers
            WHERE arxiv_id = ? AND pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''
            """,
            (v,),
        )
        hit = cur.fetchone()
        if hit and hit[0]:
            rel = str(hit[0]).strip()
            break

    if not rel and bibcode:
        bc = bibcode.strip()
        cur = conn.execute(
            """
            SELECT pdf_relpath FROM papers
            WHERE bibcode = ? AND pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''
            """,
            (bc,),
        )
        hit = cur.fetchone()
        if hit and hit[0]:
            rel = str(hit[0]).strip()

    if not rel:
        return None
    cand = (root / rel).resolve()
    if cand.is_file():
        return str(cand)
    return None


def upsert_paper(
    conn: sqlite3.Connection,
    *,
    arxiv_id: Optional[str],
    title: str,
    abstract: str = "",
    authors: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    matched_keywords: Optional[List[str]] = None,
    published: Optional[str] = None,
    bibcode: Optional[str] = None,
    source: str = "manual",
    pdf_relpath: Optional[str] = None,
    commit: bool = True,
) -> int:
    """Insert or update by arxiv_id and/or bibcode (at least one required)."""
    ensure_schema(conn)
    if not arxiv_id and not bibcode:
        raise ValueError("arxiv_id or bibcode is required for upsert")
    authors = authors or []
    categories = categories or []
    matched_keywords = matched_keywords or []
    now = _now_iso()
    authors_json = json.dumps(authors, ensure_ascii=False)
    categories_json = json.dumps(categories, ensure_ascii=False)
    matched_json = json.dumps(matched_keywords, ensure_ascii=False)

    row = None
    if arxiv_id:
        cur = conn.execute("SELECT id FROM papers WHERE arxiv_id = ?", (arxiv_id,))
        row = cur.fetchone()
    if row is None and bibcode:
        cur = conn.execute("SELECT id FROM papers WHERE bibcode = ?", (bibcode,))
        row = cur.fetchone()

    if row:
        pid = int(row[0])
        conn.execute(
            """
            UPDATE papers SET
                arxiv_id = COALESCE(?, arxiv_id),
                bibcode = COALESCE(?, bibcode),
                title = ?,
                abstract = ?,
                authors_json = ?,
                categories_json = ?,
                matched_keywords_json = ?,
                published = COALESCE(?, published),
                source = ?,
                pdf_relpath = COALESCE(?, pdf_relpath),
                updated_at = ?
            WHERE id = ?
            """,
            (
                arxiv_id,
                bibcode,
                title,
                abstract or "",
                authors_json,
                categories_json,
                matched_json,
                published,
                source,
                pdf_relpath,
                now,
                pid,
            ),
        )
        conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (pid,))
    else:
        conn.execute(
            """
            INSERT INTO papers (
                arxiv_id, bibcode, title, abstract,
                authors_json, categories_json, matched_keywords_json,
                published, source, pdf_relpath, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                arxiv_id,
                bibcode,
                title,
                abstract or "",
                authors_json,
                categories_json,
                matched_json,
                published,
                source,
                pdf_relpath,
                now,
                now,
            ),
        )
        pid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?, ?, ?)",
        (pid, title, abstract or ""),
    )
    if commit:
        conn.commit()
    return pid


def upsert_from_arxiv_cache_entry(
    conn: sqlite3.Connection,
    entry: Dict[str, Any],
    *,
    commit: bool = True,
) -> int:
    """Map arxiv_keywords cache / new_entries dict into upsert."""
    return upsert_paper(
        conn,
        commit=commit,
        arxiv_id=entry.get("id") or entry.get("arxiv_id"),
        title=entry.get("title") or "",
        abstract=entry.get("summary") or entry.get("abstract") or "",
        authors=entry.get("authors") or [],
        categories=entry.get("categories") or [],
        matched_keywords=entry.get("matched_kw") or entry.get("matched_keywords") or [],
        published=entry.get("published"),
        bibcode=entry.get("bibcode"),
        source=entry.get("source") or "arxiv_keyword_scan",
        pdf_relpath=entry.get("pdf_relpath"),
    )


def search_fts(conn: sqlite3.Connection, query: str, limit: int = 20) -> List[Dict[str, Any]]:
    ensure_schema(conn)
    q = query.strip()
    if not q:
        return []
    sql = """
        SELECT p.* FROM papers p
        INNER JOIN papers_fts ON p.id = papers_fts.paper_id
        WHERE papers_fts MATCH ?
        ORDER BY bm25(papers_fts)
        LIMIT ?
    """
    token = q.replace('"', '""')
    try:
        rows = conn.execute(sql, (token, limit)).fetchall()
    except sqlite3.OperationalError:
        parts = [p for p in q.split() if p]
        if not parts:
            return []
        safe = " AND ".join(p.replace('"', '""') for p in parts)
        rows = conn.execute(sql, (safe, limit)).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        out.append(d)
    return out


def _norm_arxiv_for_dedupe(a: Optional[str]) -> Optional[str]:
    from research_library.library.reference_parse import strip_arxiv_version

    s = (a or "").strip()
    if not s:
        return None
    return strip_arxiv_version(s)


def _norm_pdf_relpath_for_dedupe(p: Optional[str]) -> Optional[str]:
    s = (p or "").strip()
    if not s:
        return None
    return Path(s).as_posix().casefold()


def _nonempty_str(s: Optional[str]) -> bool:
    return bool((s or "").strip())


def _authors_json_nonempty(auth: Optional[str]) -> bool:
    t = (auth or "").strip()
    if not t or t == "[]":
        return False
    return True


def _paper_rank_key(conn: sqlite3.Connection, pid: int) -> Tuple[int, int, int, int]:
    row = conn.execute(
        "SELECT id, bibcode, pdf_relpath FROM papers WHERE id = ?", (pid,)
    ).fetchone()
    if not row:
        return (0, 0, 0, 0)
    ch = int(
        conn.execute(
            "SELECT COUNT(*) FROM paper_chunks WHERE paper_id = ?", (pid,)
        ).fetchone()[0]
    )
    bib = 1 if _nonempty_str(row["bibcode"]) else 0
    pdf = 1 if _nonempty_str(row["pdf_relpath"]) else 0
    return (ch, pdf, bib, -pid)


class _PaperUF:
    __slots__ = ("p",)

    def __init__(self) -> None:
        self.p: Dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self.p:
            self.p[x] = x

    def find(self, x: int) -> int:
        self.add(x)
        if self.p[x] != x:
            self.p[x] = self.find(self.p[x])
        return self.p[x]

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _dedupe_union_pairs(conn: sqlite3.Connection) -> List[Tuple[int, int]]:
    pairs: List[Tuple[int, int]] = []
    seen: Set[Tuple[int, int]] = set()

    def add_chain(ids: List[int]) -> None:
        u = sorted({int(x) for x in ids})
        if len(u) < 2:
            return
        for i in range(len(u) - 1):
            a, b = u[i], u[i + 1]
            key = (a, b) if a < b else (b, a)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((u[i], u[i + 1]))

    for (bc,) in conn.execute(
        """
        SELECT bibcode FROM papers
        WHERE bibcode IS NOT NULL AND TRIM(bibcode) != ''
        GROUP BY UPPER(TRIM(bibcode)) HAVING COUNT(*) > 1
        """
    ):
        rows = conn.execute(
            """
            SELECT id FROM papers
            WHERE UPPER(TRIM(bibcode)) = UPPER(TRIM(?)) ORDER BY id
            """,
            (bc,),
        ).fetchall()
        add_chain([int(r[0]) for r in rows])

    by_ax: Dict[str, List[int]] = {}
    for r in conn.execute(
        """
        SELECT id, arxiv_id FROM papers
        WHERE arxiv_id IS NOT NULL AND TRIM(arxiv_id) != ''
        """
    ):
        ax = _norm_arxiv_for_dedupe(r["arxiv_id"])
        if not ax:
            continue
        by_ax.setdefault(ax, []).append(int(r["id"]))
    for ids in by_ax.values():
        add_chain(ids)

    by_pdf: Dict[str, List[int]] = {}
    for r in conn.execute(
        """
        SELECT id, pdf_relpath FROM papers
        WHERE pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''
        """
    ):
        k = _norm_pdf_relpath_for_dedupe(r["pdf_relpath"])
        if not k:
            continue
        by_pdf.setdefault(k, []).append(int(r["id"]))
    for ids in by_pdf.values():
        add_chain(ids)

    return pairs


def _merge_dup_into_keep(
    conn: sqlite3.Connection,
    keep_id: int,
    dup_id: int,
    *,
    chroma_delete: bool,
    errors: List[str],
) -> None:
    if keep_id == dup_id:
        return
    now = _now_iso()

    if chroma_delete:
        try:
            from research_library.library.semantic import (
                delete_chroma_for_paper,
                get_semantic_collection,
            )

            delete_chroma_for_paper(get_semantic_collection(), dup_id)
        except Exception as e:
            errors.append(f"chroma_delete paper_id={dup_id}: {e}")

    krow = conn.execute("SELECT * FROM papers WHERE id = ?", (keep_id,)).fetchone()
    drow = conn.execute("SELECT * FROM papers WHERE id = ?", (dup_id,)).fetchone()
    if not krow or not drow:
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO paper_references(from_paper_id, ref_bibcode, created_at)
        SELECT ?, ref_bibcode, created_at FROM paper_references WHERE from_paper_id = ?
        """,
        (keep_id, dup_id),
    )
    conn.execute(
        "DELETE FROM paper_references WHERE from_paper_id = ?", (dup_id,)
    )

    for tr in conn.execute(
        "SELECT id, tag_key FROM paper_tags WHERE paper_id = ?", (dup_id,)
    ):
        tid, tkey = int(tr["id"]), tr["tag_key"]
        hit = conn.execute(
            "SELECT 1 FROM paper_tags WHERE paper_id = ? AND tag_key = ?",
            (keep_id, tkey),
        ).fetchone()
        if hit:
            conn.execute("DELETE FROM paper_tags WHERE id = ?", (tid,))
        else:
            conn.execute(
                "UPDATE paper_tags SET paper_id = ? WHERE id = ?", (keep_id, tid)
            )

    nk = paper_chunk_count(conn, keep_id)
    nd = paper_chunk_count(conn, dup_id)
    if nk > 0 and nd > 0:
        delete_chunks_for_paper(conn, dup_id)
    elif nd > 0:
        conn.execute(
            "UPDATE paper_chunks SET paper_id = ? WHERE paper_id = ?",
            (keep_id, dup_id),
        )
        conn.execute(
            "UPDATE paper_chunks_fts SET paper_id = ? WHERE paper_id = ?",
            (keep_id, dup_id),
        )

    conn.execute(
        "UPDATE papers SET arxiv_id = NULL, bibcode = NULL WHERE id = ?",
        (dup_id,),
    )

    new_ax = (
        drow["arxiv_id"]
        if not _nonempty_str(krow["arxiv_id"])
        else krow["arxiv_id"]
    )
    new_bc = (
        drow["bibcode"]
        if not _nonempty_str(krow["bibcode"])
        else krow["bibcode"]
    )
    new_pdf = (
        drow["pdf_relpath"]
        if not _nonempty_str(krow["pdf_relpath"])
        else krow["pdf_relpath"]
    )
    new_title = drow["title"] if not _nonempty_str(krow["title"]) else krow["title"]
    new_abs = (
        drow["abstract"] if not _nonempty_str(krow["abstract"]) else krow["abstract"]
    )
    new_auth = (
        drow["authors_json"]
        if not _authors_json_nonempty(krow["authors_json"])
        else krow["authors_json"]
    )
    new_pub = (
        krow["published"]
        if _nonempty_str(krow["published"])
        else drow["published"]
    )
    new_src = (
        krow["source"] if _nonempty_str(krow["source"]) else drow["source"]
    )

    conn.execute(
        """
        UPDATE papers SET
            arxiv_id = ?, bibcode = ?, pdf_relpath = ?, title = ?, abstract = ?,
            authors_json = ?, categories_json = ?, matched_keywords_json = ?,
            published = ?, source = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            new_ax,
            new_bc,
            new_pdf,
            new_title,
            new_abs,
            new_auth,
            drow["categories_json"]
            if (
                not (krow["categories_json"] or "").strip()
                or (krow["categories_json"] or "").strip() == "[]"
            )
            else krow["categories_json"],
            drow["matched_keywords_json"]
            if (
                not (krow["matched_keywords_json"] or "").strip()
                or (krow["matched_keywords_json"] or "").strip() == "[]"
            )
            else krow["matched_keywords_json"],
            new_pub,
            new_src,
            now,
            keep_id,
        ),
    )

    conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (dup_id,))
    conn.execute("DELETE FROM papers WHERE id = ?", (dup_id,))

    prow = conn.execute(
        "SELECT title, abstract FROM papers WHERE id = ?", (keep_id,)
    ).fetchone()
    conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (keep_id,))
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?,?,?)",
        (keep_id, (prow["title"] or "") if prow else "", (prow["abstract"] or "") if prow else ""),
    )


def dedupe_papers(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    chroma_delete: bool = False,
) -> Dict[str, Any]:
    """Merge rows that share bibcode (case-insensitive), normalized arXiv id, or same ``pdf_relpath``.

    Keeps one row per equivalence class: most chunk rows, then PDF path, then bibcode, then lowest id.
    """
    ensure_schema(conn)
    errors: List[str] = []
    pairs = _dedupe_union_pairs(conn)
    uf = _PaperUF()
    for a, b in pairs:
        uf.union(a, b)

    clusters: Dict[int, List[int]] = {}
    for x in uf.p:
        r = uf.find(x)
        clusters.setdefault(r, []).append(x)

    plans: List[Dict[str, Any]] = []
    removed: List[int] = []
    for _root, ids in sorted(clusters.items(), key=lambda x: min(x[1])):
        u = sorted(set(ids))
        if len(u) < 2:
            continue
        keep = max(u, key=lambda i: _paper_rank_key(conn, i))
        for dup in sorted((x for x in u if x != keep), reverse=True):
            plans.append({"keep_id": keep, "remove_id": dup})
            if not dry_run:
                _merge_dup_into_keep(
                    conn,
                    keep,
                    dup,
                    chroma_delete=chroma_delete,
                    errors=errors,
                )
                removed.append(dup)

    n_clusters = sum(1 for ids in clusters.values() if len(set(ids)) > 1)
    return {
        "dry_run": dry_run,
        "clusters_with_duplicates": n_clusters,
        "merge_operations": len(plans),
        "papers_removed": len(plans) if dry_run else len(removed),
        "merges": plans,
        "errors": errors,
    }


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    ensure_schema(conn)
    n = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    last = conn.execute(
        "SELECT MAX(updated_at) FROM papers"
    ).fetchone()[0]
    return {"papers": int(n), "last_updated": last, "db_path": str(db_path())}


def update_paper_bibcode(conn: sqlite3.Connection, paper_id: int, bibcode: str) -> bool:
    """Set bibcode for a row (e.g. resolved from arXiv). Does not commit."""
    ensure_schema(conn)
    if not bibcode:
        return False
    now = _now_iso()
    cur = conn.execute(
        "UPDATE papers SET bibcode = ?, updated_at = ? WHERE id = ?",
        (bibcode.strip(), now, paper_id),
    )
    return cur.rowcount > 0


def list_paper_reference_edges(conn: sqlite3.Connection, from_paper_id: int) -> List[Dict[str, Any]]:
    """Rows from ``paper_references`` with optional join to cited paper (library + PDF)."""
    ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT pr.ref_bibcode, p.id, p.title,
               CASE WHEN p.pdf_relpath IS NOT NULL AND TRIM(p.pdf_relpath) != '' THEN 1 ELSE 0 END AS has_pdf
        FROM paper_references pr
        LEFT JOIN papers p ON p.bibcode = pr.ref_bibcode
        WHERE pr.from_paper_id = ?
        ORDER BY pr.ref_bibcode
        """,
        (int(from_paper_id),),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        bc, pid, title, has_pdf = row[0], row[1], row[2], int(row[3] or 0)
        out.append(
            {
                "ref_bibcode": str(bc).strip(),
                "to_paper_id": int(pid) if pid is not None else None,
                "title": str(title or "").strip(),
                "has_local_pdf": bool(has_pdf),
            }
        )
    return out


def replace_paper_references(
    conn: sqlite3.Connection,
    from_paper_id: int,
    ref_bibcodes: List[str],
) -> int:
    """Replace ADS reference list for one paper. Caller should commit the transaction."""
    ensure_schema(conn)
    now = _now_iso()
    conn.execute("DELETE FROM paper_references WHERE from_paper_id = ?", (from_paper_id,))
    seen: set[str] = set()
    n = 0
    for bc in ref_bibcodes:
        if not bc or not isinstance(bc, str):
            continue
        b = bc.strip()
        if not b or b in seen:
            continue
        seen.add(b)
        conn.execute(
            "INSERT INTO paper_references(from_paper_id, ref_bibcode, created_at) VALUES (?,?,?)",
            (from_paper_id, b, now),
        )
        n += 1
    return n


def import_cache_json(conn: sqlite3.Connection, entries: Dict[str, Dict[str, Any]]) -> int:
    """Bulk import from arxiv_cache entries dict (single transaction)."""
    ensure_schema(conn)
    n = 0
    for _, rec in entries.items():
        try:
            upsert_from_arxiv_cache_entry(conn, rec, commit=False)
            n += 1
        except Exception:
            continue
    conn.commit()
    return n
