"""Semantic search → chunks + per‑paper references → LLM synthesis with explicit source tags."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from research_library.analysis.llm.base import ChatMessage
from research_library.analysis.llm.registry import get_chat_client
from research_library.config import load_env
from research_library.library import db as library_db


def _chunk_dedupe_key(row: Dict[str, Any]) -> Tuple[int, Any]:
    pid = int(row.get("paper_id", 0) or 0)
    cid = int(row.get("chunk_id", 0) or 0)
    if cid:
        return pid, cid
    return pid, str(row.get("chroma_id") or "")


def gather_chunks_for_report(
    conn: Any,
    query: str,
    *,
    extra_queries: Optional[List[str]] = None,
    expand_queries: bool = False,
    per_query_limit: int = 12,
    semantic_backend: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Retrieve chunks: single-query semantic search, or multi-query expansion like ``topic_dossier``."""
    from research_library.library.semantic import semantic_search
    from research_library.library.topic_dossier import gather_topic_chunks

    q = (query or "").strip()
    if not q:
        return [], {"error": "empty_query", "queries_used": []}

    if expand_queries:
        return gather_topic_chunks(
            conn,
            q,
            extra_queries=extra_queries,
            per_query_limit=per_query_limit,
            semantic_backend=semantic_backend,
        )

    lim = max(1, min(int(per_query_limit), 80))
    rows = semantic_search(conn, q, limit=lim, semantic_backend=semantic_backend)
    merged: List[Dict[str, Any]] = []
    for r in rows:
        c = dict(r)
        c["matched_query"] = q
        merged.append(c)
    meta = {
        "queries_used": [q],
        "deduped": 0,
        "n_chunks": len(merged),
        "expand_queries": False,
    }
    if extra_queries:
        seen = {_chunk_dedupe_key(x) for x in merged}
        deduped = 0
        for eq in extra_queries:
            s = (eq or "").strip()
            if not s:
                continue
            for r in semantic_search(conn, s, limit=lim, semantic_backend=semantic_backend):
                c = dict(r)
                c["matched_query"] = s
                k = _chunk_dedupe_key(c)
                if k in seen:
                    deduped += 1
                    continue
                seen.add(k)
                merged.append(c)
            meta["queries_used"].append(s)
        merged.sort(key=lambda x: float(x.get("distance", 9e9)))
        meta["deduped"] = deduped
        meta["n_chunks"] = len(merged)
    return merged, meta


def enrich_chunks_with_references(
    conn: Any,
    chunks: List[Dict[str, Any]],
    *,
    ref_limit_per_paper: int = 30,
) -> List[Dict[str, Any]]:
    """Attach ``paper_references`` (subset) for each chunk's parent paper."""
    library_db.ensure_schema(conn)
    cap = max(1, min(int(ref_limit_per_paper), 200))
    cache: Dict[int, List[Dict[str, Any]]] = {}
    out: List[Dict[str, Any]] = []
    for c in chunks:
        d = dict(c)
        pid = int(d.get("paper_id", 0) or 0)
        if pid <= 0:
            d["references"] = []
            out.append(d)
            continue
        if pid not in cache:
            edges = library_db.list_paper_reference_edges(conn, pid)
            cache[pid] = [
                {
                    "ref_bibcode": e["ref_bibcode"],
                    "title": (e.get("title") or "").strip(),
                    "in_library": bool(e.get("has_local_pdf")),
                    "to_paper_id": e.get("to_paper_id"),
                }
                for e in edges[:cap]
            ]
        d["references"] = list(cache[pid])
        out.append(d)
    return out


def _location_label(c: Dict[str, Any]) -> str:
    """Render the ``page``/``section`` Chroma metadata into a short label.

    Tex-backed chunks have ``section`` (most-recent heading); PDF chunks keep
    the old ``page`` form-feed count.
    """
    sec = (c.get("section") or "").strip()
    page = c.get("page")
    src_kind = (c.get("source_kind") or "").strip()
    src_be = (c.get("source_backend") or "").strip()
    parts: List[str] = []
    if sec:
        parts.append(f"section=“{sec[:120]}”")
    if page is not None and page != "":
        try:
            parts.append(f"page={int(page)}")
        except (TypeError, ValueError):
            pass
    if src_kind:
        parts.append(f"source_kind={src_kind}")
    if src_be:
        parts.append(f"source_backend={src_be}")
    return ", ".join(parts) if parts else "—"


