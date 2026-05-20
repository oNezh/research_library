#!/usr/bin/env python3
"""Retry `library ingest-pdf` for FAIL lines in a batch log, using DOI/arXiv from a Zotero .bib.

Example:
  uv run python scripts/retry_failures_from_bib.py \\
    --bib "/path/导出的条目.bib" \\
    --files-root "/path/导出的条目/files" \\
    --log chain_runs/batch_zotero_ingest.log \\
    --sleep 0.2
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

FAIL_RE = re.compile(
    r"^\[\d+/\d+\] FAIL (?P<rel>.+?) :: .+$",
    re.MULTILINE,
)


def _split_bib_entries(text: str) -> List[str]:
    t = text.lstrip("\ufeff\n\r ")
    parts = re.split(r"\n(?=[@][a-zA-Z]+\{)", t)
    return [p.rstrip() for p in parts if p.lstrip().startswith("@")]


def _bib_field(block: str, name: str) -> Optional[str]:
    m = re.search(
        rf"(?m)^\s*{re.escape(name)}\s*=\s*\{{([^}}]*)\}}\s*,?",
        block,
    )
    if not m:
        return None
    return m.group(1).strip()


def _arxiv_from_block(block: str) -> Optional[str]:
    for name in ("url", "eprint"):
        v = _bib_field(block, name)
        if not v:
            continue
        m = re.search(r"arxiv\.org/(?:abs|pdf)/([^/\s]+)", v, re.I)
        if m:
            return re.sub(r"v\d+$", "", m.group(1).strip())
    note = _bib_field(block, "note") or ""
    m = re.search(r"arXiv:\s*([0-9]{4}\.[0-9]{4,5}|[0-9]{7})", note, re.I)
    if m:
        return m.group(1)
    return None


def _zotero_pdf_names(block: str) -> List[str]:
    raw = _bib_field(block, "file")
    if not raw:
        return []
    names: List[str] = []
    for part in raw.split(";"):
        p = part.strip()
        if p.upper().startswith("PDF:"):
            p = p[4:]
        elif ":" in p:
            p = p.split(":", 1)[1]
        low = p.lower()
        if low.endswith(":application/pdf"):
            p = p[: -len(":application/pdf")]
        base = p.rsplit("/", 1)[-1] if "/" in p else p
        if base.lower().endswith(".pdf"):
            names.append(base)
    return names


def _doi_clean(doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    d = doi.strip()
    if d.lower().startswith("doi:"):
        d = d[4:].strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.I)
    if d.startswith("10.48550/arXiv."):
        return None
    return d or None


def build_bib_map(bib_path: Path) -> Dict[str, Dict[str, str]]:
    text = bib_path.read_text(encoding="utf-8", errors="replace")
    out: Dict[str, Dict[str, str]] = {}
    for block in _split_bib_entries(text):
        doi = _doi_clean(_bib_field(block, "doi"))
        ax = _arxiv_from_block(block)
        for name in _zotero_pdf_names(block):
            key = name.casefold()
            rec: Dict[str, str] = {}
            if doi:
                rec["doi"] = doi
            if ax:
                rec["arxiv"] = ax
            if not rec:
                continue
            prev = out.get(key)
            if prev and prev != rec:
                pass
            out[key] = rec
    return out


def parse_fail_rels(log_text: str) -> List[str]:
    rels: List[str] = []
    for m in FAIL_RE.finditer(log_text):
        rels.append(m.group("rel").strip())
    return rels


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bib", type=Path, required=True)
    ap.add_argument("--files-root", type=Path, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    bib_map = build_bib_map(args.bib.expanduser().resolve())
    log_text = args.log.expanduser().resolve().read_text(
        encoding="utf-8", errors="replace"
    )
    root = args.files_root.expanduser().resolve()

    fails = parse_fail_rels(log_text)
    if not fails:
        print("no FAIL lines in log", file=sys.stderr)
        return 1

    n_ok = n_skip = n_miss = n_fail = 0
    for rel in fails:
        pdf = root / rel
        if not pdf.is_file():
            print(f"[missing] {rel}", file=sys.stderr)
            n_miss += 1
            continue
        name_key = pdf.name.casefold()
        meta = bib_map.get(name_key)
        if not meta:
            print(f"[no-bib] {rel}", file=sys.stderr)
            n_skip += 1
            continue
        cmd: List[str] = [
            sys.executable,
            "-m",
            "research_library.cli",
            "library",
            "ingest-pdf",
            str(pdf),
        ]
        if meta.get("doi"):
            cmd.extend(["--doi", meta["doi"]])
        elif meta.get("arxiv"):
            cmd.extend(["--arxiv", meta["arxiv"]])
        else:
            print(f"[no-id] {rel}", file=sys.stderr)
            n_skip += 1
            continue

        if args.dry_run:
            print("dry-run", " ".join(cmd))
            n_ok += 1
            continue

        r = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[1])
        if r.returncode == 0:
            n_ok += 1
        else:
            n_fail += 1
        if args.sleep > 0:
            time.sleep(args.sleep)

    print(
        json.dumps(
            {
                "fail_lines": len(fails),
                "reingested_ok": n_ok,
                "reingest_failed": n_fail,
                "pdf_missing": n_miss,
                "no_bib_or_id": n_skip,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
