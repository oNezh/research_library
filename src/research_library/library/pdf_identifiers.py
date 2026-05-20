"""Extract DOI / arXiv / title candidates from PDF text (pdfminer + strings)."""

from __future__ import annotations

import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple


def _doi_from_text(raw: str) -> Optional[str]:
    m = re.search(r"10\.\d{4,}/[^\s<>\"'\)\\\]]+", raw or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;:)")


def _split_front_matter(clean: Optional[str]) -> str:
    """Heading + abstract: cut before numbered INTRODUCTION or REFERENCES (pdfminer order)."""
    if not clean or not clean.strip():
        return ""
    for pat in (r"\n\s*\d+\s+introduction\b", r"\n\s*introduction\s*\n\s*\d"):
        m = re.search(pat, clean, re.IGNORECASE)
        if m and m.start() > 400:
            return clean[: m.start()]
    m = re.search(r"\n\s*references\s*\n", clean, re.IGNORECASE)
    if m and m.start() > 400:
        return clean[: m.start()]
    return clean[: min(len(clean), 60_000)]


_MASTHEAD_HINT = re.compile(
    r"MNRAS|A\&A|A&A|Monthly\s+Notices|Astronomy\s+and\s+Astrophysics|"
    r"Astron\.|^\s*ApJ\b|The\s+Astrophysical\s+Journal|^\s*Nature\b|^\s*Science\b",
    re.IGNORECASE,
)


def _authorish_line(ln: str) -> bool:
    if re.search(
        r"\b(Department|University|Laboratory|Observatory|Institute|College)\b",
        ln,
        re.I,
    ):
        return True
    if re.search(r"People.s\s+Republic|Republic\s+of\s+China|\bUSA\b|\bJapan\b", ln):
        return True
    if ln.count(",") >= 4 and re.search(r"\d+[,:]", ln):
        return True
    if re.match(r"^[A-Z][a-z]+\s+[A-Z][a-z]+", ln) and re.search(
        r"\d{1,2}[,★]?\d", ln
    ):
        return True
    return False


def _title_from_front_matter(front: str) -> Optional[str]:
    """Title lines after journal masthead (avoids first-match arXiv/DOI from bibliography in raw strings)."""
    if not front or not front.strip():
        return None
    lines = [ln.strip() for ln in front.splitlines() if ln.strip()]
    start: Optional[int] = None
    for i, ln in enumerate(lines):
        if _MASTHEAD_HINT.search(ln) and not re.match(r"^Compiled\b", ln, re.I):
            start = i + 1
            break
    if start is None:
        return None
    block: List[str] = []
    for j in range(start, min(start + 10, len(lines))):
        ln = lines[j]
        if re.match(
            r"^(Preprint|Compiled|Received|Accepted|Revised|Key\s+words)\b",
            ln,
            re.I,
        ):
            continue
        if len(ln) < 6:
            continue
        if _authorish_line(ln):
            break
        block.append(ln)
    if not block:
        return None
    title = re.sub(r"\s+", " ", " ".join(block).strip())
    if 25 <= len(title) <= 520:
        return title
    return None


def _arxiv_from_text(raw: str) -> Optional[str]:
    if not raw:
        return None
    for pat in (
        r"arXiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)",
        r"arxiv\.org/abs/(\d{4}\.\d{4,5}(?:v\d+)?)",
        r"doi:\s*10\.48550/(?:arXiv\.)?(\d{4}\.\d{4,5}(?:v\d+)?)",
    ):
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            aid = m.group(1)
            return re.sub(r"v\d+$", "", aid, flags=re.IGNORECASE)
    m = re.search(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", raw)
    if m and raw[max(0, m.start() - 25) : m.start()].lower().rstrip().endswith("arxiv"):
        aid = m.group(1)
        return re.sub(r"v\d+$", "", aid, flags=re.IGNORECASE)
    return None


def _title_candidate_from_clean_text(clean_text: Optional[str]) -> Optional[str]:
    if not clean_text or not clean_text.strip():
        return None
    lines = [l.strip() for l in clean_text.splitlines() if 10 < len(l.strip()) < 200]
    for line in lines[:100]:
        words = line.split()
        if not (4 <= len(words) <= 35):
            continue
        if re.match(r"^[A-Z][a-z]+(\s+[A-Z][a-z]+)*\s+[A-Z]", line) and not line[0].isdigit():
            if re.search(r"\(\d{4}\)|et al\.|^\d+\s+[A-Z]", line):
                continue
            clean_line = re.sub(r"\s+", " ", line).strip()
            clean_line = re.sub(r"\s+[A&E]&&,\s+\d+.*$", "", clean_line)
            if 10 < len(clean_line) < 200:
                return clean_line
    return None


def read_pdf_text_layers(pdf_path: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Return (pdfminer_clean_text_or_none, strings_raw_stdout, note)."""
    clean: Optional[str] = None
    note: Optional[str] = None
    try:
        from pdfminer.high_level import extract_text

        clean = extract_text(pdf_path) or ""
        if not clean.strip():
            clean = None
    except Exception as e:
        note = f"pdfminer:{e}"
        clean = None

    raw = ""
    try:
        result = subprocess.run(
            ["strings", pdf_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout or ""
    except Exception as e:
        if note:
            note = f"{note};strings:{e}"
        else:
            note = f"strings:{e}"

    return clean, raw, note


def extract_pdf_identifiers(
    pdf_path: str, *, include_clean_text: bool = False
) -> Dict[str, Any]:
    """Collect DOI, arXiv id, title heuristic from a PDF file."""
    clean, raw, note = read_pdf_text_layers(pdf_path)
    front = _split_front_matter(clean)
    mast_title = _title_from_front_matter(front)
    title = mast_title or _title_candidate_from_clean_text(clean)

    doi = _doi_from_text(front)
    if not doi and clean:
        mref = re.search(r"\n\s*references\s*\n", clean, re.IGNORECASE)
        head = clean[: mref.start()] if mref else clean[:200_000]
        doi = _doi_from_text(head)
    if not doi and not mast_title:
        doi = _doi_from_text(raw)

    arxiv_id = _arxiv_from_text(front)
    if not arxiv_id and clean:
        mref = re.search(r"\n\s*references\s*\n", clean, re.IGNORECASE)
        head = clean[: mref.start()] if mref else clean[:200_000]
        arxiv_id = _arxiv_from_text(head)
    if not arxiv_id and not mast_title:
        arxiv_id = _arxiv_from_text(raw)

    out: Dict[str, Any] = {
        "doi": doi,
        "arxiv_id": arxiv_id,
        "title_candidate": title,
        "clean_text_len": len(clean or ""),
        "raw_strings_len": len(raw or ""),
        "extraction_note": note,
    }
    if include_clean_text:
        out["_clean_text"] = clean
    return out
