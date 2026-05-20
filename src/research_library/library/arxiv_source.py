"""arXiv e-print tarball fetch + extract for local TeX-to-text backends.

Used only when ``RESEARCH_TEX_BACKEND`` is one of ``pylatexenc`` / ``pandoc`` /
``latexml``. The ar5iv default backend does **not** need this — it fetches
LaTeXML-compiled HTML directly.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import tarfile
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from research_library.config import get_data_dir, load_env
from research_library.log import log_event
from research_library.lookup import http_get_with_retry

ARXIV_EPRINT_URL = "https://arxiv.org/e-print"


@dataclass
class ArxivSourceBundle:
    arxiv_id: str
    source_path: Path
    extracted_dir: Path
    main_tex: Optional[Path]
    is_pdf_only: bool


def _strip_arxiv_version(arxiv_id: str) -> str:
    s = (arxiv_id or "").strip()
    if s.lower().startswith("arxiv:"):
        s = s[6:].strip()
    return re.sub(r"v\d+$", "", s, flags=re.IGNORECASE)


def arxiv_source_url(arxiv_id: str) -> str:
    return f"{ARXIV_EPRINT_URL}/{_strip_arxiv_version(arxiv_id)}"


def arxiv_sources_root() -> Path:
    d = get_data_dir() / "arxiv_sources"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _arxiv_sleep_seconds() -> float:
    load_env()
    raw = (os.environ.get("RESEARCH_ARXIV_SLEEP_SECONDS") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else 3.0
    except ValueError:
        return 3.0


def _looks_like_main_tex(path: Path, blob: bytes) -> int:
    """Heuristic score: higher = more likely to be the entry .tex."""
    if not path.suffix.lower() == ".tex":
        return -1
    score = 0
    try:
        text = blob.decode("utf-8", errors="replace")
    except Exception:
        return -1
    if "\\documentclass" in text:
        score += 50
    name = path.name.lower()
    if name in ("ms.tex", "main.tex", "paper.tex", "manuscript.tex"):
        score += 10
    if name.startswith(path.parent.name.lower()):
        score += 2
    score += min(len(text) // 5000, 10)
    return score


def _select_main_tex(extracted_dir: Path) -> Optional[Path]:
    candidates: List[tuple[int, Path]] = []
    for p in extracted_dir.rglob("*.tex"):
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        score = _looks_like_main_tex(p, blob)
        if score >= 0:
            candidates.append((score, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], len(str(x[1]))))
    return candidates[0][1]


def fetch_arxiv_source(
    arxiv_id: str,
    *,
    timeout: int = 90,
    sleep_seconds: Optional[float] = None,
    force: bool = False,
) -> Optional[ArxivSourceBundle]:
    """Download e-print archive, extract, return ``ArxivSourceBundle``.

    Returns ``None`` if the upload only contains a PDF (no TeX) or fetch fails.
    """
    aid = _strip_arxiv_version(arxiv_id)
    if not aid:
        return None
    work_dir = arxiv_sources_root() / aid
    work_dir.mkdir(parents=True, exist_ok=True)
    source_path = work_dir / "source"
    extracted_dir = work_dir / "extracted"

    if not force and source_path.is_file() and extracted_dir.is_dir():
        main_tex = _select_main_tex(extracted_dir)
        return ArxivSourceBundle(
            arxiv_id=aid,
            source_path=source_path,
            extracted_dir=extracted_dir,
            main_tex=main_tex,
            is_pdf_only=(main_tex is None) and any(extracted_dir.glob("*.pdf")),
        )

    delay = _arxiv_sleep_seconds() if sleep_seconds is None else max(0.0, sleep_seconds)
    if delay > 0:
        time.sleep(delay)

    url = arxiv_source_url(aid)
    try:
        body = http_get_with_retry(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        log_event("arxiv_source.http_error", arxiv_id=aid, code=e.code, url=url)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log_event("arxiv_source.network_error", arxiv_id=aid, error=str(e), url=url)
        return None

    if not body:
        return None
    source_path.write_bytes(body)

    is_pdf_only = body[:4] == b"%PDF"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    if is_pdf_only:
        (extracted_dir / f"{aid}.pdf").write_bytes(body)
        return ArxivSourceBundle(
            arxiv_id=aid,
            source_path=source_path,
            extracted_dir=extracted_dir,
            main_tex=None,
            is_pdf_only=True,
        )

    if body[:2] == b"\x1f\x8b":
        try:
            decompressed = gzip.decompress(body)
        except OSError as e:
            log_event("arxiv_source.gunzip_failed", arxiv_id=aid, error=str(e))
            return None
        try:
            tarfile.open(fileobj=io.BytesIO(decompressed)).extractall(extracted_dir)
        except tarfile.TarError:
            # Not a tar archive — treat as a single tex file.
            single = extracted_dir / f"{aid}.tex"
            single.write_bytes(decompressed)
    else:
        try:
            tarfile.open(fileobj=io.BytesIO(body)).extractall(extracted_dir)
        except tarfile.TarError:
            single = extracted_dir / f"{aid}.tex"
            single.write_bytes(body)

    main_tex = _select_main_tex(extracted_dir)
    return ArxivSourceBundle(
        arxiv_id=aid,
        source_path=source_path,
        extracted_dir=extracted_dir,
        main_tex=main_tex,
        is_pdf_only=False,
    )


def arxiv_source_relpath(bundle: ArxivSourceBundle) -> str:
    root = get_data_dir().resolve()
    return str(bundle.source_path.resolve().relative_to(root)).replace("\\", "/")


_INPUT_RE = re.compile(r"\\(?:input|include|subfile)\s*\{([^}]+)\}")


def expand_inputs(main_tex: Path, *, max_depth: int = 8) -> str:
    """Inline ``\\input{...}`` / ``\\include{...}`` recursively from ``main_tex``."""
    base = main_tex.parent
    seen: set[Path] = set()

    def load(path: Path, depth: int) -> str:
        try:
            resolved = path.resolve()
        except OSError:
            return ""
        if resolved in seen or depth > max_depth:
            return ""
        seen.add(resolved)
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

        def _sub(match: "re.Match[str]") -> str:
            target = match.group(1).strip()
            if not target:
                return ""
            candidates: List[Path] = []
            if target.endswith(".tex"):
                candidates.append(base / target)
            else:
                candidates.append(base / f"{target}.tex")
                candidates.append(base / target)
            for cand in candidates:
                if cand.is_file():
                    return load(cand, depth + 1)
            return ""

        return _INPUT_RE.sub(_sub, text)

    return load(main_tex, 0)
