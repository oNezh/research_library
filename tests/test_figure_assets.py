"""Tests for figure_assets (extraction, mention matching, report attachment)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from research_library.library import db
from research_library.library.figure_assets import (
    attach_figures_to_report,
    detect_figure_mentions,
    extract_figures_from_ar5iv_html,
    match_figures_in_text,
    persist_figures_from_ar5iv_html,
    read_figures,
    write_figures,
    FigureRecord,
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    db.init_schema(c)
    db.ensure_schema(c)
    return c


_SAMPLE_AR5IV = """
<html><body><article class="ltx_document">
<figure id="fig:demo">
  <img src="assets/fig1.png" alt="plot"/>
  <figcaption><span class="ltx_tag_figure">Figure 1.</span> Stellar mass function.</figcaption>
</figure>
<figure>
  <img src="https://ar5iv.labs.arxiv.org/html/2101.00001/assets/fig2.jpg"/>
  <figcaption>Figure 2. Redshift distribution for the sample.</figcaption>
</figure>
</article></body></html>
"""


def test_extract_figures_from_ar5iv_html() -> None:
    figs = extract_figures_from_ar5iv_html(_SAMPLE_AR5IV, "2101.00001")
    assert len(figs) == 2
    assert figs[0].number == 1
    assert "mass function" in figs[0].caption
    assert figs[0].source_url.endswith("assets/fig1.png")
    assert figs[1].number == 2


def test_detect_and_match_figure_mentions() -> None:
    figs = extract_figures_from_ar5iv_html(_SAMPLE_AR5IV, "2101.00001")
    for f in figs:
        f.local_relpath = f"sources/1/figures/fig{f.index}.png"

    text = "We compare with Fig. 2 and discuss the trend."
    mentions = detect_figure_mentions(text)
    assert any(m[1] == 2 for m in mentions)

    matched = match_figures_in_text(text, figs)
    assert len(matched) == 1
    assert matched[0][1].number == 2

    block = "[Figure] Stellar mass function."
    matched2 = match_figures_in_text(block, figs)
    assert matched2[0][1].number == 1


def test_attach_figures_to_report(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    conn = _conn()
    conn.execute(
        """
        INSERT INTO papers (
            id, arxiv_id, bibcode, title, abstract, authors_json, categories_json,
            matched_keywords_json, published, source, pdf_relpath, created_at, updated_at
        ) VALUES (3, '2101.00001', '2021ApJ...1....1A', 'Demo', '', '[]', '[]', '[]', NULL, 't', NULL, 'a', 'a')
        """
    )
    conn.commit()

    figs = extract_figures_from_ar5iv_html(_SAMPLE_AR5IV, "2101.00001")
    fig_dir = tmp_path / "sources" / "3" / "figures"
    fig_dir.mkdir(parents=True)
    img = fig_dir / "fig1.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 20)
    figs[0].local_relpath = "sources/3/figures/fig1.png"
    write_figures(3, [figs[0]], arxiv_id="2101.00001")

    chunks = [
        {
            "paper_id": 3,
            "chunk_id": 1,
            "bibcode": "2021ApJ...1....1A",
            "snippet": "The mass function is shown in Fig. 1.",
            "title": "Demo",
        }
    ]

    def fake_ensure(conn, paper_id, *, force=False):
        return read_figures(paper_id)

    import research_library.library.figure_assets as fa

    monkeypatch.setattr(fa, "ensure_paper_figures", fake_ensure)

    out = attach_figures_to_report(conn, chunks, "# Report\n\nBody text.")
    assert "## Referenced figures" in out["markdown"]
    assert len(out["figures"]) == 1
    assert out["figures"][0]["number"] == 1
    assert str(img) in out["markdown"]


def test_persist_figures_writes_json(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))

    def fake_download(paper_id, figures, *, timeout=60):
        updated = []
        for f in figures:
            rec = FigureRecord(**f.to_dict())
            dest = tmp_path / "sources" / str(paper_id) / "figures" / f"fig{f.index}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"png")
            rec.local_relpath = f"sources/{paper_id}/figures/fig{f.index}.png"
            updated.append(rec)
        return updated

    import research_library.library.figure_assets as fa

    monkeypatch.setattr(fa, "download_figure_images", fake_download)

    rows = persist_figures_from_ar5iv_html(5, "2101.00001", _SAMPLE_AR5IV, force=True)
    assert len(rows) == 2
    p = tmp_path / "sources" / "5" / "figures.json"
    assert p.is_file()
    obj = json.loads(p.read_text())
    assert len(obj["figures"]) == 2