def _format_sources_for_llm(
    chunks: List[Dict[str, Any]],
    max_chars: int,
) -> Tuple[str, List[Dict[str, Any]]]:
    parts: List[str] = []
    index_rows: List[Dict[str, Any]] = []
    total = 0
    for i, c in enumerate(chunks, start=1):
        tag = f"S{i}"
        bib = (c.get("bibcode") or "").strip()
        ax = (c.get("arxiv_id") or "").strip()
        pid = c.get("paper_id")
        title = (c.get("title") or "").replace("\n", " ")[:200]
        try:
            dist = float(c["distance"]) if c.get("distance") is not None else None
        except (TypeError, ValueError):
            dist = None
        dist_s = f"{dist:.4f}" if dist is not None else "n/a"
        sn = (c.get("snippet") or "").strip()
        refs = c.get("references") or []
        ref_lines: List[str] = []
        for j, ref in enumerate(refs[:40], start=1):
            bc = ref.get("ref_bibcode") or ""
            rt = (ref.get("title") or "").replace("\n", " ")[:120]
            lib = " [in library]" if ref.get("in_library") else ""
            ref_lines.append(f"  - R{j}: {bc}{lib} — {rt}")
        ref_block = (
            "\n".join(ref_lines)
            if ref_lines
            else "  (no references synced for this paper in the local index)"
        )
        location = _location_label(c)
        block = (
            f"### {tag} — retrieved excerpt\n"
            f"- **paper_id**: {pid}\n"
            f"- **bibcode**: {bib or '—'}\n"
            f"- **arxiv_id**: {ax or '—'}\n"
            f"- **title**: {title}\n"
            f"- **location**: {location}\n"
            f"- **retrieval distance**: {dist_s} (lower is closer for vector backend)\n"
            f"- **matched_query**: {c.get('matched_query', '')}\n\n"
            f"**Excerpt:**\n{sn}\n\n"
            f"**References from this paper** (ADS bibcodes in local `paper_references`; may be partial):\n"
            f"{ref_block}\n"
        )
        if total + len(block) > max_chars and parts:
            break
        parts.append(block)
        total += len(block)
        idx_row: Dict[str, Any] = {
            "tag": tag,
            "paper_id": pid,
            "chunk_id": c.get("chunk_id"),
            "bibcode": bib or None,
            "arxiv_id": ax or None,
            "title": title,
        }
        for k in ("section", "page", "source_kind"):
            v = c.get(k)
            if v:
                idx_row[k] = v
        index_rows.append(idx_row)
    return "\n---\n".join(parts), index_rows


def synthesize_semantic_report_markdown(
    query: str,
    bundle: str,
    *,
    max_tokens: Optional[int] = None,
) -> str:
    load_env()
    if max_tokens is None:
        raw = (os.environ.get("RESEARCH_SEMANTIC_REPORT_MAX_TOKENS") or "").strip()
        try:
            max_tokens = int(raw) if raw else 12_288
        except ValueError:
            max_tokens = 12_288
    cap = max(1000, min(int(max_tokens), 65536))

    llm = get_chat_client()
    system = (
        "You write a single coherent research report in markdown from retrieved paper excerpts bundled below. "
        "Rules:\n"
        "1) **Excerpts only:** Every substantive claim (facts, results, methods, interpretation) must be directly "
        "supported by the **Excerpt** text under some ### S* block. Paraphrase closely; do not add detail that is "
        "not stated or clearly implied there. Do not use outside knowledge or the open web.\n"
        "2) **Reference lists (R lines):** The “References from this paper” lists are not independent evidence. "
        "Do not summarize papers or assert findings based only on R-line titles or bibcodes. Mention an R-line "
        "entry only when the **Excerpt** for that S tag explicitly names or discusses that citation; otherwise "
        "omit it from the narrative.\n"
        "3) **Gaps:** If the excerpts do not answer the user question, say that plainly and report only what the "
        "excerpts do state—do not invent filler.\n"
        "4) Every paragraph (or each bullet) MUST end with one or more **[S1]**, **[S2]**, … tags for which "
        "excerpts support that text, matching the bundle headers (### S1 — …).\n"
        "5) Close with a **References** section: one bullet per **S** tag you used, with bibcode, arxiv_id if any, "
        "and short title from the bundle headers.\n"
        "6) Prefer the same language as the user's query when reasonable."
    )
    messages = [
        ChatMessage(role="system", content=system),
        ChatMessage(
            role="user",
            content=(
                f"## User question / topic\n{query.strip()}\n\n"
                "Ground the report **only** in the **Excerpt** passages in the following blocks; "
                "do not use other sources.\n\n"
                f"## Bundled excerpt sources (with per-paper reference lists)\n\n{bundle}"
            ),
        ),
    ]
    return llm.chat(messages, max_completion_tokens=cap, temperature=0.2).strip()


def build_semantic_report(
    conn: Any,
    query: str,
    *,
    extra_queries: Optional[List[str]] = None,
    expand_queries: bool = False,
    per_query_limit: int = 12,
    ref_limit_per_paper: int = 30,
    semantic_backend: Optional[str] = None,
    synthesize: bool = True,
    max_context_chars: int = 56_000,
) -> Dict[str, Any]:
    """Run semantic retrieval, attach references, optionally call LLM for a cited markdown report."""
    library_db.ensure_schema(conn)
    chunks, gmeta = gather_chunks_for_report(
        conn,
        query,
        extra_queries=extra_queries,
        expand_queries=expand_queries,
        per_query_limit=per_query_limit,
        semantic_backend=semantic_backend,
    )
    enriched = enrich_chunks_with_references(
        conn, chunks, ref_limit_per_paper=ref_limit_per_paper
    )
    bundle, source_index = _format_sources_for_llm(
        enriched, max(8000, min(int(max_context_chars), 200_000))
    )
    out: Dict[str, Any] = {
        "query": (query or "").strip(),
        "gather": gmeta,
        "source_index": source_index,
        "chunks": enriched,
        "bundle_chars": len(bundle),
        "markdown": "",
    }
    if synthesize and bundle.strip():
        out["markdown"] = synthesize_semantic_report_markdown(query, bundle)
    return out
