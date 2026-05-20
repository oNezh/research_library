"""CLI: research-lib library — local SQLite + FTS index."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import List, Optional

from research_library.config import get_pdfs_dir, load_env
from research_library.library import db


def main(argv: Optional[List[str]] = None) -> int:
    load_env()

    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog="research-lib library")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create tables and FTS (idempotent)")
    sub.add_parser("stats", help="Paper count and db path")
    p_s = sub.add_parser("search", help="Full-text search (title + abstract)")
    p_s.add_argument("query")
    p_s.add_argument("--limit", type=int, default=20)
    p_s.add_argument("--json", action="store_true")
    sub.add_parser("import-cache", help="Bulk upsert from arxiv_cache.json")
    p_bib = sub.add_parser(
        "bib-export",
        help="ADS-format BibTeX for a reference list (ingest missing rows into DB)",
    )
    p_bib.add_argument(
        "bib_input",
        nargs="?",
        help="File: one bibcode / arXiv / DOI / ref line per line; '-' or omit for stdin",
    )
    p_bib.add_argument("--text", default="", help="Inline list (use \\n for newlines in shell)")
    p_bib.add_argument("--no-ingest", action="store_true", help="Skip DB upsert for missing papers")
    p_bib.add_argument("--json", action="store_true")

    p_in = sub.add_parser(
        "ingest-ref",
        help="Resolve one reference line (ADS), download PDF under data/pdfs, upsert library.db",
    )
    p_in.add_argument(
        "--text",
        default="",
        help="Single bibliography line or bibcode / arXiv / DOI",
    )
    p_in.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="PDF download timeout seconds",
    )
    p_in.add_argument(
        "--skip-download",
        action="store_true",
        help="Only ingest if PDF already exists at expected path under data/pdfs",
    )
    p_in.add_argument("--json", action="store_true")

    p_ip = sub.add_parser(
        "ingest-pdf",
        help="Extract DOI/arXiv/title from PDF, match ADS, upsert papers + pdf_relpath (default: copy PDF to data/pdfs/). Use --doi/--arxiv/--match-title/--bibcode when auto match fails.",
    )
    p_ip.add_argument("pdf_path", help="Path to PDF")
    p_ip.add_argument("--dry-run", action="store_true", help="Resolve only; do not write DB")
    p_ip.add_argument(
        "--require-strong-id",
        action="store_true",
        help="Require DOI or arXiv in PDF (no title-only ADS match)",
    )
    p_ip.add_argument(
        "--no-copy-to-pdfs",
        action="store_true",
        help="Do not copy into data/pdfs/ (record original path as pdf_relpath)",
    )
    p_ip.add_argument(
        "--symlink-to-pdfs",
        action="store_true",
        help="Symlink PDF into data/pdfs/ instead of copying (implies no copy)",
    )
    p_ip.add_argument("--title-rows", type=int, default=3, help="ADS title search rows")
    p_ip.add_argument(
        "--doi",
        default=None,
        metavar="DOI",
        help="Manual metadata: DOI (10.xx/…) merged with PDF extraction for ADS match",
    )
    p_ip.add_argument(
        "--arxiv",
        default=None,
        metavar="ID",
        help="Manual metadata: arXiv id (version suffix optional)",
    )
    p_ip.add_argument(
        "--match-title",
        default=None,
        metavar="TITLE",
        help="Manual metadata: title for ADS search when extraction is wrong or empty",
    )
    p_ip.add_argument(
        "--bibcode",
        default=None,
        metavar="BIBCODE",
        help="Skip matching: attach PDF to this ADS bibcode (needs ADS_API_TOKEN)",
    )
    p_ip.add_argument(
        "--no-sync-references",
        action="store_true",
        help="Skip filling paper_references from ADS (default: sync when ADS_API_TOKEN set)",
    )
    p_ip.add_argument("--json", action="store_true")

    p_cit = sub.add_parser(
        "citation",
        help="ADS reference graph: sync, graph/mindmap, ingest missing hubs",
    )
    p_cit.add_argument(
        "citation_args",
        nargs=argparse.REMAINDER,
        help="e.g. sync --resolve-arxiv | graph --min-hub 2 | ingest-hubs -",
    )

    p_fs = sub.add_parser(
        "fetch-source",
        help="Pull arXiv source (default: ar5iv HTML) and persist clean text + section index per paper",
    )
    p_fs.add_argument(
        "--paper-id",
        action="append",
        type=int,
        dest="paper_ids",
        help="Paper id (repeatable); default: all papers with arxiv_id",
    )
    p_fs.add_argument(
        "--all",
        action="store_true",
        help="All papers with arxiv_id (default unless --paper-id is given)",
    )
    p_fs.add_argument(
        "--force",
        action="store_true",
        help="Refetch even if source_text_relpath already populated",
    )
    p_fs.add_argument(
        "--backend",
        default="",
        help="Override RESEARCH_TEX_BACKEND (ar5iv | pylatexenc | pandoc | latexml)",
    )
    p_fs.add_argument("--json", action="store_true")

    p_up = sub.add_parser(
        "update-pdf",
        help="Refetch PDF for a paper (publisher version preferred); overwrites the existing file",
    )
    p_up.add_argument(
        "--paper-id",
        action="append",
        type=int,
        dest="paper_ids",
        help="Paper id (repeatable); default: all papers with a current pdf_relpath",
    )
    p_up.add_argument(
        "--all",
        action="store_true",
        help="All papers with pdf_relpath (default unless --paper-id is given)",
    )
    p_up.add_argument(
        "--source",
        default="auto",
        choices=("auto", "pub", "ads", "eprint", "arxiv"),
        help="Which ADS link to prefer (default: auto = pub > ads > eprint > arxiv)",
    )
    p_up.add_argument("--timeout", type=int, default=120)
    p_up.add_argument(
        "--reindex",
        action="store_true",
        help="Re-run semantic-index --force for papers whose source_kind==pdf",
    )
    p_up.add_argument("--json", action="store_true")

    p_re = sub.add_parser(
        "reembed-from-source",
        help="fetch-source then semantic-index --force (only on papers we successfully (re)pulled source for)",
    )
    p_re.add_argument(
        "--paper-id",
        action="append",
        type=int,
        dest="paper_ids",
        help="Paper id (repeatable); default: all papers with arxiv_id",
    )
    p_re.add_argument(
        "--all",
        action="store_true",
        help="All papers with arxiv_id (default unless --paper-id is given)",
    )
    p_re.add_argument(
        "--force",
        action="store_true",
        help="Refetch source even when present",
    )
    p_re.add_argument(
        "--backend",
        default="",
        help="Override RESEARCH_TEX_BACKEND",
    )
    p_re.add_argument("--json", action="store_true")

    p_sem = sub.add_parser(
        "semantic-index",
        help="Chunk local PDFs: fts=SQLite FTS only; vector=Chroma+embeddings (pip install -e '.[semantic]')",
    )
    p_sem.add_argument(
        "--paper-id",
        action="append",
        type=int,
        dest="paper_ids",
        help="Paper id (repeatable); default: all papers with pdf_relpath",
    )
    p_sem.add_argument(
        "--force",
        action="store_true",
        help="Rebuild chunks even if rows already exist",
    )
    p_sem.add_argument(
        "--only-missing",
        action="store_true",
        help="Only papers with PDF and no paper_chunks yet (skips already-indexed)",
    )
    p_sem.add_argument("--json", action="store_true")

    p_ss = sub.add_parser(
        "semantic-search",
        help="Chunk-level search (RESEARCH_SEMANTIC_BACKEND=fts: BM25; else vector similarity)",
    )
    p_ss.add_argument("query")
    p_ss.add_argument("--limit", type=int, default=10)
    p_ss.add_argument("--json", action="store_true")

    p_find = sub.add_parser(
        "find",
        help="Local library first (bibcode/arXiv/FTS), then ADS + arXiv if no hits",
    )
    p_find.add_argument("query")
    p_find.add_argument("--limit-local", type=int, default=15)
    p_find.add_argument("--limit-remote", type=int, default=10)
    p_find.add_argument(
        "--also-remote",
        action="store_true",
        help="Include ADS/arXiv results even when the library has matches",
    )
    p_find.add_argument(
        "--remote-only",
        action="store_true",
        help="Skip local search (ADS + arXiv only)",
    )
    p_find.add_argument("--json", action="store_true")

    p_td = sub.add_parser(
        "topic-dossier",
        help="Multi-query semantic gather + dedupe + optional LLM markdown synthesis",
    )
    p_td.add_argument("topic")
    p_td.add_argument(
        "--extra-query",
        action="append",
        dest="extra_queries",
        help="Additional search phrase (repeatable)",
    )
    p_td.add_argument("--per-query-limit", type=int, default=12)
    p_td.add_argument(
        "--no-synth",
        action="store_true",
        help="Only return gathered chunks, skip LLM",
    )
    p_td.add_argument("--json", action="store_true")

    p_sr = sub.add_parser(
        "semantic-report",
        help="Semantic chunks + paper references + LLM report with [S1] source tags",
    )
    p_sr.add_argument("query", help="Question or topic")
    p_sr.add_argument(
        "--extra-query",
        action="append",
        dest="extra_queries",
        help="Additional search phrase (repeatable)",
    )
    p_sr.add_argument(
        "--expand-queries",
        action="store_true",
        help="Use topic-dossier-style multi-query expansion",
    )
    p_sr.add_argument("--limit", type=int, default=12, help="Chunks per query (default 12)")
    p_sr.add_argument(
        "--refs-per-paper",
        type=int,
        default=30,
        dest="refs_per_paper",
        help="Max rows from paper_references per parent paper",
    )
    p_sr.add_argument(
        "--max-context-chars",
        type=int,
        default=56000,
        dest="max_context_chars",
    )
    p_sr.add_argument(
        "--no-synth",
        action="store_true",
        help="Skip LLM; return structured JSON (--json) or stderr summary only",
    )
    p_sr.add_argument(
        "--semantic-backend",
        default="",
        help="fts | vector | omit for RESEARCH_SEMANTIC_BACKEND",
    )
    p_sr.add_argument("--json", action="store_true")

    p_dedupe = sub.add_parser(
        "dedupe",
        help="Merge duplicate papers (same bibcode, arXiv id, or pdf_relpath)",
    )
    p_dedupe.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned merges only; do not write",
    )
    p_dedupe.add_argument(
        "--chroma",
        action="store_true",
        help="Remove dropped paper ids from Chroma vector index if available",
    )
    p_dedupe.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        conn = db.connect()
        db.init_schema(conn)
        print(str(db.db_path()), file=sys.stderr)
        return 0

    if args.cmd == "stats":
        conn = db.connect()
        print(json.dumps(db.stats(conn), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "dedupe":
        conn = db.connect()
        out = db.dedupe_papers(
            conn,
            dry_run=bool(getattr(args, "dry_run", False)),
            chroma_delete=bool(getattr(args, "chroma", False)),
        )
        if not getattr(args, "dry_run", False):
            conn.commit()
        if getattr(args, "json", False):
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(
                f"clusters={out['clusters_with_duplicates']} "
                f"merges={out['merge_operations']} "
                f"removed={out['papers_removed']} dry_run={out['dry_run']}"
            )
            for m in out["merges"]:
                print(f"keep_id={m['keep_id']}\tremove_id={m['remove_id']}")
            for err in out.get("errors") or []:
                print(err, file=sys.stderr)
        return 0

    if args.cmd == "search":
        conn = db.connect()
        rows = db.search_fts(conn, args.query, args.limit)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for r in rows:
                aid = r.get("arxiv_id") or ""
                title = (r.get("title") or "").replace("\n", " ")
                print(f"{aid}\t{title}")
        return 0

    if args.cmd == "import-cache":
        from research_library.arxiv_keywords import load_cache

        conn = db.connect()
        entries = load_cache()
        n = db.import_cache_json(conn, entries)
        print(
            json.dumps({"imported": n, "cache_entries": len(entries)}, ensure_ascii=False)
        )
        return 0

    if args.cmd == "bib-export":
        from research_library.library import bib_export as be

        argv_be: List[str] = []
        bi = getattr(args, "bib_input", None)
        if bi:
            argv_be.append(bi)
        tx = getattr(args, "text", None) or ""
        if tx:
            argv_be.extend(["--text", tx])
        if getattr(args, "no_ingest", False):
            argv_be.append("--no-ingest")
        if getattr(args, "json", False):
            argv_be.append("--json")
        return be.main(argv_be)

    if args.cmd == "ingest-ref":
        from research_library.library.reference_acquire import acquire_pdf
        from research_library.library.reference_ingest import ingest_downloaded_reference
        from research_library.library.reference_parse import parse_catalog_line

        line = (getattr(args, "text", None) or "").strip()
        if not line:
            print("ingest-ref: provide --text '…'", file=sys.stderr)
            return 1
        conn = db.connect()
        ref = parse_catalog_line(line, conn, use_ads=True)
        if not ref.bibcode and not ref.arxiv_id:
            out = {
                "ok": False,
                "error": "Could not resolve bibcode or arXiv",
                "resolution_note": ref.resolution_note,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2 if args.json else None))
            return 1
        key = ref.bibcode or ref.arxiv_id or "paper"
        safe_name = re.sub(r"[^\w\-]", "_", str(key))
        dest = os.path.join(str(get_pdfs_dir()), f"{safe_name}.pdf")
        if getattr(args, "skip_download", False):
            if not os.path.isfile(dest):
                out = {"ok": False, "error": "PDF missing", "expected": dest}
                print(json.dumps(out, ensure_ascii=False, indent=2 if args.json else None))
                return 1
            path, acquire_reason = dest, "skip_download_existing"
        else:
            path, acquire_reason = acquire_pdf(ref, conn, dest, timeout=args.timeout)
        if not path:
            out = {
                "ok": False,
                "error": "acquire_pdf failed",
                "reason": acquire_reason,
                "bibcode": ref.bibcode,
            }
            print(json.dumps(out, ensure_ascii=False, indent=2 if args.json else None))
            return 1
        ingest_meta = ingest_downloaded_reference(
            conn, ref, path, source="cli_ingest_ref", acquire_reason=acquire_reason
        )
        conn.commit()
        out = {
            "ok": bool(ingest_meta.get("ok")),
            "pdf_path": path,
            "acquire_reason": acquire_reason,
            "bibcode": ref.bibcode,
            "ingest": ingest_meta,
        }
        if getattr(args, "json", False):
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(path)
            if not ingest_meta.get("ok"):
                print(json.dumps(ingest_meta, ensure_ascii=False), file=sys.stderr)
                return 1
        return 0 if ingest_meta.get("ok") else 1

    if args.cmd == "ingest-pdf":
        from research_library.library.pdf_ingest import (
            ingest_pdf_file,
            preresolved_from_manual_bibcode,
        )
        from research_library.library.reference_parse import strip_arxiv_version

        conn = db.connect()
        symlink_pdf = bool(args.symlink_to_pdfs)

        extracted_override: dict[str, str] = {}
        if getattr(args, "doi", None):
            d = str(args.doi).strip()
            if d.lower().startswith("doi:"):
                d = d[4:].strip()
            if d:
                extracted_override["doi"] = d
        if getattr(args, "arxiv", None):
            ax = strip_arxiv_version(str(args.arxiv).strip())
            if ax:
                extracted_override["arxiv_id"] = ax
        if getattr(args, "match_title", None):
            t = str(args.match_title).strip()
            if t:
                extracted_override["title_candidate"] = t

        preresolved = None
        bc_manual = getattr(args, "bibcode", None)
        if bc_manual and str(bc_manual).strip():
            preresolved = preresolved_from_manual_bibcode(str(bc_manual).strip())

        out = ingest_pdf_file(
            conn,
            args.pdf_path,
            dry_run=bool(args.dry_run),
            require_strong_id=True if args.require_strong_id else None,
            title_rows=max(1, int(args.title_rows)),
            copy_to_pdfs=(not bool(args.no_copy_to_pdfs)) and not symlink_pdf,
            symlink_to_pdfs=symlink_pdf,
            source="cli_library_ingest_pdf",
            sync_references=(False if getattr(args, "no_sync_references", False) else None),
            preresolved=preresolved,
            extracted_override=extracted_override if extracted_override else None,
        )
        conn.commit()
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            if out.get("ok"):
                print(
                    f"paper_id={out.get('paper_id')} bibcode={out.get('bibcode')} "
                    f"match={out.get('match_method')} pdf_relpath={out.get('pdf_relpath') or out.get('pdf_relpath_would_be')}"
                )
            else:
                print(out.get("error") or "failed", file=sys.stderr)
        return 0 if out.get("ok") else 1

    if args.cmd == "citation":
        from research_library.library import citations as cit

        extra = list(getattr(args, "citation_args", None) or [])
        if extra and extra[0] == "--":
            extra = extra[1:]
        return cit.main(extra)

    if args.cmd == "fetch-source":
        from research_library.library.tex_to_text import fetch_source_for_paper

        conn = db.connect()
        ids = getattr(args, "paper_ids", None)
        force = bool(getattr(args, "force", False))
        backend = (getattr(args, "backend", "") or "").strip() or None
        if ids:
            target_ids: List[int] = list(ids)
        else:
            target_ids = db.list_paper_ids_with_arxiv(conn)
        items = []
        errors = 0
        for pid in target_ids:
            try:
                r = fetch_source_for_paper(conn, int(pid), backend=backend, force=force)
            except Exception as e:
                r = {"ok": False, "paper_id": int(pid), "error": str(e)}
            items.append(r)
            if not r.get("ok"):
                errors += 1
        out = {
            "requested": len(target_ids),
            "errors": errors,
            "backend_default": backend or os.environ.get("RESEARCH_TEX_BACKEND") or "ar5iv",
            "items": items,
        }
        if getattr(args, "json", False):
            print(json.dumps(out, ensure_ascii=False))
        else:
            ok = sum(1 for it in items if it.get("ok"))
            print(json.dumps({k: v for k, v in out.items() if k != "items"}, ensure_ascii=False, indent=2))
            print(f"ok={ok} errors={errors} total={len(target_ids)}", file=sys.stderr)
        return 0 if errors == 0 else 1

    if args.cmd == "update-pdf":
        from research_library.library.pdf_update import update_pdfs

        conn = db.connect()
        ids = getattr(args, "paper_ids", None)
        if ids:
            target_ids = list(ids)
        else:
            target_ids = db.list_paper_ids_with_pdf(conn)
        out = update_pdfs(
            conn,
            target_ids,
            source=getattr(args, "source", "auto"),
            timeout=int(getattr(args, "timeout", 120)),
            reindex=bool(getattr(args, "reindex", False)),
        )
        if getattr(args, "json", False):
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(
                json.dumps(
                    {k: v for k, v in out.items() if k != "items"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            ok = sum(1 for it in out["items"] if it.get("ok"))
            print(
                f"ok={ok} errors={out['errors']} total={out['requested']}",
                file=sys.stderr,
            )
        return 0 if out.get("errors", 0) == 0 else 1

    if args.cmd == "reembed-from-source":
        from research_library.library.semantic import index_paper
        from research_library.library.tex_to_text import fetch_source_for_paper

        conn = db.connect()
        ids = getattr(args, "paper_ids", None)
        force = bool(getattr(args, "force", False))
        backend = (getattr(args, "backend", "") or "").strip() or None
        if ids:
            target_ids = list(ids)
        else:
            target_ids = db.list_paper_ids_with_arxiv(conn)
        items = []
        errors = 0
        for pid in target_ids:
            entry: dict = {"paper_id": int(pid)}
            try:
                fetched = fetch_source_for_paper(
                    conn, int(pid), backend=backend, force=force
                )
            except Exception as e:
                fetched = {"ok": False, "paper_id": int(pid), "error": str(e)}
            entry["fetch"] = fetched
            if fetched.get("ok"):
                try:
                    entry["index"] = index_paper(conn, int(pid), force=True)
                except Exception as e:
                    entry["index"] = {"ok": False, "error": str(e)}
            entry["ok"] = bool(fetched.get("ok") and entry.get("index", {}).get("ok", True))
            if not entry["ok"]:
                errors += 1
            items.append(entry)
        out = {"requested": len(target_ids), "errors": errors, "items": items}
        if getattr(args, "json", False):
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(
                json.dumps(
                    {k: v for k, v in out.items() if k != "items"},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            ok = sum(1 for it in items if it.get("ok"))
            print(f"ok={ok} errors={errors} total={len(target_ids)}", file=sys.stderr)
        return 0 if errors == 0 else 1

    if args.cmd == "semantic-index":
        from research_library.library.semantic import index_papers

        conn = db.connect()
        ids = getattr(args, "paper_ids", None)
        force = bool(getattr(args, "force", False))
        only_missing = bool(getattr(args, "only_missing", False))
        if ids:
            target = ids
        elif only_missing:
            target = db.list_paper_ids_with_pdf_missing_chunks(conn)
        else:
            target = None
        out = index_papers(conn, target, force=force)
        if getattr(args, "json", False):
            print(json.dumps(out, ensure_ascii=False))
        else:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get("errors", 0) == 0 else 1

    if args.cmd == "semantic-search":
        from research_library.library.semantic import semantic_search

        conn = db.connect()
        rows = semantic_search(conn, args.query, limit=args.limit)
        if getattr(args, "json", False):
            print(json.dumps(rows, ensure_ascii=False))
        else:
            for r in rows:
                print(
                    f"{r.get('paper_id')}\t{r.get('distance', 0):.4f}\t"
                    f"{(r.get('title') or '')[:80]}"
                )
        return 0

    if args.cmd == "find":
        from research_library.library import search as libsearch

        out = libsearch.search_papers(
            args.query,
            limit_local=getattr(args, "limit_local", 15),
            limit_remote=getattr(args, "limit_remote", 10),
            force_remote=bool(getattr(args, "remote_only", False)),
            include_remote_when_local=bool(getattr(args, "also_remote", False)),
        )
        if getattr(args, "json", False):
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for row in out.get("local") or []:
                t = (row.get("title") or "")[:72]
                bc = row.get("bibcode") or ""
                pdf = row.get("pdf_abspath") or row.get("pdf_relpath") or ""
                print(f"[local]\t{bc}\t{t}\t{pdf}")
            for row in out.get("remote") or []:
                t = (row.get("title") or "")[:72]
                bc = row.get("bibcode") or ""
                src = row.get("source") or ""
                print(f"[{src}]\t{bc}\t{t}")
            if out.get("tier") == "none" and not out.get("remote"):
                print("(no results)", file=sys.stderr)
        return 0 if (out.get("local") or out.get("remote")) else 1

    if args.cmd == "topic-dossier":
        from research_library.library import topic_dossier as tdoc

        conn = db.connect()
        ex = list(getattr(args, "extra_queries", None) or [])
        out = tdoc.build_topic_dossier(
            conn,
            args.topic,
            extra_queries=ex or None,
            per_query_limit=int(getattr(args, "per_query_limit", 12)),
            synthesize=not bool(getattr(args, "no_synth", False)),
        )
        if getattr(args, "json", False):
            payload = {
                "topic": out.get("topic"),
                "gather": out.get("gather"),
                "n_chunks": len(out.get("chunks") or []),
                "markdown": out.get("markdown") or "",
                "chunks": out.get("chunks") or [],
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(out.get("markdown") or "(no markdown; use --json for chunks)", file=sys.stderr)
            if out.get("markdown"):
                print(out["markdown"])
        return 0

    if args.cmd == "semantic-report":
        from research_library.library import report as rep

        conn = db.connect()
        ovr = (getattr(args, "semantic_backend", "") or "").strip() or None
        out = rep.build_semantic_report(
            conn,
            args.query,
            extra_queries=list(getattr(args, "extra_queries", None) or []) or None,
            expand_queries=bool(getattr(args, "expand_queries", False)),
            per_query_limit=int(getattr(args, "limit", 12)),
            ref_limit_per_paper=int(getattr(args, "refs_per_paper", 30)),
            semantic_backend=ovr,
            synthesize=not bool(getattr(args, "no_synth", False)),
            max_context_chars=int(getattr(args, "max_context_chars", 56000)),
        )
        if getattr(args, "json", False):
            payload = {
                "query": out.get("query"),
                "gather": out.get("gather"),
                "source_index": out.get("source_index"),
                "bundle_chars": out.get("bundle_chars"),
                "markdown": out.get("markdown") or "",
                "n_chunks": len(out.get("chunks") or []),
            }
            if out.get("chunks") is not None:
                payload["chunks"] = out["chunks"]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"chunks={len(out.get('chunks') or [])} bundle_chars={out.get('bundle_chars', 0)}",
                file=sys.stderr,
            )
            md = out.get("markdown") or ""
            if md:
                print(md)
            elif not bool(getattr(args, "no_synth", False)):
                print("(no markdown; check chunks / LLM)", file=sys.stderr)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
