"""Fetch and clean ar5iv HTML (arXiv's LaTeXML-compiled view of every paper).

ar5iv URL pattern: ``https://ar5iv.labs.arxiv.org/html/<arxiv_id>``.
Compared with a local ``pylatexenc`` run on the e-print tarball, this gives us
the arXiv-blessed LaTeXML output (real macro expansion, structured tables, math
preserved as ``alttext`` on ``<math>`` tags, cite/refs rendered as author-year)
for zero local Perl install.

The cleaner emits prose with section markers (``# Title``) and writes a
``sections.json`` sidecar so the chunker can attach ``section`` metadata for
each chunk; figures are reduced to their captions, tables to a TSV-ish
text form, math to ``$...$`` from the ``alttext`` attribute.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from research_library.config import get_data_dir, load_env
from research_library.log import log_event
from research_library.lookup import http_get_with_retry

AR5IV_BASE_URL = "https://ar5iv.labs.arxiv.org/html"


@dataclass
class Section:
    char_start: int
    level: int
    title: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class CleanedHtml:
    text: str
    sections: List[Section]


def _strip_arxiv_version(arxiv_id: str) -> str:
    s = (arxiv_id or "").strip()
    if s.lower().startswith("arxiv:"):
        s = s[6:].strip()
    return re.sub(r"v\d+$", "", s, flags=re.IGNORECASE)


def ar5iv_url(arxiv_id: str) -> str:
    return f"{AR5IV_BASE_URL}/{_strip_arxiv_version(arxiv_id)}"


def _ar5iv_sleep_seconds() -> float:
    load_env()
    raw = (os.environ.get("RESEARCH_AR5IV_SLEEP_SECONDS") or "").strip()
    try:
        return max(0.0, float(raw)) if raw else 1.0
    except ValueError:
        return 1.0


def fetch_ar5iv_html(
    arxiv_id: str,
    *,
    timeout: int = 60,
    sleep_seconds: Optional[float] = None,
) -> Optional[str]:
    """GET ar5iv HTML. Returns body string on success, ``None`` on 404 / failure.

    A best-effort sleep is honored to stay polite to the ar5iv host; pass 0 to
    disable. Compilation errors detected in the body return ``None`` so the
    caller can fall back to a local TeX backend.
    """
    if not (arxiv_id or "").strip():
        return None
    url = ar5iv_url(arxiv_id)
    delay = _ar5iv_sleep_seconds() if sleep_seconds is None else max(0.0, sleep_seconds)
    if delay > 0:
        time.sleep(delay)
    try:
        body = http_get_with_retry(url, timeout=timeout)
    except urllib.error.HTTPError as e:
        log_event("ar5iv.http_error", arxiv_id=arxiv_id, code=e.code, url=url)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log_event("ar5iv.network_error", arxiv_id=arxiv_id, error=str(e), url=url)
        return None
    try:
        text = body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    if not text.strip():
        return None
    if "ltx_document" not in text:
        log_event("ar5iv.no_ltx_document", arxiv_id=arxiv_id)
        return None
    lower = text.lower()
    if "conversion to html had a failure" in lower or "class=\"conversion-errors\"" in lower:
        log_event("ar5iv.conversion_failed", arxiv_id=arxiv_id)
        return None
    return text


def _bs4():
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "ar5iv backend needs beautifulsoup4 (or lxml). "
            "Install: pip install beautifulsoup4"
        ) from e
    return BeautifulSoup


_DROP_SELECTORS: Tuple[str, ...] = (
    "footer",
    "nav",
    "header.ltx_page_header",
    "div.ltx_page_footer",
    "section.ltx_bibliography",
    "section#bib",
    "section.ltx_appendix.ltx_role_bibliography",
    "div.ltx_dates",
    "div.ltx_authors",  # author block already covered by title/abstract elsewhere
    "div.ltx_classification",
    "div.ltx_keywords",
    "ul.ltx_pagination",
    "span.ltx_tag_equation",
)


_HEADING_TAGS: Tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")


def _heading_level(tag_name: str) -> int:
    try:
        return max(1, int(tag_name[1]))
    except (ValueError, IndexError):
        return 1


def _math_to_text(node) -> str:  # type: ignore[no-untyped-def]
    """Prefer original LaTeX from ``alttext``/annotation; fall back to text."""
    alt = (node.get("alttext") or "").strip()
    if alt:
        return f" ${alt}$ "
    ann = node.find("annotation", attrs={"encoding": "application/x-tex"})
    if ann is not None and ann.get_text(strip=True):
        return f" ${ann.get_text(strip=True)}$ "
    txt = node.get_text(" ", strip=True)
    return f" {txt} " if txt else " "


def _normalize_table(node) -> str:  # type: ignore[no-untyped-def]
    rows: List[str] = []
    for tr in node.find_all("tr"):
        cells: List[str] = []
        for cell in tr.find_all(["td", "th"]):
            for math in cell.find_all("math"):
                math.replace_with(_math_to_text(math))
            text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
            cells.append(text)
        if any(cells):
            rows.append("\t".join(cells))
    return "\n".join(rows)


_WHITESPACE_RE = re.compile(r"[ \t]+")


def _collapse_ws(s: str) -> str:
    return _WHITESPACE_RE.sub(" ", s).strip()


def clean_ar5iv_html(html: str) -> CleanedHtml:
    """Turn an ar5iv HTML document into clean prose + section index."""
    BeautifulSoup = _bs4()
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article", class_="ltx_document") or soup.body or soup
    if root is None:
        return CleanedHtml(text="", sections=[])

    for selector in _DROP_SELECTORS:
        for el in root.select(selector):
            el.decompose()

    # Materialize math first so it survives later get_text walks.
    for math in root.find_all("math"):
        replacement = _math_to_text(math)
        math.replace_with(replacement)

    # Reduce figures to caption text, drop images entirely.
    for fig in root.find_all("figure"):
        cap = fig.find(["figcaption", "caption"])
        caption_text = ""
        if cap is not None:
            caption_text = _collapse_ws(cap.get_text(" ", strip=True))
        fig.clear()
        if caption_text:
            fig.append(soup.new_string(f"\n\n[Figure] {caption_text}\n\n"))

    for img in root.find_all("img"):
        img.decompose()

    # Tables → text blocks
    for table in root.find_all("table"):
        text = _normalize_table(table)
        if text:
            table.replace_with(soup.new_string(f"\n\n[Table]\n{text}\n\n"))
        else:
            table.decompose()

    # Walk in document order to produce text + record section boundaries.
    parts: List[str] = []
    sections: List[Section] = []
    cursor = 0

    def emit(piece: str) -> None:
        nonlocal cursor
        if not piece:
            return
        parts.append(piece)
        cursor += len(piece)

    seen_titles: set[int] = set()

    def walk(el):  # type: ignore[no-untyped-def]
        from bs4 import NavigableString, Tag

        if isinstance(el, NavigableString):
            text = str(el)
            text = re.sub(r"\s+", " ", text)
            emit(text)
            return
        if not isinstance(el, Tag):
            return
        name = (el.name or "").lower()
        if name in _HEADING_TAGS:
            title = _collapse_ws(el.get_text(" ", strip=True))
            if title and id(el) not in seen_titles:
                seen_titles.add(id(el))
                emit("\n\n")
                sections.append(
                    Section(
                        char_start=cursor,
                        level=_heading_level(name),
                        title=title[:200],
                    )
                )
                emit(f"# {title}\n\n")
            return
        if name in ("script", "style", "noscript"):
            return
        if name in ("br",):
            emit("\n")
            return
        if name in ("p", "div", "section", "article", "li"):
            for child in el.children:
                walk(child)
            emit("\n\n")
            return
        if name in ("ul", "ol"):
            for child in el.children:
                walk(child)
            emit("\n")
            return
        for child in el.children:
            walk(child)

    walk(root)

    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    # Recompute section char_start against the post-normalization text by
    # searching for the heading marker we emitted.
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

    return CleanedHtml(text=text, sections=final_sections)


def sources_dir_for_paper(paper_id: int) -> Path:
    d = get_data_dir() / "sources" / str(int(paper_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_source_text(
    paper_id: int,
    text: str,
    sections: List[Section],
    *,
    backend: str,
    arxiv_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Write ``main.txt`` and ``sections.json`` under ``data/sources/<paper_id>/``.

    Returns ``(text_relpath_posix, text_abs_path)``.
    """
    out_dir = sources_dir_for_paper(paper_id)
    text_path = out_dir / "main.txt"
    sections_path = out_dir / "sections.json"
    text_path.write_text(text, encoding="utf-8")
    sections_path.write_text(
        json.dumps(
            {
                "backend": backend,
                "arxiv_id": arxiv_id or None,
                "sections": [s.to_dict() for s in sections],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    root = get_data_dir().resolve()
    rel = text_path.resolve().relative_to(root)
    return str(rel).replace("\\", "/"), str(text_path)


def read_sections_for(paper_id: int) -> List[Section]:
    p = get_data_dir() / "sources" / str(int(paper_id)) / "sections.json"
    if not p.is_file():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    secs = obj.get("sections") if isinstance(obj, dict) else None
    if not isinstance(secs, list):
        return []
    out: List[Section] = []
    for s in secs:
        if not isinstance(s, dict):
            continue
        try:
            out.append(
                Section(
                    char_start=int(s.get("char_start", 0)),
                    level=int(s.get("level", 1)),
                    title=str(s.get("title", ""))[:200],
                )
            )
        except (TypeError, ValueError):
            continue
    return out
