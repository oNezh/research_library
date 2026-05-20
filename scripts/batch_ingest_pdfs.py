#!/usr/bin/env python3
"""Batch: library ingest-pdf (copy into data/pdfs) + optional semantic index for many PDFs.

Example:
  uv run python scripts/batch_ingest_pdfs.py "/path/to/root" 2>&1 | tee batch.log
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description="Ingest PDFs under a directory tree, then embed.")
    p.add_argument("root", help="Root folder (e.g. Zotero files/)")
    p.add_argument(
        "--sleep",
        type=float,
        default=0.15,
        help="Seconds between ADS ingest calls (rate limit cushion; default 0.15)",
    )
    p.add_argument(
        "--no-sync-references",
        action="store_true",
        help="Skip paper_references sync per paper (faster; run citation sync later)",
    )
    p.add_argument(
        "--no-semantic",
        action="store_true",
        help="Only ingest; skip embedding/index step",
    )
    p.add_argument(
        "--semantic-force",
        action="store_true",
        help="Rebuild chunks for indexed papers",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List PDFs only",
    )
    args = p.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    pdfs = sorted({p.resolve() for p in root.rglob("*.pdf") if p.is_file()})
    print(f"[batch] root={root} pdf_count={len(pdfs)}", flush=True)
    if args.dry_run:
        for x in pdfs:
            print(x)
        return 0

    from research_library.config import load_env

    load_env()

    from research_library.library import db as library_db
    from research_library.library.pdf_ingest import ingest_pdf_file
    from research_library.library.semantic import index_papers

    conn = library_db.connect()
    ok_ids: list[int] = []
    fail: list[dict[str, object]] = []

    t0 = time.perf_counter()
    for i, pdf in enumerate(pdfs, start=1):
        rel = pdf.relative_to(root) if pdf.is_relative_to(root) else pdf.name
        try:
            out = ingest_pdf_file(
                conn,
                str(pdf),
                dry_run=False,
                require_strong_id=None,
                title_rows=5,
                copy_to_pdfs=True,
                symlink_to_pdfs=False,
                source="batch_ingest_pdfs",
                sync_references=False if args.no_sync_references else None,
            )
            conn.commit()
        except Exception as e:
            fail.append({"path": str(pdf), "error": str(e)})
            print(f"[{i}/{len(pdfs)}] ERR {rel} :: {e}", flush=True)
            if args.sleep > 0:
                time.sleep(args.sleep)
            continue

        if out.get("ok") and out.get("paper_id") is not None:
            pid = int(out["paper_id"])
            ok_ids.append(pid)
            print(
                f"[{i}/{len(pdfs)}] OK paper_id={pid} bibcode={out.get('bibcode')} :: {rel}",
                flush=True,
            )
        else:
            fail.append({"path": str(pdf), "error": out.get("error"), "out": out})
            print(
                f"[{i}/{len(pdfs)}] FAIL {rel} :: {out.get('error')}",
                flush=True,
            )
        if args.sleep > 0:
            time.sleep(args.sleep)

    ingest_s = time.perf_counter() - t0
    summary: dict[str, object] = {
        "total_pdfs": len(pdfs),
        "ingested_ok": len(ok_ids),
        "ingest_failed": len(fail),
        "ingest_seconds": round(ingest_s, 2),
        "failures": fail[:50],
    }

    if not args.no_semantic and ok_ids:
        t1 = time.perf_counter()
        idx = index_papers(conn, ok_ids, force=bool(args.semantic_force))
        summary["semantic"] = {
            "indexed": idx.get("indexed"),
            "errors": idx.get("errors"),
            "backend": idx.get("backend"),
            "seconds": round(time.perf_counter() - t1, 2),
        }
        print(json.dumps(summary["semantic"], ensure_ascii=False), flush=True)

    summary["failures"] = fail
    out_path = root.parent / "batch_ingest_summary.json"
    try:
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[batch] summary -> {out_path}", flush=True)
    except OSError:
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

    return 0 if len(fail) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
