"""PDF chunking, embedding index (Chroma), semantic search, and related papers."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from research_library.analysis.embeddings import get_embedding_client
from research_library.analysis.embeddings.base import EmbeddingClient
from research_library.analysis.pdf import extract_pdf_text
from research_library.config import (
    effective_semantic_backend,
    get_chroma_semantic_dir,
    get_data_dir,
    load_env,
)
from research_library.library import db as library_db

_CHROMA_COLLECTION = "paper_chunks_semantic"

_CHROMA_CLIENT_CACHE: Dict[str, Any] = {}
_CHROMA_COLLECTION_CACHE: Dict[str, Any] = {}


def _is_fts_backend(semantic_backend: str | None = None) -> bool:
    return effective_semantic_backend(semantic_backend) == "fts"


def _require_chromadb():  # noqa: ANN201
    try:
        import chromadb  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "chromadb is required for semantic index/search. "
            'Install: pip install -e ".[semantic]"'
        ) from e
    return __import__("chromadb")


def get_semantic_collection():
    """Shared persistent Chroma collection (cosine on embeddings).

    Lazily caches both the ``PersistentClient`` and the collection handle per data
    directory so that repeated retrieval / indexing calls do not pay the
    connection / open overhead each time.
    """
    path = str(get_chroma_semantic_dir())
    cached = _CHROMA_COLLECTION_CACHE.get(path)
    if cached is not None:
        return cached
    chromadb = _require_chromadb()
    client = _CHROMA_CLIENT_CACHE.get(path)
    if client is None:
        client = chromadb.PersistentClient(path=path)
        _CHROMA_CLIENT_CACHE[path] = client
    coll = client.get_or_create_collection(
        name=_CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )
    _CHROMA_COLLECTION_CACHE[path] = coll
    return coll


def _reset_semantic_caches() -> None:
    """Forget cached Chroma client + collection (mostly for tests)."""
    _CHROMA_CLIENT_CACHE.clear()
    _CHROMA_COLLECTION_CACHE.clear()


def delete_chroma_for_paper(collection: Any, paper_id: int) -> None:
    collection.delete(where={"paper_id": paper_id})


def chunk_text(
    text: str,
    *,
    size: int | None = None,
    overlap: int | None = None,
) -> List[Tuple[int, int, str]]:
    """Return list of (char_start, char_end, chunk_text).

    Sentence/paragraph aware: when the next window would split inside a
    sentence we look backwards a small window for the nearest paragraph break,
    line break, or sentence terminator (``. ! ?`` plus CJK ``。``/``！``/``？``).
    Set ``RESEARCH_SEMANTIC_CHUNK_BOUNDARY=0`` to fall back to the naive sliding
    window.
    """
    load_env()
    sz = size if size is not None else int(os.environ.get("RESEARCH_SEMANTIC_CHUNK_SIZE") or "1200")
    ov = overlap if overlap is not None else int(os.environ.get("RESEARCH_SEMANTIC_CHUNK_OVERLAP") or "200")
    sz = max(200, sz)
    ov = max(0, min(ov, sz // 2))
    t = text.strip()
    if not t:
        return []
    boundary_aware = (os.environ.get("RESEARCH_SEMANTIC_CHUNK_BOUNDARY") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    out: List[Tuple[int, int, str]] = []
    start = 0
    n = len(t)
    look_back = min(max(sz // 4, 80), 400)
    while start < n:
        end = min(n, start + sz)
        if boundary_aware and end < n:
            window = t[max(end - look_back, start + 1):end]
            cut_rel = -1
            for marker in ("\n\n", "\n"):
                idx = window.rfind(marker)
                if idx >= 0:
                    cut_rel = idx + len(marker)
                    break
            if cut_rel < 0:
                for marker in (". ", "。", "！", "？", "! ", "? "):
                    idx = window.rfind(marker)
                    if idx >= 0 and idx + len(marker) > look_back // 2:
                        cut_rel = idx + len(marker)
                        break
            if cut_rel > 0:
                end = max(end - look_back, start + 1) + cut_rel
        piece = t[start:end]
        if piece.strip():
            out.append((start, end, piece))
        if end >= n:
            break
        nxt = end - ov
        if nxt <= start:
            nxt = start + max(1, sz // 4)
        start = min(nxt, n)
    return out


def _pdf_abs_path_for_paper(conn: Any, paper_id: int) -> Optional[str]:
    library_db.ensure_schema(conn)
    row = conn.execute(
        "SELECT pdf_relpath FROM papers WHERE id = ?", (paper_id,)
    ).fetchone()
    if not row or not row[0]:
        return None
    root = get_data_dir().resolve()
    p = (root / str(row[0]).strip()).resolve()
    return str(p) if p.is_file() else None


def _auto_fetch_tex_enabled() -> bool:
    load_env()
    raw = (os.environ.get("RESEARCH_SEMANTIC_AUTO_FETCH_TEX") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _read_stored_tex_source(
    conn: Any, paper_id: int
) -> Tuple[Optional[str], str, Optional[List[Any]], Optional[str]]:
    library_db.ensure_schema(conn)
    row = conn.execute(
        "SELECT source_text_relpath, source_kind FROM papers WHERE id = ?",
        (paper_id,),
    ).fetchone()
    if row is None:
        return None, "", None, None
    src_rel = (row[0] or "").strip() if row[0] else ""
    src_kind = (row[1] or "").strip() if row[1] else ""
    if src_kind != "tex" or not src_rel:
        return None, "", None, None
    from research_library.library.ar5iv_source import read_sections_for

    root = get_data_dir().resolve()
    src_path = (root / src_rel).resolve()
    if not src_path.is_file():
        return None, "", None, None
    try:
        text = src_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if not text.strip():
        return None, "", None, None
    return text, "tex", read_sections_for(paper_id), str(src_path)


def _read_pdf_source(
    conn: Any, paper_id: int
) -> Tuple[Optional[str], str, Optional[List[Any]], Optional[str]]:
    library_db.ensure_schema(conn)
    row = conn.execute(
        "SELECT pdf_relpath FROM papers WHERE id = ?",
        (paper_id,),
    ).fetchone()
    if row is None:
        return None, "", None, None
    pdf_rel = (row[0] or "").strip() if row[0] else ""
    if not pdf_rel:
        return None, "", None, None
    root = get_data_dir().resolve()
    pdf_path = (root / pdf_rel).resolve()
    if not pdf_path.is_file():
        return None, "", None, None
    pdf_text = extract_pdf_text(str(pdf_path))
    if not (pdf_text or "").strip():
        return None, "", None, None
    return pdf_text, "pdf", None, str(pdf_path)


def resolve_text_source_for_paper(
    conn: Any,
    paper_id: int,
    *,
    try_fetch_tex: Optional[bool] = None,
) -> Tuple[Optional[str], str, Optional[List[Any]], Optional[str], Dict[str, Any]]:
    """Resolve prose for embedding: cached TeX → fetch TeX chain → PDF.

    TeX fetch uses ``ar5iv → local tarball backend`` (see :mod:`tex_to_text`).
    Returns ``(text, source_kind, sections, source_path, meta)``.
    """
    meta: Dict[str, Any] = {}
    text, kind, sections, path = _read_stored_tex_source(conn, paper_id)
    if text and kind:
        meta["source_resolution"] = "cached_tex"
        return text, kind, sections, path, meta

    do_fetch = _auto_fetch_tex_enabled() if try_fetch_tex is None else bool(try_fetch_tex)
    if do_fetch:
        paper = library_db.get_paper_row(conn, paper_id)
        arxiv_id = (paper.get("arxiv_id") or "").strip() if paper else ""
        if arxiv_id:
            from research_library.library.tex_to_text import fetch_source_for_paper

            meta["tex_fetch_attempted"] = True
            meta["tex_fetch"] = fetch_source_for_paper(conn, paper_id, force=False)
            text, kind, sections, path = _read_stored_tex_source(conn, paper_id)
            if text and kind:
                meta["source_resolution"] = "fetched_tex"
                return text, kind, sections, path, meta

    text, kind, sections, path = _read_pdf_source(conn, paper_id)
    if text and kind:
        meta["source_resolution"] = "pdf"
        return text, kind, sections, path, meta

    return None, "", None, None, meta


def _text_source_for_paper(
    conn: Any, paper_id: int
) -> Tuple[Optional[str], str, Optional[List[Any]], Optional[str]]:
    """Resolve the indexing source for ``paper_id`` (with TeX auto-fetch)."""
    text, kind, sections, path, _meta = resolve_text_source_for_paper(conn, paper_id)
    return text, kind, sections, path


def list_paper_ids_with_indexable_source(conn: Any) -> List[int]:
    """Papers indexable via cached/fetchable TeX and/or a local PDF."""
    library_db.ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT id FROM papers
        WHERE (source_text_relpath IS NOT NULL AND TRIM(source_text_relpath) != '')
           OR (pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != '')
           OR (arxiv_id IS NOT NULL AND TRIM(arxiv_id) != '')
        ORDER BY id
        """
    )
    return [int(r[0]) for r in cur.fetchall()]


