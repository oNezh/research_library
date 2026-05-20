"""Citation edges from ADS over local papers: sync, graph JSON, missing hubs, ingest."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Set

from research_library.config import load_env
from research_library.library import db as library_db
from research_library.library.bib_export import ingest_ads_doc
from research_library.library.reference_parse import (
    db_lookup_bibcode_by_arxiv_variants,
    strip_arxiv_version,
)
from research_library.lookup import ads_fetch_doc_by_bibcode, ads_query


def fetch_ads_reference_bibcodes(bibcode: str) -> List[str]:
    r = ads_query(f'bibcode:"{bibcode}"', rows=1, fl=["bibcode", "reference"])
    docs = r.get("response", {}).get("docs", [])
    if not docs:
        return []
    refs = docs[0].get("reference") or []
    if not isinstance(refs, list):
        return []
    return [str(x).strip() for x in refs if x]


def resolve_bibcode_from_arxiv(conn: Any, arxiv_id: str) -> Optional[str]:
    qid = strip_arxiv_version(arxiv_id)
    if not qid:
        return None
    bc = db_lookup_bibcode_by_arxiv_variants(conn, qid)
    if bc:
        return bc
    try:
        r = ads_query(f"arxiv:{qid}", rows=1, fl=["bibcode"])
        docs = r.get("response", {}).get("docs", [])
        if docs and docs[0].get("bibcode"):
            return str(docs[0]["bibcode"])
    except Exception:
        pass
    return None


def _paper_ids_missing_references(conn: Any) -> Set[int]:
    """Local papers with no ``paper_references`` rows but have bibcode or arXiv id."""
    library_db.ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT p.id FROM papers p
        WHERE NOT EXISTS (
            SELECT 1 FROM paper_references pr WHERE pr.from_paper_id = p.id
        )
        AND (
            TRIM(COALESCE(p.bibcode, '')) != ''
            OR TRIM(COALESCE(p.arxiv_id, '')) != ''
        )
        """
    )
    return {int(r[0]) for r in cur.fetchall()}


def sync_references_from_ads(
    conn: Any,
    *,
    resolve_arxiv_bibcodes: bool = False,
    paper_ids: Optional[Set[int]] = None,
    sleep_s: float = 0.0,
    missing_only: bool = False,
) -> Dict[str, Any]:
    load_env()
    import os

    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {"ok": False, "error": "ADS_API_TOKEN is required"}

    library_db.ensure_schema(conn)
    if missing_only:
        miss = _paper_ids_missing_references(conn)
        if paper_ids is not None:
            paper_ids = paper_ids & miss
        else:
            paper_ids = miss

    if paper_ids is not None and len(paper_ids) == 0:
        conn.commit()
        return {
            "ok": True,
            "papers_considered": 0,
            "papers_synced": 0,
            "reference_edges_written": 0,
            "skipped_no_bibcode": 0,
            "bibcodes_resolved_from_arxiv": 0,
            "errors": [],
            "error_truncated": False,
            "missing_only": missing_only,
        }

    if paper_ids is not None:
        id_list = sorted(paper_ids)
        placeholders = ",".join("?" * len(id_list))
        rows = conn.execute(
            f"SELECT id, bibcode, arxiv_id FROM papers WHERE id IN ({placeholders}) ORDER BY id",
            id_list,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, bibcode, arxiv_id FROM papers ORDER BY id"
        ).fetchall()

    skipped_no_bibcode = 0
    resolved_arxiv = 0
    processed = 0
    total_ref_edges = 0
    errors: List[Dict[str, Any]] = []

    for row in rows:
        pid = int(row[0])
        bib = (row[1] or "").strip() or None
        arx = (row[2] or "").strip() or None

        if not bib and resolve_arxiv_bibcodes and arx:
            nb = resolve_bibcode_from_arxiv(conn, arx)
            if nb:
                try:
                    library_db.update_paper_bibcode(conn, pid, nb)
                    resolved_arxiv += 1
                except sqlite3.IntegrityError:
                    # Another row already holds this bibcode; still fetch refs for this row.
                    pass
                bib = nb

        if not bib:
            skipped_no_bibcode += 1
            continue

        try:
            refs = fetch_ads_reference_bibcodes(bib)
            n = library_db.replace_paper_references(conn, pid, refs)
            total_ref_edges += n
            processed += 1
        except Exception as e:
            errors.append({"paper_id": pid, "bibcode": bib, "error": str(e)})

        if sleep_s > 0:
            time.sleep(sleep_s)

    conn.commit()
    return {
        "ok": True,
        "papers_considered": len(rows),
        "papers_synced": processed,
        "reference_edges_written": total_ref_edges,
        "skipped_no_bibcode": skipped_no_bibcode,
        "bibcodes_resolved_from_arxiv": resolved_arxiv,
        "errors": errors[:50],
        "error_truncated": len(errors) > 50,
        "missing_only": missing_only,
    }


