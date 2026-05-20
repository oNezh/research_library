"""LLM-backed structured comparison over semantic chunks (scaffold)."""

from __future__ import annotations

import csv
import io
import json
import os
from typing import Any, Dict, List, Optional, Sequence

from research_library.analysis.embeddings import get_embedding_client
from research_library.analysis.llm.base import ChatMessage
from research_library.analysis.llm.registry import get_chat_client
from research_library.config import effective_semantic_backend, load_env
from research_library.library import db as library_db
from research_library.library.semantic import get_semantic_collection


def _gather_context_for_paper(
    collection: Any,
    topic: str,
    paper_id: int,
    *,
    chunks_per_paper: int,
    embed_client: Any,
    query_vector: Optional[List[float]] = None,
) -> str:
    """Run a single Chroma query for one paper.

    If ``query_vector`` is provided we re-use it across papers — this avoids
    re-embedding the same topic N times when comparing N papers.
    """
    qv = query_vector if query_vector is not None else embed_client.embed_texts(
        [topic], for_query=True
    )[0]
    k = max(1, min(int(chunks_per_paper), 20))
    raw = collection.query(
        query_embeddings=[qv],
        n_results=k,
        where={"paper_id": paper_id},
        include=["documents"],
    )
    docs = (raw.get("documents") or [[]])[0]
    parts = [str(d).strip() for d in docs if d]
    return "\n\n---\n\n".join(parts)


def _gather_chunks_fts_for_paper(
    conn: Any,
    topic: str,
    paper_id: int,
    *,
    chunks_per_paper: int,
) -> str:
    k = max(1, min(int(chunks_per_paper), 20))
    need = max(k * 4, k + 4)
    hits = library_db.search_chunks_fts(
        conn, topic, limit=need, paper_id=int(paper_id)
    )
    parts = [str(h["text"]).strip() for h in hits if h.get("text")]
    if len(parts) < k:
        cur = conn.execute(
            """
            SELECT text FROM paper_chunks WHERE paper_id = ?
            ORDER BY chunk_ord LIMIT ?
            """,
            (int(paper_id), k),
        )
        for (t,) in cur.fetchall():
            if t and str(t).strip() and str(t).strip() not in parts:
                parts.append(str(t).strip())
            if len(parts) >= k:
                break
    return "\n\n---\n\n".join(parts[:k])


