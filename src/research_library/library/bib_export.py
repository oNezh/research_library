"""Build an ADS-format BibTeX file from a free-form reference list; optional DB ingest."""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional

from research_library.config import load_env
from . import db as library_db
from .reference_parse import (
    fallback_arxiv_for_refline,  # noqa: F401 — re-export
    parse_catalog_line,
    parse_lines,
)
from research_library.lookup import ads_fetch_doc_by_bibcode, fetch_bibtex_bulk


def _ingest_doc(
    conn: Any,
    doc: Dict[str, Any],
    *,
    source: str = "ads_bib_export",
    pdf_relpath: Optional[str] = None,
) -> None:
    bibcode = doc.get("bibcode")
    if not bibcode:
        return
    title_l = doc.get("title")
    title = title_l[0] if isinstance(title_l, list) and title_l else (title_l or "") or ""
    abs_raw = doc.get("abstract")
    if isinstance(abs_raw, list):
        abstract = abs_raw[0] if abs_raw else ""
    else:
        abstract = (abs_raw or "") or ""
    authors = list(doc.get("author") or [])
    from research_library.lookup import choose_identifier

    _, arxiv_id = choose_identifier(doc.get("identifier") or [])
    year = doc.get("year")
    published = f"{year}-01-01" if year else None
    library_db.upsert_paper(
        conn,
        arxiv_id=arxiv_id,
        title=title,
        abstract=abstract,
        authors=authors,
        categories=[],
        matched_keywords=[],
        published=published,
        bibcode=bibcode,
        source=source,
        pdf_relpath=pdf_relpath,
    )


def ingest_ads_doc(
    conn: Any,
    doc: Dict[str, Any],
    *,
    source: str = "ads_bib_export",
    pdf_relpath: Optional[str] = None,
) -> None:
    """Upsert one ADS search API document into papers (+ FTS)."""
    _ingest_doc(conn, doc, source=source, pdf_relpath=pdf_relpath)


def list_to_bibtex_export(
    text: str,
    *,
    ingest_missing: bool = True,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    load_env()
    token = __import__("os").environ.get("ADS_API_TOKEN")
    if not token:
        return {
            "ok": False,
            "error": "ADS_API_TOKEN is required for ADS BibTeX export",
            "bibtex": "",
            "items": [],
        }

    lines = parse_lines(text)
    if conn is None:
        conn = library_db.connect()

    bibcodes: List[str] = []
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for line in lines:
        rec: Dict[str, Any] = {"line": line, "bibcode": None, "source": None, "ingested": False}
        ref = parse_catalog_line(line, conn, use_ads=True)
        if not ref.bibcode:
            rec["resolution"] = ref.resolution_note
            rec["error"] = ref.resolution_note
            items.append(rec)
            continue

        bib = ref.bibcode
        rec["bibcode"] = bib
        rec["source"] = "database" if ref.library_hit else "ads"
        rec["resolution"] = ref.resolution_note

        if ingest_missing and not ref.library_hit:
            doc = ads_fetch_doc_by_bibcode(bib)
            if doc:
                try:
                    _ingest_doc(conn, doc)
                    rec["ingested"] = True
                except Exception as e:
                    rec["ingest_error"] = str(e)

        if bib and bib not in seen:
            seen.add(bib)
            bibcodes.append(bib)
        items.append(rec)

    bibtex = fetch_bibtex_bulk(bibcodes) or ""
    return {
        "ok": True,
        "bibtex": bibtex,
        "bibcodes": bibcodes,
        "items": items,
        "n_lines": len(lines),
        "n_bibcodes": len(bibcodes),
    }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    load_env()
    p = argparse.ArgumentParser(prog="research-lib library bib-export")
    p.add_argument(
        "input",
        nargs="?",
        help="File with one reference / bibcode / arXiv / DOI per line; omit to read stdin",
    )
    p.add_argument("--text", default="", help="Inline newline-separated list instead of file")
    p.add_argument("--no-ingest", action="store_true", help="Do not upsert missing papers into library.db")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    body = args.text.replace("\\n", "\n") if args.text else ""
    if not body:
        if args.input and args.input != "-":
            try:
                with open(args.input, encoding="utf-8") as f:
                    body = f.read()
            except OSError as e:
                print(str(e), file=sys.stderr)
                return 2
        else:
            body = sys.stdin.read()

    out = list_to_bibtex_export(body, ingest_missing=not args.no_ingest)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if not out.get("ok"):
            print(out.get("error", "error"), file=sys.stderr)
            return 1
        print(out.get("bibtex", ""))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
