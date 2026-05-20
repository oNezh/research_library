"""Backend dispatch for converting a paper's TeX source to clean prose.

The default backend is **ar5iv** (HTTP fetch of arXiv's LaTeXML-compiled HTML),
which avoids any local Perl install while equalling LaTeXML output quality.
Other backends operate on the e-print tarball pulled by :mod:`arxiv_source`:

* ``pylatexenc`` — pure Python ``LatexNodes2Text``; lightweight, decent quality.
* ``pandoc`` — subprocess; needs system ``pandoc`` binary.
* ``latexml`` — subprocess to ``latexmlc``; needs system Perl LaTeXML.

All non-ar5iv backends share the same TeX pre-clean (input expansion, comment
stripping, citation folding, section markers) before handing off to their
respective formatter.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from research_library.config import load_env
from research_library.log import log_event
from research_library.library.ar5iv_source import (
    Section,
    clean_ar5iv_html,
    fetch_ar5iv_html,
)
from research_library.library.arxiv_source import (
    ArxivSourceBundle,
    expand_inputs,
    fetch_arxiv_source,
)


class TexExtractError(RuntimeError):
    """Raised when a chosen backend cannot produce usable text."""


@dataclass
class CleanText:
    text: str
    sections: List[Section]
    backend: str
    arxiv_id: Optional[str] = None
    source_relpath: Optional[str] = None


_VALID_BACKENDS: Tuple[str, ...] = ("ar5iv", "pylatexenc", "pandoc", "latexml")
_TEX_BACKENDS: frozenset[str] = frozenset({"pylatexenc", "pandoc", "latexml"})


def resolve_backend(override: Optional[str] = None) -> str:
    load_env()
    raw = (override or os.environ.get("RESEARCH_TEX_BACKEND") or "ar5iv").strip().lower()
    if raw in _VALID_BACKENDS:
        return raw
    return "ar5iv"


def resolve_local_tex_backend(override: Optional[str] = None) -> str:
    """Backend for arXiv e-print tarball conversion (second step after ar5iv)."""
    load_env()
    raw = (
        override
        or os.environ.get("RESEARCH_TEX_LOCAL_BACKEND")
        or os.environ.get("RESEARCH_TEX_BACKEND")
        or "pylatexenc"
    ).strip().lower()
    if raw in _TEX_BACKENDS:
        return raw
    return "pylatexenc"


def tex_fallback_chain(
    *,
    allow_fallback: bool = True,
    local_backend: Optional[str] = None,
    primary: Optional[str] = None,
) -> List[str]:
    """Ordered TeX backends: ar5iv first, then local tarball cleaning."""
    local = resolve_local_tex_backend(local_backend)
    chosen = resolve_backend(primary)
    if not allow_fallback:
        return [chosen]
    chain: List[str] = []
    for be in ("ar5iv", local):
        if be not in chain:
            chain.append(be)
    return chain


_COMMENT_RE = re.compile(r"(?<!\\)%.*", re.MULTILINE)
_BIB_BLOCK_RE = re.compile(
    r"\\begin\{thebibliography\}.*?\\end\{thebibliography\}",
    re.DOTALL,
)
_BIBLIOGRAPHY_CMD_RE = re.compile(r"\\bibliography\{[^}]*\}")
_CITE_RE = re.compile(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])?\{([^}]+)\}")
_REF_RE = re.compile(r"\\(?:eq)?ref\*?\{([^}]+)\}")
_LABEL_RE = re.compile(r"\\label\{[^}]*\}")
_FOOTNOTE_RE = re.compile(r"\\footnote\{[^{}]*\}")
_SECTION_RE = re.compile(
    r"\\(section|subsection|subsubsection|chapter|paragraph)\*?\{([^}]+)\}"
)


def _preclean_tex(tex: str) -> Tuple[str, List[Section]]:
    """Strip pre-document, comments, bib block, fold cites/refs, mark sections.

    Returns ``(cleaned_tex, sections)`` where each section's ``char_start`` is
    relative to the cleaned text (post-pre-clean, pre-backend conversion).
    """
    doc = tex
    m = re.search(r"\\begin\{document\}", doc)
    if m:
        doc = doc[m.end():]
    end = re.search(r"\\end\{document\}", doc)
    if end:
        doc = doc[: end.start()]

    doc = _BIB_BLOCK_RE.sub(" ", doc)
    doc = _BIBLIOGRAPHY_CMD_RE.sub(" ", doc)
    doc = _COMMENT_RE.sub("", doc)
    doc = _FOOTNOTE_RE.sub(" ", doc)

    def _cite(match: "re.Match[str]") -> str:
        keys = [k.strip() for k in match.group(1).split(",") if k.strip()]
        if not keys:
            return ""
        return f"[CITE: {', '.join(keys)}]"

    doc = _CITE_RE.sub(_cite, doc)
    doc = _REF_RE.sub(lambda m: f"[REF: {m.group(1).strip()}]", doc)
    doc = _LABEL_RE.sub("", doc)

    sections: List[Section] = []
    parts: List[str] = []
    cursor = 0
    pos = 0
    for match in _SECTION_RE.finditer(doc):
        parts.append(doc[pos: match.start()])
        cursor += match.start() - pos
        title = re.sub(r"\s+", " ", match.group(2)).strip()
        level = {
            "chapter": 1,
            "section": 1,
            "subsection": 2,
            "subsubsection": 3,
            "paragraph": 4,
        }.get(match.group(1), 2)
        marker = f"\n\n# {title}\n\n"
        sections.append(Section(char_start=cursor, level=level, title=title[:200]))
        parts.append(marker)
        cursor += len(marker)
        pos = match.end()
    parts.append(doc[pos:])
    cleaned = "".join(parts)
    return cleaned, sections


def _backend_pylatexenc(cleaned_tex: str) -> str:
    try:
        from pylatexenc.latex2text import LatexNodes2Text  # type: ignore[import-not-found]
    except ImportError as e:
        raise TexExtractError(
            "pylatexenc backend requires the pylatexenc package "
            "(pip install pylatexenc)"
        ) from e
    converter = LatexNodes2Text(
        keep_braced_groups=False,
        math_mode="text",
        strict_latex_spaces=False,
    )
    try:
        return converter.latex_to_text(cleaned_tex)
    except Exception as e:  # pylatexenc raises bare Exception on parse trouble
        raise TexExtractError(f"pylatexenc conversion failed: {e}") from e


def _backend_pandoc(cleaned_tex: str) -> str:
    try:
        proc = subprocess.run(
            [
                "pandoc",
                "-f",
                "latex+raw_tex",
                "-t",
                "plain",
                "--wrap=none",
            ],
            input=cleaned_tex.encode("utf-8", errors="replace"),
            capture_output=True,
            timeout=180,
        )
    except FileNotFoundError as e:
        raise TexExtractError("pandoc binary not found in PATH") from e
    except subprocess.TimeoutExpired as e:
        raise TexExtractError("pandoc timed out") from e
    if proc.returncode != 0:
        raise TexExtractError(
            f"pandoc exit={proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:500]}"
        )
    out = proc.stdout.decode("utf-8", "replace").strip()
    if not out:
        raise TexExtractError("pandoc produced empty output")
    return out


def _backend_latexml(main_tex: Path) -> str:
    """Run ``latexmlc`` → HTML → reuse ar5iv cleaner."""
    try:
        proc = subprocess.run(
            [
                "latexmlc",
                "--quiet",
                "--dest=-",
                "--format=html5",
                str(main_tex),
            ],
            capture_output=True,
            timeout=600,
            cwd=str(main_tex.parent),
        )
    except FileNotFoundError as e:
        raise TexExtractError("latexmlc not found in PATH") from e
    except subprocess.TimeoutExpired as e:
        raise TexExtractError("latexmlc timed out") from e
    if proc.returncode != 0:
        raise TexExtractError(
            f"latexmlc exit={proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[:500]}"
        )
    html = proc.stdout.decode("utf-8", "replace")
    if not html.strip():
        raise TexExtractError("latexmlc produced empty output")
    cleaned = clean_ar5iv_html(html)
    if not cleaned.text.strip():
        raise TexExtractError("latexmlc HTML cleaner returned empty text")
    return cleaned.text  # Sections recomputed from headings in cleaner


def _ar5iv_clean_text(arxiv_id: str) -> Optional[CleanText]:
    html = fetch_ar5iv_html(arxiv_id)
    if not html:
        return None
    cleaned = clean_ar5iv_html(html)
    if not cleaned.text.strip():
        return None
    return CleanText(
        text=cleaned.text,
        sections=cleaned.sections,
        backend="ar5iv",
        arxiv_id=arxiv_id,
    )


def _tex_backend_clean_text(
    backend: str,
    bundle: ArxivSourceBundle,
) -> Optional[CleanText]:
    if bundle.is_pdf_only or bundle.main_tex is None:
        log_event(
            "tex_to_text.no_main_tex",
            arxiv_id=bundle.arxiv_id,
            is_pdf_only=bundle.is_pdf_only,
        )
        return None
    try:
        raw_tex = expand_inputs(bundle.main_tex)
    except OSError as e:
        log_event(
            "tex_to_text.input_expand_failed",
            arxiv_id=bundle.arxiv_id,
            error=str(e),
        )
        return None
    if not raw_tex.strip():
        return None
    cleaned_tex, sections = _preclean_tex(raw_tex)

    if backend == "pylatexenc":
        text = _backend_pylatexenc(cleaned_tex)
    elif backend == "pandoc":
        text = _backend_pandoc(cleaned_tex)
    elif backend == "latexml":
        text = _backend_latexml(bundle.main_tex)
    else:  # pragma: no cover - guarded by resolve_backend
        return None

    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    if not text.strip():
        return None

    # Re-anchor section char_starts in the converted text by searching for the
    # marker we injected; fall back to keeping the pre-clean cursor (best effort).
    final_sections: List[Section] = []
    cursor = 0
    for sec in sections:
        marker = f"# {sec.title}"
        idx = text.find(marker, cursor)
        if idx < 0:
            idx = text.find(marker)
        if idx < 0:
            continue
        final_sections.append(
            Section(char_start=idx, level=sec.level, title=sec.title)
        )
        cursor = idx + len(marker)
    return CleanText(
        text=text,
        sections=final_sections,
        backend=backend,
        arxiv_id=bundle.arxiv_id,
    )


def to_clean_text(
    arxiv_id: str,
    *,
    backend: Optional[str] = None,
    allow_fallback: bool = True,
    force_refresh: bool = False,
) -> Optional[CleanText]:
    """Convert ``arxiv_id``'s TeX to clean prose.

    With ``allow_fallback`` (default), tries ``ar5iv`` then the local tarball
    backend (``RESEARCH_TEX_LOCAL_BACKEND``, default ``pylatexenc``). Returns
    ``None`` if every TeX backend fails — callers should fall back to PDF text.
    """
    aid = (arxiv_id or "").strip()
    if not aid:
        return None
    chain = tex_fallback_chain(
        allow_fallback=allow_fallback,
        local_backend=backend if backend in _TEX_BACKENDS else None,
        primary=backend,
    )

    bundle: Optional[ArxivSourceBundle] = None
    last_error: Optional[BaseException] = None
    for be in chain:
        try:
            if be == "ar5iv":
                ct = _ar5iv_clean_text(aid)
                if ct is not None:
                    return ct
                continue
            if bundle is None:
                bundle = fetch_arxiv_source(aid, force=force_refresh)
            if bundle is None:
                continue
            ct = _tex_backend_clean_text(be, bundle)
            if ct is not None:
                if bundle is not None:
                    from research_library.library.arxiv_source import (
                        arxiv_source_relpath,
                    )

                    try:
                        ct.source_relpath = arxiv_source_relpath(bundle)
                    except ValueError:
                        ct.source_relpath = None
                return ct
        except TexExtractError as e:
            last_error = e
            log_event(
                "tex_to_text.backend_failed",
                backend=be,
                arxiv_id=aid,
                error=str(e),
            )
            continue
        except Exception as e:  # noqa: BLE001
            last_error = e
            log_event(
                "tex_to_text.backend_exception",
                backend=be,
                arxiv_id=aid,
                error=str(e),
            )
            continue
    if last_error is not None:
        print(
            f"[tex_to_text] all backends failed for {aid}: {last_error}",
            file=sys.stderr,
        )
    return None


def fetch_source_for_paper(
    conn,
    paper_id: int,
    *,
    backend: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Fetch + clean a paper's TeX source and persist text + DB pointer.

    Returns a structured dict suitable for CLI ``--json`` output.
    """
    from datetime import datetime, timezone

    from research_library.library import db as library_db
    from research_library.library.ar5iv_source import write_source_text

    library_db.ensure_schema(conn)
    paper = library_db.get_paper_row(conn, paper_id)
    if not paper:
        return {"ok": False, "paper_id": paper_id, "error": "paper_not_found"}

    arxiv_id = (paper.get("arxiv_id") or "").strip()
    if not arxiv_id:
        library_db.update_paper_source_metadata(
            conn,
            paper_id,
            source_kind="",
            source_backend="",
            clear_source_text_relpath=True,
            commit=True,
        )
        return {
            "ok": False,
            "paper_id": paper_id,
            "error": "no_arxiv_id",
            "source_kind": "",
        }

    existing_rel = (paper.get("source_text_relpath") or "").strip()
    if not force and existing_rel:
        return {
            "ok": True,
            "paper_id": paper_id,
            "skipped": True,
            "source_text_relpath": existing_rel,
            "source_kind": paper.get("source_kind"),
            "source_backend": paper.get("source_backend"),
        }

    ct = to_clean_text(arxiv_id, backend=backend, force_refresh=force)
    if ct is None:
        library_db.update_paper_source_metadata(
            conn,
            paper_id,
            source_kind="",
            source_backend="",
            clear_source_text_relpath=True,
            commit=True,
        )
        return {
            "ok": False,
            "paper_id": paper_id,
            "arxiv_id": arxiv_id,
            "error": "all_backends_failed",
        }

    rel, abs_path = write_source_text(
        paper_id,
        ct.text,
        ct.sections,
        backend=ct.backend,
        arxiv_id=arxiv_id,
    )
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    library_db.update_paper_source_metadata(
        conn,
        paper_id,
        source_text_relpath=rel,
        source_kind="tex",
        source_backend=ct.backend,
        arxiv_source_relpath=ct.source_relpath,
        source_fetched_at=fetched_at,
        commit=True,
    )
    return {
        "ok": True,
        "paper_id": paper_id,
        "arxiv_id": arxiv_id,
        "source_text_relpath": rel,
        "source_text_abspath": abs_path,
        "source_kind": "tex",
        "source_backend": ct.backend,
        "sections": len(ct.sections),
        "chars": len(ct.text),
    }
