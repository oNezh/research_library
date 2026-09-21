"""Research companion endpoints: citation graph and per-paper AI Q&A."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from research_library.library import db as library_db
from research_library.server.deps import get_conn

router = APIRouter(tags=["research"])


def _filter_graph_payload(
    g: Dict[str, Any],
    *,
    min_hub_citing: int,
    max_hubs: int,
    include_external_edges: bool,
    only_connected: bool,
    max_papers: int,
) -> Dict[str, Any]:
    hubs = sorted(
        g.get("missing_hubs") or [],
        key=lambda h: -int(h.get("citing_papers") or 0),
    )[:max_hubs]
    hub_bibs = {h["bibcode"] for h in hubs}

    edges_all = list(g.get("edges") or [])
    internal_edges = [e for e in edges_all if e.get("in_library")]

    connected_ids: set[int] = set()
    degree: dict[int, int] = defaultdict(int)
    for e in internal_edges:
        fr = int(e["from_paper_id"])
        to = e.get("to_paper_id")
        if to is None:
            continue
        tid = int(to)
        connected_ids.add(fr)
        connected_ids.add(tid)
        degree[fr] += 1
        degree[tid] += 1

    paper_nodes = [n for n in g.get("nodes") or [] if n.get("kind") == "paper"]
    truncated = False
    if only_connected:
        paper_nodes = [n for n in paper_nodes if n.get("paper_id") in connected_ids]
    if len(paper_nodes) > max_papers:
        truncated = True
        paper_nodes = sorted(
            paper_nodes,
            key=lambda n: -degree.get(int(n.get("paper_id") or 0), 0),
        )[:max_papers]
    kept_paper_ids = {int(n["paper_id"]) for n in paper_nodes if n.get("paper_id") is not None}

    hub_nodes = [
        n
        for n in g.get("nodes") or []
        if n.get("kind") == "missing_hub" and n.get("bibcode") in hub_bibs
    ]
    nodes = paper_nodes + hub_nodes
    node_ids = {n["id"] for n in nodes}

    edges: List[Dict[str, Any]] = []
    for e in edges_all:
        if e.get("in_library"):
            fr = int(e["from_paper_id"])
            to = e.get("to_paper_id")
            if to is None:
                continue
            tid = int(to)
            if fr in kept_paper_ids and tid in kept_paper_ids:
                edges.append(e)
        elif include_external_edges or e.get("ref_bibcode") in hub_bibs:
            fr = int(e["from_paper_id"])
            if fr not in kept_paper_ids:
                continue
            src = f"paper:{fr}"
            tgt = f"ext:{e.get('ref_bibcode')}"
            if src in node_ids and tgt in node_ids:
                edges.append(e)

    stats = dict(g.get("stats") or {})
    stats.update(
        {
            "nodes_returned": len(nodes),
            "edges_returned": len(edges),
            "truncated": truncated,
        }
    )

    return {
        "nodes": nodes,
        "edges": edges,
        "missing_hubs": hubs,
        "stats": stats,
    }


@router.get("/graph")
def citation_graph(
    min_hub_citing: int = Query(3, ge=1, le=50),
    max_hubs: int = Query(150, ge=0, le=2000),
    include_external_edges: bool = Query(False),
    only_connected: bool = Query(True),
    max_papers: int = Query(800, ge=50, le=5000),
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    from research_library.library.citations import build_citation_graph

    g = build_citation_graph(
        conn,
        min_hub_citing_papers=min_hub_citing,
        include_mermaid=False,
    )
    return _filter_graph_payload(
        g,
        min_hub_citing=min_hub_citing,
        max_hubs=max_hubs,
        include_external_edges=include_external_edges,
        only_connected=only_connected,
        max_papers=max_papers,
    )


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AskBody(BaseModel):
    question: str
    max_chunks: int = 16
    history: List[ChatTurn] = Field(default_factory=list)


@router.post("/papers/{paper_id}/ask")
def ask_paper(
    paper_id: int,
    body: AskBody,
    conn: sqlite3.Connection = Depends(get_conn),
) -> Dict[str, Any]:
    """Answer a question about one paper from its indexed chunks (sync LLM call)."""
    row = library_db.get_paper_row(conn, paper_id)
    if not row:
        raise HTTPException(status_code=404, detail="paper not found")
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question must not be empty")

    from research_library.library.semantic import retrieve_context_for_paper_question

    context, source = retrieve_context_for_paper_question(
        conn, paper_id, question, max_chunks=body.max_chunks
    )
    if not context.strip():
        raise HTTPException(
            status_code=409,
            detail="paper has no indexed chunks; run semantic_index first",
        )

    from research_library.analysis.llm.base import ChatMessage
    from research_library.analysis.llm.registry import get_chat_client

    try:
        llm = get_chat_client()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"LLM unavailable: {e}")

    system = (
        "You answer questions about one research paper using only the provided excerpts. "
        "Quote chunk markers like [chunk_id=N] after claims they support. "
        "If the excerpts do not contain the answer, say so plainly. "
        "Answer in the same language as the question."
    )
    user = (
        f"## Paper\n{row.get('title') or ''} ({row.get('bibcode') or row.get('arxiv_id') or ''})\n\n"
        f"## Question\n{question}\n\n## Excerpts\n{context}"
    )

    messages: List[ChatMessage] = [ChatMessage(role="system", content=system)]
    for turn in body.history[-6:]:
        content = turn.content.strip()
        if content:
            messages.append(ChatMessage(role=turn.role, content=content))
    messages.append(ChatMessage(role="user", content=user))

    try:
        answer = llm.chat(
            messages,
            max_completion_tokens=4096,
            temperature=0.2,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    return {
        "paper_id": paper_id,
        "question": question,
        "answer": answer.strip(),
        "retrieval_source": source,
    }