def missing_citation_hubs(
    conn: Any, *, min_citing_papers: int = 2
) -> List[Dict[str, Any]]:
    library_db.ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT pr.ref_bibcode AS bibcode,
               COUNT(DISTINCT pr.from_paper_id) AS citing_papers
        FROM paper_references pr
        LEFT JOIN papers p ON p.bibcode = pr.ref_bibcode
        WHERE p.id IS NULL
        GROUP BY pr.ref_bibcode
        HAVING COUNT(DISTINCT pr.from_paper_id) >= ?
        ORDER BY citing_papers DESC
        """,
        (min_citing_papers,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "bibcode": r[0],
                "citing_papers": int(r[1] or 0),
            }
        )
    return out


def _paper_label(title: str, bibcode: Optional[str], arxiv_id: Optional[str], max_len: int = 48) -> str:
    t = (title or "").replace("\n", " ").strip()
    if len(t) > max_len:
        t = t[: max_len - 1] + "…"
    if t:
        return t
    return bibcode or arxiv_id or "?"


def build_citation_graph(
    conn: Any,
    *,
    min_hub_citing_papers: int = 2,
    mermaid_max_nodes: int = 48,
) -> Dict[str, Any]:
    library_db.ensure_schema(conn)
    prow = conn.execute(
        """
        SELECT id, bibcode, arxiv_id, title FROM papers ORDER BY id
        """
    ).fetchall()
    papers: Dict[int, Dict[str, Any]] = {}
    bib_to_pid: Dict[str, int] = {}
    for r in prow:
        pid = int(r[0])
        rec = {
            "id": pid,
            "bibcode": r[1],
            "arxiv_id": r[2],
            "title": r[3],
            "label": _paper_label(str(r[3] or ""), r[1], r[2]),
        }
        papers[pid] = rec
        if r[1]:
            bib_to_pid[str(r[1]).strip()] = pid

    edges_rows = conn.execute(
        "SELECT from_paper_id, ref_bibcode FROM paper_references"
    ).fetchall()

    edges_out: List[Dict[str, Any]] = []
    for fr, ref_bc in edges_rows:
        fr = int(fr)
        ref_bc = str(ref_bc).strip()
        to = bib_to_pid.get(ref_bc)
        if to is not None:
            edges_out.append(
                {
                    "from_paper_id": fr,
                    "to_paper_id": to,
                    "ref_bibcode": ref_bc,
                    "in_library": True,
                }
            )
        else:
            edges_out.append(
                {
                    "from_paper_id": fr,
                    "to_paper_id": None,
                    "ref_bibcode": ref_bc,
                    "in_library": False,
                }
            )

    hubs = missing_citation_hubs(conn, min_citing_papers=min_hub_citing_papers)
    hub_set = {h["bibcode"] for h in hubs}

    ghost_nodes: List[Dict[str, Any]] = []
    for bc in sorted(hub_set):
        c = next(h["citing_papers"] for h in hubs if h["bibcode"] == bc)
        ghost_nodes.append(
            {
                "id": f"ext:{bc}",
                "kind": "missing_hub",
                "bibcode": bc,
                "citing_papers": c,
                "label": bc,
            }
        )

    nodes_out: List[Dict[str, Any]] = []
    for p in papers.values():
        nodes_out.append(
            {
                "id": f"paper:{p['id']}",
                "kind": "paper",
                "paper_id": p["id"],
                "bibcode": p["bibcode"],
                "arxiv_id": p["arxiv_id"],
                "label": p["label"],
            }
        )
    nodes_out.extend(ghost_nodes)

    mermaid = _mermaid_mindmap(
        papers,
        edges_out,
        hubs,
        max_nodes=mermaid_max_nodes,
    )

    return {
        "nodes": nodes_out,
        "edges": edges_out,
        "missing_hubs": hubs,
        "stats": {
            "papers": len(papers),
            "edges": len(edges_out),
            "internal_edges": sum(1 for e in edges_out if e["in_library"]),
            "missing_hubs": len(hubs),
        },
        "mermaid": mermaid,
    }


def _mermaid_escape(s: str) -> str:
    return (
        s.replace('"', "#quot;")
        .replace("\n", " ")
        .replace("(", " ")
        .replace(")", " ")
    )


def _mermaid_mindmap(
    papers: Dict[int, Dict[str, Any]],
    edges: List[Dict[str, Any]],
    hubs: List[Dict[str, Any]],
    *,
    max_nodes: int,
) -> str:
    """Compact mindmap: library + top missing hubs + citing papers (truncated)."""
    if not papers:
        return "mindmap\n  root((empty library))"

    hub_list = hubs[: min(12, len(hubs))]
    if not hub_list:
        root_l = "Library"
        lines = ["mindmap", f"  root(({_mermaid_escape(root_l)}))"]
        for p in list(papers.values())[: max(1, max_nodes // 2)]:
            lines.append(f"    {_mermaid_escape(p['label'])}")
        return "\n".join(lines)

    cite_by_hub: Dict[str, List[int]] = defaultdict(list)
    for e in edges:
        if e.get("in_library"):
            continue
        bc = e.get("ref_bibcode")
        if bc in {h["bibcode"] for h in hub_list}:
            cite_by_hub[str(bc)].append(int(e["from_paper_id"]))
    for bc_key, lst in cite_by_hub.items():
        cite_by_hub[bc_key] = list(dict.fromkeys(lst))

    lines = ["mindmap", "  root((Your library))"]
    used = 1
    for h in hub_list:
        if used >= max_nodes:
            break
        bc = h["bibcode"]
        short = bc if len(bc) <= 28 else bc[:27] + "…"
        hub_lbl = f"{short} ({h['citing_papers']}× cited)"
        hub_line = f"    {_mermaid_escape(hub_lbl)}"
        lines.append(hub_line)
        used += 1
        for pid in cite_by_hub.get(bc, [])[:8]:
            if used >= max_nodes:
                break
            pl = papers.get(pid, {}).get("label", str(pid))
            lines.append(f"      {_mermaid_escape(pl)}")
            used += 1
    return "\n".join(lines)


def ingest_hub_bibcodes(conn: Any, bibcodes: Sequence[str]) -> Dict[str, Any]:
    load_env()
    import os

    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return {"ok": False, "error": "ADS_API_TOKEN is required"}

    library_db.ensure_schema(conn)
    items: List[Dict[str, Any]] = []
    for raw in bibcodes:
        bc = (raw or "").strip()
        if not bc:
            continue
        doc = ads_fetch_doc_by_bibcode(bc)
        if not doc:
            items.append({"bibcode": bc, "ok": False, "error": "not found in ADS"})
            continue
        try:
            ingest_ads_doc(conn, doc, source="ads_citation_hub")
            items.append({"bibcode": bc, "ok": True})
        except Exception as e:
            items.append({"bibcode": bc, "ok": False, "error": str(e)})
    conn.commit()
    return {"ok": True, "ingested": items}


def _parse_bibcode_lines(text: str) -> List[str]:
    out: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_env()
    argv = list(argv if argv is not None else sys.argv[1:])
    parser = argparse.ArgumentParser(prog="research-lib library citation-*")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="Fetch ADS reference lists into paper_references")
    p_sync.add_argument(
        "--resolve-arxiv",
        action="store_true",
        help="Resolve bibcode from arXiv via ADS for rows missing bibcode",
    )
    p_sync.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds between ADS paper requests (rate limit)",
    )
    p_sync.add_argument(
        "--paper-id",
        type=int,
        action="append",
        dest="paper_ids",
        help="Only these local paper ids (repeatable)",
    )
    p_sync.add_argument(
        "--missing-only",
        action="store_true",
        help="Only papers that have no paper_references rows yet",
    )

    p_graph = sub.add_parser("graph", help="Nodes/edges + mindmap + missing hubs")
    p_graph.add_argument("--min-hub", type=int, default=2)
    p_graph.add_argument("--mermaid-max", type=int, default=48)
    p_graph.add_argument("--json", action="store_true")

    p_ing = sub.add_parser("ingest-hubs", help="Ingest ADS records for confirmed bibcodes")
    p_ing.add_argument("input", nargs="?", help="File or '-' for stdin")
    p_ing.add_argument("--text", default="", help="Inline newline-separated bibcodes")

    a = parser.parse_args(argv)

    conn = library_db.connect()

    if a.cmd == "sync":
        ids = set(a.paper_ids) if getattr(a, "paper_ids", None) else None
        out = sync_references_from_ads(
            conn,
            resolve_arxiv_bibcodes=getattr(a, "resolve_arxiv", False),
            paper_ids=ids,
            sleep_s=float(getattr(a, "sleep", 0.0) or 0.0),
            missing_only=bool(getattr(a, "missing_only", False)),
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    if a.cmd == "graph":
        g = build_citation_graph(
            conn,
            min_hub_citing_papers=int(a.min_hub),
            mermaid_max_nodes=int(a.mermaid_max),
        )
        if getattr(a, "json", False):
            print(json.dumps(g, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(g, ensure_ascii=False, indent=2))
            print("\n--- mermaid ---\n", file=sys.stderr)
            print(g.get("mermaid", ""))
        return 0

    if a.cmd == "ingest-hubs":
        lines: List[str] = []
        tx = getattr(a, "text", "") or ""
        if tx:
            lines.extend(_parse_bibcode_lines(tx))
        inp = getattr(a, "input", None)
        if inp:
            if inp == "-":
                lines.extend(_parse_bibcode_lines(sys.stdin.read()))
            else:
                from pathlib import Path

                lines.extend(_parse_bibcode_lines(Path(inp).read_text(encoding="utf-8")))
        out = ingest_hub_bibcodes(conn, lines)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("ok") else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