def _section_for_chunk(sections: Optional[List[Any]], char_start: int) -> Optional[str]:
    """Return the most recent section title at or before ``char_start``."""
    if not sections:
        return None
    chosen: Optional[str] = None
    for sec in sections:
        sec_start = getattr(sec, "char_start", None)
        if sec_start is None and isinstance(sec, dict):
            sec_start = sec.get("char_start")
        title = getattr(sec, "title", None)
        if title is None and isinstance(sec, dict):
            title = sec.get("title")
        if sec_start is None or title is None:
            continue
        try:
            if int(sec_start) <= int(char_start):
                chosen = str(title)
            else:
                break
        except (TypeError, ValueError):
            continue
    return chosen


def _embed_batch_size() -> int:
    load_env()
    raw = (os.environ.get("RESEARCH_SEMANTIC_EMBED_BATCH") or "").strip()
    try:
        return max(1, int(raw)) if raw else 16
    except ValueError:
        return 16


def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:32]


def _paper_row(conn: Any, paper_id: int) -> Optional[Dict[str, Any]]:
    library_db.ensure_schema(conn)
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    return dict(row) if row else None


def index_paper(
    conn: Any,
    paper_id: int,
    *,
    embed_client: EmbeddingClient | None = None,
    force: bool = False,
    collection: Any | None = None,
    semantic_backend: str | None = None,
) -> Dict[str, Any]:
    """Chunk a paper's source text (TeX-derived if available, else PDF) into ``paper_chunks``.

    When the semantic backend is ``vector`` the chunks are also embedded and
    written to Chroma with ``source_kind`` + ``section`` (or ``page``)
    metadata.
    """
    load_env()
    fts = _is_fts_backend(semantic_backend)
    if not fts:
        collection = collection or get_semantic_collection()
        ec = embed_client or get_embedding_client()
    else:
        collection = None
        ec = None

    if not force:
        cur = conn.execute(
            "SELECT COUNT(*) FROM paper_chunks WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        if cur and int(cur[0]) > 0:
            return {"ok": True, "paper_id": paper_id, "skipped": True, "chunks": int(cur[0])}

    text, source_kind, sections, source_path, source_meta = resolve_text_source_for_paper(
        conn, paper_id
    )
    if not text or not source_kind:
        err = "no_source_text"
        if source_meta.get("tex_fetch_attempted"):
            err = "tex_and_pdf_unavailable"
        return {"ok": False, "paper_id": paper_id, "error": err, **source_meta}

    triples = chunk_text(text)
    if not triples:
        return {"ok": False, "paper_id": paper_id, "error": "no_chunks"}

    if not fts:
        assert collection is not None
        delete_chroma_for_paper(collection, paper_id)
    library_db.delete_chunks_for_paper(conn, paper_id)
    conn.commit()

    chunk_rows: List[Dict[str, Any]] = []
    for ord_, (cs, ce, piece) in enumerate(triples):
        chunk_rows.append(
            {
                "chunk_ord": ord_,
                "char_start": cs,
                "char_end": ce,
                "text": piece[:65000],
                "text_hash": _hash_text(piece),
            }
        )

    chunk_ids = library_db.replace_paper_chunks(conn, paper_id, chunk_rows)
    conn.commit()

    if fts:
        return {
            "ok": True,
            "paper_id": paper_id,
            "chunks": len(chunk_ids),
            "source_path": source_path,
            "source_kind": source_kind,
            "source_resolution": source_meta.get("source_resolution"),
            "backend": "fts",
        }

    texts_for_emb = [chunk_rows[i]["text"] for i in range(len(chunk_rows))]
    batch = _embed_batch_size()
    all_vec: List[List[float]] = []
    for i in range(0, len(texts_for_emb), batch):
        batch_texts = texts_for_emb[i : i + batch]
        all_vec.extend(ec.embed_texts(batch_texts, for_query=False))

    chroma_ids = [f"c{rid}" for rid in chunk_ids]
    paper_meta = _paper_row(conn, paper_id) or {}
    bib_meta = str(paper_meta.get("bibcode") or "")
    arx_meta = str(paper_meta.get("arxiv_id") or "")
    src_backend = str(paper_meta.get("source_backend") or "")
    page_breaks: List[int] = []
    if source_kind == "pdf":
        page_breaks = [i for i, ch in enumerate(text) if ch == "\f"]
    metadatas = []
    for j, cid in enumerate(chunk_ids):
        cs = int(chunk_rows[j]["char_start"])
        md = {
            "paper_id": paper_id,
            "chunk_id": cid,
            "chunk_ord": chunk_rows[j]["chunk_ord"],
            "char_start": cs,
            "source_kind": source_kind,
        }
        if src_backend:
            md["source_backend"] = src_backend
        if source_kind == "pdf" and page_breaks:
            page_no = 0
            for pb in page_breaks:
                if pb <= cs:
                    page_no += 1
                else:
                    break
            page_no += 1
            md["page"] = page_no
        elif source_kind == "tex":
            sec_title = _section_for_chunk(sections, cs)
            if sec_title:
                md["section"] = sec_title[:200]
        if bib_meta:
            md["bibcode"] = bib_meta
        if arx_meta:
            md["arxiv_id"] = arx_meta
        metadatas.append(md)
    assert collection is not None and ec is not None
    collection.add(
        ids=chroma_ids,
        embeddings=all_vec,
        documents=texts_for_emb,
        metadatas=metadatas,
    )

    return {
        "ok": True,
        "paper_id": paper_id,
        "chunks": len(chunk_ids),
        "source_path": source_path,
        "source_kind": source_kind,
        "source_resolution": source_meta.get("source_resolution"),
        "embedding_dim": ec.embedding_dim if ec.embedding_dim else len(all_vec[0]),
        "backend": "vector",
    }


def index_papers(
    conn: Any,
    paper_ids: Sequence[int] | None,
    *,
    force: bool = False,
    embed_client: EmbeddingClient | None = None,
    semantic_backend: str | None = None,
) -> Dict[str, Any]:
    load_env()
    fts = _is_fts_backend(semantic_backend)
    collection = None if fts else get_semantic_collection()
    ec = None if fts else (embed_client or get_embedding_client())
    if paper_ids is not None:
        ids = list(paper_ids)
    else:
        ids = list_paper_ids_with_indexable_source(conn)
    items: List[Dict[str, Any]] = []
    errors = 0
    for pid in ids:
        try:
            r = index_paper(
                conn,
                pid,
                embed_client=ec,
                force=force,
                collection=collection,
                semantic_backend=semantic_backend,
            )
            items.append(r)
            if not r.get("ok"):
                errors += 1
        except Exception as e:
            items.append({"ok": False, "paper_id": pid, "error": str(e)})
            errors += 1
    return {
        "indexed": len(ids),
        "errors": errors,
        "items": items,
        "backend": "fts" if fts else "vector",
    }


def _rrf_fuse(*ranked_lists: List[Any], k: int = 60) -> List[Tuple[Any, float]]:
    """Reciprocal Rank Fusion: each list contributes 1/(k + rank) per item."""
    scores: Dict[Any, float] = {}
    first_seen: Dict[Any, int] = {}
    order = 0
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
            if item not in first_seen:
                first_seen[item] = order
                order += 1
    return sorted(
        scores.items(), key=lambda x: (-x[1], first_seen.get(x[0], 0))
    )


def _mmr_select(
    candidates: List[Dict[str, Any]],
    *,
    k: int,
    lambda_mult: float = 0.6,
) -> List[Dict[str, Any]]:
    """Maximal Marginal Relevance (lexical-overlap proxy) for chunk diversity.

    Avoids needing an embedding for every candidate at query time — we instead
    use a fast token-set Jaccard similarity as a proxy. This is a no-op
    diversifier (returns the top-k as-is) when fewer than ``k`` candidates exist.
    """
    if len(candidates) <= k:
        return list(candidates)

    def toks(s: str) -> set:
        return set(t for t in re.findall(r"[\w\u4e00-\u9fff]+", (s or "").lower()) if len(t) > 2)

    sims = [toks(c.get("text") or "") for c in candidates]
    selected: List[int] = []
    remaining = set(range(len(candidates)))
    while remaining and len(selected) < k:
        best_i = None
        best_score = -1e9
        for i in remaining:
            rel = float(candidates[i].get("score", 0.0))
            if not selected:
                marg = rel
            else:
                max_overlap = 0.0
                for j in selected:
                    a, b = sims[i], sims[j]
                    if not a or not b:
                        continue
                    inter = len(a & b)
                    union = len(a | b) or 1
                    max_overlap = max(max_overlap, inter / union)
                marg = lambda_mult * rel - (1 - lambda_mult) * max_overlap
            if marg > best_score:
                best_score = marg
                best_i = i
        if best_i is None:
            break
        selected.append(best_i)
        remaining.discard(best_i)
    return [candidates[i] for i in selected]


def _hybrid_chunks_for_paper(
    conn: Any,
    paper_id: int,
    question: str,
    *,
    max_chunks: int,
    fts_pool: int = 24,
    vec_pool: int = 24,
    use_vector: bool,
) -> List[Dict[str, Any]]:
    """Run FTS + vector retrieval for one paper and fuse via RRF."""
    import re as _re

    q = (question or "").strip()
    if not q:
        return []

    fts_hits = library_db.search_chunks_fts(
        conn,
        q,
        limit=fts_pool,
        paper_id=int(paper_id),
    )
    fts_ids: List[int] = []
    by_id: Dict[int, Dict[str, Any]] = {}
    for h in fts_hits:
        cid = int(h.get("chunk_id") or 0)
        if cid <= 0:
            continue
        fts_ids.append(cid)
        by_id[cid] = {
            "chunk_id": cid,
            "paper_id": int(h.get("paper_id") or paper_id),
            "chunk_ord": h.get("chunk_ord"),
            "text": str(h.get("text") or ""),
            "fts_rank": float(h.get("rank") or 0.0),
        }

    vec_ids: List[int] = []
    if use_vector:
        try:
            collection = get_semantic_collection()
            ec = get_embedding_client()
            qv = ec.embed_texts([q], for_query=True)[0]
            raw = collection.query(
                query_embeddings=[qv],
                n_results=vec_pool,
                where={"paper_id": int(paper_id)},
                include=["documents", "metadatas", "distances"],
            )
            metas = (raw.get("metadatas") or [[]])[0]
            docs = (raw.get("documents") or [[]])[0]
            dists = (raw.get("distances") or [[]])[0]
            for i, meta in enumerate(metas or []):
                if not isinstance(meta, dict):
                    continue
                cid = int(meta.get("chunk_id", 0))
                if cid <= 0:
                    continue
                vec_ids.append(cid)
                row = by_id.setdefault(
                    cid,
                    {
                        "chunk_id": cid,
                        "paper_id": int(meta.get("paper_id", paper_id)),
                        "chunk_ord": meta.get("chunk_ord"),
                        "text": str(docs[i]) if i < len(docs) else "",
                    },
                )
                if not row.get("text") and i < len(docs):
                    row["text"] = str(docs[i] or "")
                row["vec_distance"] = float(dists[i]) if i < len(dists) else 0.0
        except Exception:
            vec_ids = []

    fused = _rrf_fuse(fts_ids, vec_ids) if vec_ids else [(cid, 1.0 / (60 + i + 1)) for i, cid in enumerate(fts_ids)]
    out: List[Dict[str, Any]] = []
    for cid, score in fused:
        row = by_id.get(int(cid))
        if not row:
            continue
        row = dict(row)
        row["score"] = float(score)
        out.append(row)
        if len(out) >= max_chunks * 3:
            break
    return out


def retrieve_context_for_paper_question(
    conn: Any,
    paper_id: int,
    question: str,
    *,
    max_chars: int = 24_000,
    max_chunks: int = 16,
    semantic_backend: str | None = None,
    include_chunk_markers: Optional[bool] = None,
) -> tuple[str, str]:
    """Build LLM context from indexed chunks for one paper and a question.

    Returns ``(text, source)`` where ``source`` is ``chunks_fts``,
    ``chunks_vector``, ``chunks_hybrid`` or ``""`` if no usable index.

    Set ``RESEARCH_SEMANTIC_HYBRID=1`` (default) to run both FTS and the vector
    backend and fuse with RRF; set to 0 to keep the legacy single-backend path.
    ``RESEARCH_SEMANTIC_MMR=1`` (default) diversifies the final selection.
    Each chunk in the returned text is prefixed with ``[chunk_id=N]`` so the
    downstream LLM can quote excerpts back with stable provenance markers — set
    ``RESEARCH_GROUNDING_MARKERS=0`` to disable.
    """
    load_env()
    q = (question or "").strip()
    if not q:
        return "", ""
    library_db.ensure_schema(conn)
    if library_db.paper_chunk_count(conn, int(paper_id)) <= 0:
        return "", ""

    use_fts_only = _is_fts_backend(semantic_backend)
    hybrid_enabled = (os.environ.get("RESEARCH_SEMANTIC_HYBRID") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    mmr_enabled = (os.environ.get("RESEARCH_SEMANTIC_MMR") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    if include_chunk_markers is None:
        include_chunk_markers = (
            os.environ.get("RESEARCH_GROUNDING_MARKERS") or "1"
        ).strip().lower() not in ("0", "false", "no", "off")

    rows: List[Dict[str, Any]] = []
    tag = ""

    if use_fts_only or not hybrid_enabled:
        if use_fts_only:
            hits = library_db.search_chunks_fts(
                conn,
                q,
                limit=max(8, min(max_chunks * 3, 48)),
                paper_id=int(paper_id),
            )
            tag = "chunks_fts"
            for h in hits:
                t = str(h.get("text") or "").strip()
                if not t:
                    continue
                rows.append({
                    "chunk_id": int(h.get("chunk_id") or 0),
                    "text": t,
                    "score": -float(h.get("rank") or 0.0),
                })
        else:
            try:
                collection = get_semantic_collection()
                ec = get_embedding_client()
                qv = ec.embed_texts([q], for_query=True)[0]
                raw = collection.query(
                    query_embeddings=[qv],
                    n_results=max(6, min(max_chunks, 24)),
                    where={"paper_id": int(paper_id)},
                    include=["documents", "metadatas"],
                )
            except Exception:
                return "", ""
            docs = (raw.get("documents") or [[]])[0]
            metas = (raw.get("metadatas") or [[]])[0]
            tag = "chunks_vector"
            for i, d in enumerate(docs):
                st = str(d).strip() if d else ""
                if not st:
                    continue
                meta = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
                rows.append({
                    "chunk_id": int(meta.get("chunk_id") or 0),
                    "text": st,
                    "score": 1.0 / (i + 1),
                })
    else:
        rows = _hybrid_chunks_for_paper(
            conn,
            int(paper_id),
            q,
            max_chunks=max_chunks,
            use_vector=True,
        )
        tag = "chunks_hybrid" if rows else ""
        if not rows:
            return "", ""

    if mmr_enabled and len(rows) > max_chunks:
        rows = _mmr_select(rows, k=max_chunks)
    rows = rows[:max_chunks]

    if not rows:
        return "", ""

    sep = "\n\n---\n\n"
    out_parts: List[str] = []
    total = 0
    for r in rows:
        piece = str(r.get("text") or "").strip()
        if not piece:
            continue
        cid = int(r.get("chunk_id") or 0)
        marker = f"[chunk_id={cid}]\n" if include_chunk_markers and cid > 0 else ""
        block = marker + piece
        if total + len(block) + len(sep) > max_chars and out_parts:
            break
        out_parts.append(block)
        total += len(block) + len(sep)
        if len(out_parts) >= max_chunks:
            break
    body = sep.join(out_parts)
    if len(body) > max_chars:
        body = body[:max_chars]
    preamble = (
        "[Context: ranked excerpts from the local PDF chunk index for this paper. "
        "Each block is prefixed with [chunk_id=N] — when you quote or cite, keep "
        "the chunk_id so claims can be traced back. Do not invent material that "
        "is not present in these blocks.]\n\n"
    )
    return preamble + body, tag


def semantic_search(
    conn: Any,
    query: str,
    *,
    limit: int = 10,
    embed_client: EmbeddingClient | None = None,
    collection: Any | None = None,
    semantic_backend: str | None = None,
) -> List[Dict[str, Any]]:
    load_env()
    q = (query or "").strip()
    if not q:
        return []
    if _is_fts_backend(semantic_backend):
        n = max(1, min(int(limit), 100))
        hits = library_db.search_chunks_fts(conn, q, limit=n)
        library_db.ensure_schema(conn)
        out_fts: List[Dict[str, Any]] = []
        for h in hits:
            pid = int(h["paper_id"])
            cid = int(h["chunk_id"])
            prow = _paper_row(conn, pid)
            out_fts.append(
                {
                    "chunk_id": cid,
                    "paper_id": pid,
                    "bibcode": (prow or {}).get("bibcode"),
                    "arxiv_id": (prow or {}).get("arxiv_id"),
                    "title": (prow or {}).get("title") or "",
                    "snippet": h.get("text"),
                    "distance": float(h.get("rank") or 0.0),
                    "chroma_id": None,
                    "chunk_ord": h.get("chunk_ord"),
                    "backend": "fts",
                }
            )
        return out_fts

    collection = collection or get_semantic_collection()
    ec = embed_client or get_embedding_client()
    qv = ec.embed_texts([q], for_query=True)[0]
    n = max(1, min(int(limit), 100))
    raw = collection.query(
        query_embeddings=[qv],
        n_results=n,
        include=["documents", "distances", "metadatas"],
    )
    metas = (raw.get("metadatas") or [[]])[0]
    docs = (raw.get("documents") or [[]])[0]
    dists = (raw.get("distances") or [[]])[0]
    ids_out = (raw.get("ids") or [[]])[0]
    out: List[Dict[str, Any]] = []
    library_db.ensure_schema(conn)
    for i, meta in enumerate(metas or []):
        if not isinstance(meta, dict):
            continue
        cid = int(meta.get("chunk_id", 0))
        pid = int(meta.get("paper_id", 0))
        prow = _paper_row(conn, pid)
        title = (prow or {}).get("title") or ""
        bib = (prow or {}).get("bibcode")
        arx = (prow or {}).get("arxiv_id")
        dist = float(dists[i]) if i < len(dists) else 0.0
        snippet = docs[i] if i < len(docs) else None
        row_out = {
            "chunk_id": cid,
            "paper_id": pid,
            "bibcode": bib,
            "arxiv_id": arx,
            "title": title,
            "snippet": snippet,
            "distance": dist,
            "chroma_id": ids_out[i] if i < len(ids_out) else None,
            "chunk_ord": meta.get("chunk_ord"),
            "backend": "vector",
        }
        for k in ("page", "section", "source_kind", "source_backend"):
            if meta.get(k):
                row_out[k] = meta.get(k)
        out.append(row_out)
    return out


def _related_fts_query(conn: Any, paper_id: int) -> str:
    prow = _paper_row(conn, paper_id)
    parts: List[str] = []
    if prow:
        parts.append((prow.get("title") or "").strip())
        parts.append((prow.get("abstract") or "").strip()[:600])
    library_db.ensure_schema(conn)
    row = conn.execute(
        "SELECT text FROM paper_chunks WHERE paper_id = ? ORDER BY chunk_ord LIMIT 1",
        (paper_id,),
    ).fetchone()
    if row and row[0]:
        parts.append(str(row[0])[:450])
    return " ".join(p for p in parts if p).strip()


def _mean_vector(vecs: List[List[float]]) -> List[float]:
    if not vecs:
        return []
    dim = len(vecs[0])
    acc = [0.0] * dim
    for v in vecs:
        for j, x in enumerate(v):
            acc[j] += float(x)
    n = float(len(vecs))
    return [x / n for x in acc]


def _coerce_embeddings_list(raw: Any) -> List[List[float]]:
    """Chroma may return list of lists or a single numpy array (n, dim)."""
    if raw is None:
        return []
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if not isinstance(raw, list):
        return []
    out: List[List[float]] = []
    for e in raw:
        if e is None:
            continue
        if hasattr(e, "tolist"):
            e = e.tolist()
        try:
            out.append([float(x) for x in e])
        except (TypeError, ValueError):
            continue
    return out


def get_related_papers(
    conn: Any,
    paper_id: int,
    *,
    limit: int = 8,
    embed_client: EmbeddingClient | None = None,
    collection: Any | None = None,
    related_mode: str = "semantic",
    semantic_backend: str | None = None,
) -> List[Dict[str, Any]]:
    load_env()
    if related_mode != "semantic":
        raise ValueError("Only related_mode='semantic' is implemented (hybrid reserved).")
    if _is_fts_backend(semantic_backend):
        rq = _related_fts_query(conn, paper_id)
        if not rq:
            return []
        n = max(1, min(int(limit) * 8, 80))
        hits = library_db.search_chunks_fts(
            conn,
            rq,
            limit=n,
            exclude_paper_id=paper_id,
            fts_join=" OR ",
            fts_max_terms=14,
        )
        library_db.ensure_schema(conn)
        seen: set[int] = set()
        out_rel: List[Dict[str, Any]] = []
        for h in hits:
            pid = int(h["paper_id"])
            if pid == paper_id or pid in seen:
                continue
            seen.add(pid)
            prow = _paper_row(conn, pid)
            out_rel.append(
                {
                    "paper_id": pid,
                    "bibcode": (prow or {}).get("bibcode"),
                    "arxiv_id": (prow or {}).get("arxiv_id"),
                    "title": (prow or {}).get("title") or "",
                    "distance": float(h.get("rank") or 0.0),
                    "backend": "fts",
                }
            )
            if len(out_rel) >= int(limit):
                break
        return out_rel

    collection = collection or get_semantic_collection()
    ec = embed_client or get_embedding_client()
    got = collection.get(
        where={"paper_id": paper_id},
        include=["embeddings"],
    )
    valid = _coerce_embeddings_list(got.get("embeddings"))
    if not valid:
        return []
    qv = _mean_vector(valid)
    try:
        total_chunks = int(collection.count())
    except Exception:
        total_chunks = 256
    n_results = min(max(total_chunks, 50), max(int(limit) * 25, 150), 512)
    raw = collection.query(
        query_embeddings=[qv],
        n_results=n_results,
        include=["metadatas", "distances"],
    )
    metas = (raw.get("metadatas") or [[]])[0]
    dists = (raw.get("distances") or [[]])[0]
    best: Dict[int, float] = {}
    for i, meta in enumerate(metas or []):
        if not isinstance(meta, dict):
            continue
        pid = int(meta.get("paper_id", 0))
        if pid == paper_id or pid <= 0:
            continue
        d = float(dists[i]) if i < len(dists) else 0.0
        if pid not in best or d < best[pid]:
            best[pid] = d
    ranked = sorted(best.items(), key=lambda x: x[1])[: int(limit)]
    out: List[Dict[str, Any]] = []
    library_db.ensure_schema(conn)
    for pid, dist in ranked:
        prow = _paper_row(conn, pid)
        out.append(
            {
                "paper_id": pid,
                "bibcode": (prow or {}).get("bibcode"),
                "arxiv_id": (prow or {}).get("arxiv_id"),
                "title": (prow or {}).get("title") or "",
                "distance": dist,
                "backend": "vector",
            }
        )
    return out
