"""Figure metadata + image files for papers; attach cited figures to reports."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin

from research_library.config import get_data_dir
from research_library.library.ar5iv_source import AR5IV_BASE_URL, _collapse_ws, ar5iv_url
from research_library.lookup import http_get_with_retry

_FIGURE_BLOCK_RE = re.compile(r"\[Figure\]\s*([^\n\[]+)", re.IGNORECASE)
_FIGURE_NUM_RE = re.compile(
    r"(?<!\w)(?:Figure|Fig\.?|图|圖)\s*(\d+)(?!\d)",
    re.IGNORECASE,
)


@dataclass
class FigureRecord:
    index: int
    number: Optional[int] = None
    label: str = ""
    caption: str = ""
    source_url: str = ""
    local_relpath: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FigureRecord":
        return cls(
            index=int(d.get("index", 0) or 0),
            number=int(d["number"]) if d.get("number") is not None else None,
            label=str(d.get("label") or ""),
            caption=str(d.get("caption") or ""),
            source_url=str(d.get("source_url") or ""),
            local_relpath=d.get("local_relpath"),
        )


@dataclass
class MatchedFigure:
    tag: str
    paper_id: int
    bibcode: Optional[str]
    mention: str
    figure: FigureRecord

    def to_dict(self) -> Dict[str, Any]:
        d = self.figure.to_dict()
        d.update(
            {
                "tag": self.tag,
                "paper_id": self.paper_id,
                "bibcode": self.bibcode,
                "mention": self.mention,
            }
        )
        return d


def figures_json_path(paper_id: int) -> Path:
    return get_data_dir() / "sources" / str(int(paper_id)) / "figures.json"


def figures_dir(paper_id: int) -> Path:
    d = get_data_dir() / "sources" / str(int(paper_id)) / "figures"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_figures(paper_id: int) -> List[FigureRecord]:
    p = figures_json_path(paper_id)
    if not p.is_file():
        return []
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = obj.get("figures") if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return []
    out: List[FigureRecord] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(FigureRecord.from_dict(row))
    return out


def write_figures(paper_id: int, figures: List[FigureRecord], *, arxiv_id: Optional[str] = None) -> None:
    p = figures_json_path(paper_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "arxiv_id": arxiv_id,
        "figures": [f.to_dict() for f in figures],
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _bs4():
    try:
        from bs4 import BeautifulSoup  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "figure extraction needs beautifulsoup4. Install: pip install beautifulsoup4"
        ) from e
    return BeautifulSoup


def resolve_ar5iv_image_url(src: str, arxiv_id: str) -> str:
    s = (src or "").strip()
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("//"):
        return "https:" + s
    if s.startswith("/"):
        return f"{AR5IV_BASE_URL}{s}"
    return urljoin(ar5iv_url(arxiv_id) + "/", s)


def extract_figures_from_ar5iv_html(html: str, arxiv_id: str) -> List[FigureRecord]:
    """Parse ar5iv HTML before text cleaning; keep img URLs + captions."""
    BeautifulSoup = _bs4()
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article", class_="ltx_document") or soup.body or soup
    if root is None:
        return []

    figures: List[FigureRecord] = []
    idx = 0
    for fig in root.find_all("figure"):
        cap_el = fig.find(["figcaption", "caption"])
        caption = _collapse_ws(cap_el.get_text(" ", strip=True)) if cap_el else ""
        tag_el = fig.find(class_=re.compile(r"ltx_tag_figure"))
        tag_text = _collapse_ws(tag_el.get_text(" ", strip=True)) if tag_el else ""
        number: Optional[int] = None
        for src in (tag_text, caption[:40]):
            m = re.search(r"(?:Figure|Fig\.?|图|圖)\s*(\d+)", src, re.IGNORECASE)
            if m:
                number = int(m.group(1))
                break
        img = fig.find("img")
        src = (img.get("src") or "").strip() if img is not None else ""
        url = resolve_ar5iv_image_url(src, arxiv_id) if src else ""
        label = (fig.get("id") or "").strip()
        if not url and not caption:
            continue
        idx += 1
        figures.append(
            FigureRecord(
                index=idx,
                number=number if number is not None else idx,
                label=label,
                caption=caption,
                source_url=url,
            )
        )
    return figures


def _guess_ext(url: str, body: bytes) -> str:
    low = url.lower().split("?", 1)[0]
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        if low.endswith(ext):
            return ext.lstrip(".")
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if body[:3] == b"\xff\xd8\xff":
        return "jpg"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "png"


def download_figure_images(
    paper_id: int,
    figures: List[FigureRecord],
    *,
    timeout: int = 60,
) -> List[FigureRecord]:
    """Download remote figure URLs into ``data/sources/<paper_id>/figures/``."""
    out_dir = figures_dir(paper_id)
    data_root = get_data_dir().resolve()
    updated: List[FigureRecord] = []
    for fig in figures:
        rec = FigureRecord(**fig.to_dict())
        if not rec.source_url:
            updated.append(rec)
            continue
        if rec.local_relpath:
            abs_p = (data_root / rec.local_relpath).resolve()
            if abs_p.is_file():
                updated.append(rec)
                continue
        try:
            body = http_get_with_retry(rec.source_url, timeout=timeout)
        except Exception:
            updated.append(rec)
            continue
        ext = _guess_ext(rec.source_url, body)
        fname = f"fig{fig.index}.{ext}"
        dest = out_dir / fname
        dest.write_bytes(body)
        rel = dest.resolve().relative_to(data_root)
        rec.local_relpath = str(rel).replace("\\", "/")
        updated.append(rec)
    return updated


def persist_figures_from_ar5iv_html(
    paper_id: int,
    arxiv_id: str,
    html: str,
    *,
    force: bool = False,
    download: bool = True,
) -> List[FigureRecord]:
    if not force and figures_json_path(paper_id).is_file():
        return read_figures(paper_id)
    figures = extract_figures_from_ar5iv_html(html, arxiv_id)
    if download and figures:
        figures = download_figure_images(paper_id, figures)
    write_figures(paper_id, figures, arxiv_id=arxiv_id)
    return figures


def ensure_paper_figures(
    conn: Any,
    paper_id: int,
    *,
    force: bool = False,
) -> List[FigureRecord]:
    """Load cached figures or fetch ar5iv HTML on demand."""
    if not force:
        existing = read_figures(paper_id)
        if existing:
            return existing

    from research_library.library import db as library_db
    from research_library.library.ar5iv_source import fetch_ar5iv_html

    paper = library_db.get_paper_row(conn, int(paper_id))
    if not paper:
        return []
    arxiv_id = (paper.get("arxiv_id") or "").strip()
    if not arxiv_id:
        return read_figures(paper_id)

    html = fetch_ar5iv_html(arxiv_id)
    if not html:
        return read_figures(paper_id)
    return persist_figures_from_ar5iv_html(
        int(paper_id), arxiv_id, html, force=True, download=True
    )


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def detect_figure_mentions(text: str) -> List[Tuple[str, Optional[int]]]:
    """Return ``(mention_text, figure_number_or_none)`` pairs found in *text*."""
    if not (text or "").strip():
        return []
    out: List[Tuple[str, Optional[int]]] = []
    seen: Set[str] = set()
    for m in _FIGURE_BLOCK_RE.finditer(text):
        cap = m.group(1).strip()
        if not cap:
            continue
        key = f"block:{_norm_text(cap)[:120]}"
        if key in seen:
            continue
        seen.add(key)
        num_m = re.search(r"(?:Figure|Fig\.?|图|圖)\s*(\d+)", cap, re.IGNORECASE)
        num = int(num_m.group(1)) if num_m else None
        out.append((f"[Figure] {cap[:200]}", num))
    for m in _FIGURE_NUM_RE.finditer(text):
        num = int(m.group(1))
        key = f"num:{num}"
        if key in seen:
            continue
        seen.add(key)
        out.append((m.group(0).strip(), num))
    return out


def _match_figure_by_caption(caption_mention: str, figures: List[FigureRecord]) -> Optional[FigureRecord]:
    raw = caption_mention
    if raw.lower().startswith("[figure]"):
        raw = raw[8:].strip()
    n = _norm_text(raw)
    if not n:
        return None
    best: Optional[FigureRecord] = None
    best_len = 0
    for fig in figures:
        fc = _norm_text(fig.caption)
        if not fc:
            continue
        if n in fc or fc.startswith(n[: min(len(n), 80)]):
            if len(fc) > best_len:
                best = fig
                best_len = len(fc)
        elif fc in n:
            if len(fc) > best_len:
                best = fig
                best_len = len(fc)
    return best


def _match_figure_by_number(number: int, figures: List[FigureRecord]) -> Optional[FigureRecord]:
    for fig in figures:
        if fig.number == number:
            return fig
    for fig in figures:
        if fig.index == number:
            return fig
    return None


def match_figures_in_text(
    text: str,
    figures: List[FigureRecord],
) -> List[Tuple[str, FigureRecord]]:
    """Map figure mentions in *text* to :class:`FigureRecord` rows."""
    if not figures or not (text or "").strip():
        return []
    matched: List[Tuple[str, FigureRecord]] = []
    used: Set[int] = set()
    for mention, num in detect_figure_mentions(text):
        fig: Optional[FigureRecord] = None
        if mention.lower().startswith("[figure]"):
            fig = _match_figure_by_caption(mention, figures)
        if fig is None and num is not None:
            fig = _match_figure_by_number(num, figures)
        if fig is None or fig.index in used:
            continue
        if not fig.local_relpath and not fig.source_url:
            continue
        used.add(fig.index)
        matched.append((mention, fig))
    return matched


def collect_figures_for_tagged_chunks(
    conn: Any,
    chunks: List[Dict[str, Any]],
    *,
    tag_prefix: str = "S",
) -> List[MatchedFigure]:
    """For each chunk (S1, S2, …), attach figures mentioned in its snippet."""
    out: List[MatchedFigure] = []
    seen: Set[Tuple[int, int]] = set()
    for i, chunk in enumerate(chunks, start=1):
        pid = int(chunk.get("paper_id", 0) or 0)
        if pid <= 0:
            continue
        snippet = (chunk.get("snippet") or "").strip()
        if not snippet:
            continue
        figures = ensure_paper_figures(conn, pid)
        if not figures:
            continue
        tag = f"{tag_prefix}{i}"
        bib = (chunk.get("bibcode") or "").strip() or None
        for mention, fig in match_figures_in_text(snippet, figures):
            key = (pid, fig.index)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                MatchedFigure(
                    tag=tag,
                    paper_id=pid,
                    bibcode=bib,
                    mention=mention,
                    figure=fig,
                )
            )
    return out


def format_figures_markdown(matched: List[MatchedFigure]) -> str:
    if not matched:
        return ""
    data_root = get_data_dir().resolve()
    lines = ["## Referenced figures", ""]
    for mf in matched:
        fig = mf.figure
        path = ""
        if fig.local_relpath:
            path = str((data_root / fig.local_relpath).resolve())
        elif fig.source_url:
            path = fig.source_url
        cap = (fig.caption or mf.mention).replace("\n", " ").strip()
        if len(cap) > 240:
            cap = cap[:237] + "…"
        bib_part = f", {mf.bibcode}" if mf.bibcode else ""
        lines.append(f"### {mf.tag} — {mf.mention} (paper_id={mf.paper_id}{bib_part})")
        if path and fig.local_relpath:
            lines.append(f"![{cap}]({path})")
        elif path:
            lines.append(f"[{cap}]({path})")
        elif cap:
            lines.append(f"*{cap}* (image unavailable)")
        lines.append("")
    return "\n".join(lines).rstrip()


def attach_figures_to_report(
    conn: Any,
    chunks: List[Dict[str, Any]],
    markdown: str,
    *,
    tag_prefix: str = "S",
) -> Dict[str, Any]:
    """Append a figures section when chunk text cites figures."""
    matched = collect_figures_for_tagged_chunks(conn, chunks, tag_prefix=tag_prefix)
    figures_md = format_figures_markdown(matched)
    combined = markdown
    if figures_md:
        combined = (markdown.rstrip() + "\n\n---\n\n" + figures_md) if markdown.strip() else figures_md
    return {
        "markdown": combined,
        "figures": [m.to_dict() for m in matched],
        "figures_markdown": figures_md,
    }
