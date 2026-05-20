"""Multi-query chunk gather + optional LLM synthesis into a topic dossier (markdown)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from research_library.analysis.llm.base import ChatMessage
from research_library.analysis.llm.registry import get_chat_client
from research_library.config import load_env
from research_library.library import db as library_db


def _default_expansion_queries(topic: str) -> List[str]:
    t = (topic or "").strip()
    if not t:
        return []
    return [
        t,
        f"{t} methodology",
        f"{t} results observational implications",
    ]


def _chunk_dedupe_key(row: Dict[str, Any]) -> Tuple[int, Any]:
    pid = int(row.get("paper_id", 0) or 0)
    cid = int(row.get("chunk_id", 0) or 0)
    if cid:
        return pid, cid
    return pid, str(row.get("chroma_id") or "")


def gather_topic_chunks(
    conn: Any,
    topic: str,
    *,
    extra_queries: Optional[List[str]] = None,
    per_query_limit: int = 12,
    semantic_backend: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run semantic_search over topic + expansions; dedupe by (paper_id, chunk_id)."""
    from research_library.library.semantic import semantic_search

    t = (topic or "").strip()
    if not t:
        return [], {"queries_used": [], "deduped": 0, "error": "empty_topic"}

    qset: List[str] = []
    seen_q: set[str] = set()
    for q in [t] + list(extra_queries or []):
        s = (q or "").strip()
        if s and s.lower() not in seen_q:
            seen_q.add(s.lower())
            qset.append(s)
    if not extra_queries:
        for q in _default_expansion_queries(t)[1:]:
            if q.lower() not in seen_q:
                seen_q.add(q.lower())
                qset.append(q)

    seen_keys: set = set()
    merged: List[Dict[str, Any]] = []
    deduped = 0
    lim = max(1, min(int(per_query_limit), 50))
    for q in qset:
        rows = semantic_search(conn, q, limit=lim, semantic_backend=semantic_backend)
        for r in rows:
            key = _chunk_dedupe_key(r)
            if key in seen_keys:
                deduped += 1
                continue
            seen_keys.add(key)
            r = dict(r)
            r["matched_query"] = q
            merged.append(r)

    merged.sort(key=lambda x: float(x.get("distance", 9e9)))
    meta = {"queries_used": qset, "deduped": deduped, "n_chunks": len(merged)}
    return merged, meta


def _format_sources_for_llm(chunks: List[Dict[str, Any]], max_chars: int) -> str:
    parts: List[str] = []
    total = 0
    for i, c in enumerate(chunks, start=1):
        bib = c.get("bibcode") or ""
        pid = c.get("paper_id")
        cid = c.get("chunk_id")
        title = (c.get("title") or "").replace("\n", " ")[:120]
        dist = c.get("distance")
        try:
            dist_s = f"{float(dist):.4f}" if dist is not None else "n/a"
        except (TypeError, ValueError):
            dist_s = "n/a"
        sn = (c.get("snippet") or "").strip()
        cid_part = f", chunk_id={cid}" if cid else ""
        loc_bits: List[str] = []
        sec = (c.get("section") or "").strip()
        if sec:
            loc_bits.append(f"section=“{sec[:100]}”")
        if c.get("page") not in (None, "", 0):
            loc_bits.append(f"page={c.get('page')}")
        if c.get("source_kind"):
            loc_bits.append(f"src={c.get('source_kind')}")
        loc_part = (", " + ", ".join(loc_bits)) if loc_bits else ""
        block = (
            f"### Source {i} (paper_id={pid}, bibcode={bib}{cid_part}{loc_part}, distance={dist_s})\n"
            f"Title: {title}\n"
            f"Matched query: {c.get('matched_query', '')}\n\n{sn}\n"
        )
        if total + len(block) > max_chars and parts:
            break
        parts.append(block)
        total += len(block)
    return "\n---\n".join(parts)


def synthesize_topic_dossier_markdown(
    topic: str,
    chunks: List[Dict[str, Any]],
    *,
    max_context_chars: int = 48_000,
    max_tokens: Optional[int] = None,
) -> str:
    load_env()
    if max_tokens is None:
        raw = (os.environ.get("RESEARCH_TOPIC_DOSSIER_MAX_TOKENS") or "").strip()
        try:
            max_tokens = int(raw) if raw else 8192
        except ValueError:
            max_tokens = 8192
    cap = max(1000, min(int(max_tokens), 65536))
    bundle = _format_sources_for_llm(chunks, max(4000, min(int(max_context_chars), 120_000)))

    llm = get_chat_client()
    messages = [
        ChatMessage(
            role="system",
            content=(
                "You summarize indexed paper excerpts into a structured markdown dossier. "
                "STRICT GROUNDING: every concrete claim, number, or quote MUST be traceable "
                "to the provided sources — cite by appending `(bibcode=…, chunk_id=…)` (or "
                "`paper_id=…` if no bibcode) at the end of the relevant sentence. If you "
                "cannot find an answer in the sources, write `(未在检索片段中找到)` instead "
                "of guessing. If sources disagree, note both sides and their citations. "
                "Use headings and bullet lists. Write in the same language as the user's topic."
            ),
        ),
        ChatMessage(
            role="user",
            content=f"Topic / question:\n{topic.strip()}\n\n---\n\nIndexed excerpts:\n\n{bundle}",
        ),
    ]
    return llm.chat(messages, max_completion_tokens=cap, temperature=0.25).strip()


def build_topic_dossier(
    conn: Any,
    topic: str,
    *,
    extra_queries: Optional[List[str]] = None,
    per_query_limit: int = 12,
    semantic_backend: Optional[str] = None,
    synthesize: bool = True,
    max_context_chars: int = 48_000,
) -> Dict[str, Any]:
    """Gather chunks across queries; optionally call LLM for one markdown report."""
    library_db.ensure_schema(conn)
    chunks, gmeta = gather_topic_chunks(
        conn,
        topic,
        extra_queries=extra_queries,
        per_query_limit=per_query_limit,
        semantic_backend=semantic_backend,
    )
    out: Dict[str, Any] = {
        "topic": (topic or "").strip(),
        "gather": gmeta,
        "chunks": chunks,
        "markdown": "",
    }
    if synthesize and chunks:
        out["markdown"] = synthesize_topic_dossier_markdown(
            topic, chunks, max_context_chars=max_context_chars
        )
    return out
