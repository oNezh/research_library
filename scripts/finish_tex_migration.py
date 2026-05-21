#!/usr/bin/env python3
"""Finish tex migration: index missing papers, then retry fetch+index for arxiv-without-tex."""

from __future__ import annotations

import json
import sys
import time


def _missing_embedding_ids(conn) -> list[int]:
    rows = conn.execute(
        """
        SELECT p.id FROM papers p
        WHERE (
          (p.source_text_relpath IS NOT NULL AND TRIM(p.source_text_relpath) != '')
          OR (p.pdf_relpath IS NOT NULL AND TRIM(p.pdf_relpath) != '')
          OR (p.arxiv_id IS NOT NULL AND TRIM(p.arxiv_id) != '')
        )
        AND NOT EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id)
        ORDER BY p.id
        """
    ).fetchall()
    return [int(r[0]) for r in rows]


def _arxiv_no_tex_ids(conn) -> list[int]:
    rows = conn.execute(
        """
        SELECT id FROM papers
        WHERE arxiv_id IS NOT NULL AND TRIM(arxiv_id) != ''
          AND (
            source_text_relpath IS NULL OR TRIM(source_text_relpath) = ''
            OR source_kind IS NULL OR TRIM(source_kind) = ''
            OR source_kind != 'tex'
          )
        ORDER BY id
        """
    ).fetchall()
    return [int(r[0]) for r in rows]


def _summary(conn) -> dict:
    missing = len(_missing_embedding_ids(conn))
    no_tex = len(_arxiv_no_tex_ids(conn))
    indexed = conn.execute(
        "SELECT COUNT(DISTINCT paper_id) FROM paper_chunks"
    ).fetchone()[0]
    tex_emb = conn.execute(
        """
        SELECT COUNT(DISTINCT p.id) FROM papers p
        JOIN paper_chunks pc ON pc.paper_id = p.id
        WHERE p.source_kind = 'tex'
        """
    ).fetchone()[0]
    pdf_emb = conn.execute(
        """
        SELECT COUNT(DISTINCT p.id) FROM papers p
        JOIN paper_chunks pc ON pc.paper_id = p.id
        WHERE IFNULL(p.source_kind, '') != 'tex'
        """
    ).fetchone()[0]
    return {
        "missing_embedding": missing,
        "arxiv_no_tex": no_tex,
        "papers_indexed": indexed,
        "tex_embedded": tex_emb,
        "pdf_embedded": pdf_emb,
    }


def main() -> int:
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.semantic import index_paper, index_papers
    from research_library.library.tex_to_text import fetch_source_for_paper

    load_env()
    conn = library_db.connect()
    t0 = time.perf_counter()
    out: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "phases": []}

    print("[finish] before:", json.dumps(_summary(conn)), flush=True)

    # Phase 1: semantic-index papers that never got chunks (killed mid-run)
    p1_ids = _missing_embedding_ids(conn)
    print(f"[finish] Phase 1: semantic-index {len(p1_ids)} papers (no chunks yet)", flush=True)
    p1_errors = 0
    p1_items = []
    if p1_ids:
        r = index_papers(conn, p1_ids, force=True)
        p1_errors = int(r.get("errors") or 0)
        p1_items = r.get("items") or []
    out["phases"].append(
        {
            "name": "semantic_index_missing",
            "requested": len(p1_ids),
            "errors": p1_errors,
        }
    )
    print(
        f"[finish] Phase 1 done: ok={len(p1_ids)-p1_errors} errors={p1_errors}",
        flush=True,
    )

    # Phase 2: fetch-source for arxiv papers still without TeX
    p2_ids = _arxiv_no_tex_ids(conn)
    print(f"[finish] Phase 2: fetch-source {len(p2_ids)} papers", flush=True)
    fetch_ok = 0
    fetch_fail = 0
    for i, pid in enumerate(p2_ids, 1):
        try:
            fr = fetch_source_for_paper(conn, int(pid), force=True)
        except Exception as e:
            fr = {"ok": False, "paper_id": pid, "error": str(e)}
        if fr.get("ok"):
            fetch_ok += 1
        else:
            fetch_fail += 1
        if i % 20 == 0 or i == len(p2_ids):
            print(
                f"[finish] fetch {i}/{len(p2_ids)} ok={fetch_ok} fail={fetch_fail}",
                flush=True,
            )
    out["phases"].append(
        {
            "name": "fetch_source_no_tex",
            "requested": len(p2_ids),
            "ok": fetch_ok,
            "errors": fetch_fail,
        }
    )

    # Phase 3: index papers from phase 2 batch (tex or pdf fallback)
    print(f"[finish] Phase 3: semantic-index {len(p2_ids)} retried papers", flush=True)
    p3_errors = 0
    if p2_ids:
        r3 = index_papers(conn, p2_ids, force=True)
        p3_errors = int(r3.get("errors") or 0)
    out["phases"].append(
        {
            "name": "semantic_index_after_fetch",
            "requested": len(p2_ids),
            "errors": p3_errors,
        }
    )

    # Phase 4: any stragglers still without chunks
    p4_ids = _missing_embedding_ids(conn)
    if p4_ids:
        print(f"[finish] Phase 4: semantic-index {len(p4_ids)} stragglers", flush=True)
        r4 = index_papers(conn, p4_ids, force=True)
        out["phases"].append(
            {
                "name": "semantic_index_stragglers",
                "requested": len(p4_ids),
                "errors": int(r4.get("errors") or 0),
            }
        )

    out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out["seconds"] = round(time.perf_counter() - t0, 1)
    out["after"] = _summary(conn)
    print("[finish] after:", json.dumps(out["after"]), flush=True)
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    return 0 if out["after"]["missing_embedding"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