def extract_topic_metrics(
    conn: Any,
    topic: str,
    paper_ids: Sequence[int],
    *,
    schema_hint: str = "",
    chunks_per_paper: int = 4,
    persist_run: bool = True,
    semantic_backend: str | None = None,
) -> Dict[str, Any]:
    """Retrieve top chunks per paper for ``topic``, ask LLM for JSON metrics/compare."""
    load_env()
    library_db.ensure_schema(conn)
    use_fts = effective_semantic_backend(semantic_backend) == "fts"
    collection: Any = None
    ec: Any = None
    topic_qv: Optional[List[float]] = None
    if not use_fts:
        collection = get_semantic_collection()
        ec = get_embedding_client()
        # Embed topic exactly once and reuse across all N papers.
        try:
            topic_qv = ec.embed_texts([topic], for_query=True)[0]
        except Exception:
            topic_qv = None
    llm = get_chat_client()

    default_schema = (
        'Each item: {"paper_id": <int>, "metrics": {<key>: <string or number>}, '
        '"notes": <short string>}'
    )
    schema = (schema_hint or "").strip() or default_schema
    blocks: List[str] = []
    for pid in paper_ids:
        if use_fts:
            ctx = _gather_chunks_fts_for_paper(
                conn,
                topic,
                int(pid),
                chunks_per_paper=chunks_per_paper,
            )
        else:
            assert collection is not None and ec is not None
            ctx = _gather_context_for_paper(
                collection,
                topic,
                int(pid),
                chunks_per_paper=chunks_per_paper,
                embed_client=ec,
                query_vector=topic_qv,
            )
        if not ctx.strip():
            ctx = "(no indexed chunks for this paper)"
        prow = conn.execute("SELECT title, bibcode FROM papers WHERE id = ?", (int(pid),)).fetchone()
        title = (prow[0] if prow else "") or ""
        bib = (prow[1] if prow else "") or ""
        blocks.append(f"=== paper_id={pid} title={title!r} bibcode={bib!r} ===\n{ctx}")

    joined = "\n\n".join(blocks)
    user_msg = (
        f"主题/问题：{topic}\n\n"
        f"下面是多篇论文中与该主题最相关的摘录（可能不完整）。\n\n{joined}\n\n"
        f"请只输出一个 JSON 数组（不要 Markdown），数组元素格式：{schema}\n"
        "**硬约束**：所有数值与陈述必须能在上面给出的摘录中**逐字找到**或由其直接换算得来；"
        "若摘录中没有对应数字，对应 metrics 字段填 null（**不要**根据领域常识补全）。"
        "notes 字段也只能引用摘录里出现过的内容。"
    )
    messages: List[ChatMessage] = [
        {
            "role": "system",
            "content": (
                "只输出合法 JSON 数组，不要其它说明文字。"
                "你只能依据用户消息里的摘录回答；任何在摘录之外的信息一律视为缺失。"
            ),
        },
        {"role": "user", "content": user_msg},
    ]
    cap_raw = (os.environ.get("RESEARCH_SEMANTIC_COMPARE_MAX_TOKENS") or "").strip()
    try:
        cap = max(512, int(cap_raw)) if cap_raw else 8192
    except ValueError:
        cap = 8192
    raw = llm.chat(messages, max_completion_tokens=cap, temperature=0.2)
    text = raw.strip()

    def _strip_json_fence(s: str) -> str:
        t = s.strip()
        if not t.startswith("```"):
            return t
        t = t[3:].lstrip()
        if t.lower().startswith("json"):
            t = t[4:].lstrip("\n")
        if t.endswith("```"):
            t = t[:-3]
        return t.strip()

    text = _strip_json_fence(text)

    parsed: Any = None
    err = ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        err = str(e)
        parsed = None

    csv_s = ""
    if isinstance(parsed, list) and parsed:
        buf = io.StringIO()
        flat_keys: set[str] = set()
        for row in parsed:
            if isinstance(row, dict) and isinstance(row.get("metrics"), dict):
                for k in row["metrics"]:
                    flat_keys.add(str(k))
        fieldnames = ["paper_id", "bibcode", "title"] + sorted(flat_keys) + ["notes"]
        w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in parsed:
            if not isinstance(row, dict):
                continue
            pid = row.get("paper_id")
            bibcode = ""
            title = ""
            if pid is not None:
                r2 = conn.execute(
                    "SELECT bibcode, title FROM papers WHERE id = ?", (int(pid),)
                ).fetchone()
                if r2:
                    bibcode = r2[0] or ""
                    title = (r2[1] or "").replace("\n", " ")
            out_row: Dict[str, Any] = {
                "paper_id": pid if pid is not None else "",
                "bibcode": bibcode,
                "title": title,
                "notes": row.get("notes", ""),
            }
            m = row.get("metrics")
            if isinstance(m, dict):
                for k, v in m.items():
                    out_row[str(k)] = v
            w.writerow({k: out_row.get(k, "") for k in fieldnames})
        csv_s = buf.getvalue()

    result: Dict[str, Any] = {
        "topic": topic,
        "paper_ids": [int(x) for x in paper_ids],
        "schema_hint": schema_hint,
        "semantic_backend": "fts" if use_fts else "vector",
        "raw_model_output": raw,
        "parsed_json": parsed,
        "parse_error": err or None,
        "csv": csv_s,
    }
    if persist_run and parsed is not None:
        rid = library_db.insert_extraction_run(
            conn,
            topic=topic,
            schema_json=schema,
            result_json=json.dumps(parsed, ensure_ascii=False),
        )
        conn.commit()
        result["extraction_run_id"] = rid
    return result
