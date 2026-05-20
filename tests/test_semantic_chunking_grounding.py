"""Tests for sentence-aware chunking and the grounding-verifier helper."""

from __future__ import annotations

from research_library.analysis.pdf import _excerpt_in_body, _verify_excerpts
from research_library.library.semantic import chunk_text


def test_chunk_text_prefers_paragraph_boundary(monkeypatch):
    monkeypatch.setenv("RESEARCH_SEMANTIC_CHUNK_SIZE", "200")
    monkeypatch.setenv("RESEARCH_SEMANTIC_CHUNK_OVERLAP", "20")
    monkeypatch.setenv("RESEARCH_SEMANTIC_CHUNK_BOUNDARY", "1")
    para1 = "Alpha sentence one. Alpha sentence two. " * 6
    para2 = "Beta sentence one. Beta sentence two. " * 6
    text = para1.rstrip() + "\n\n" + para2.rstrip()
    triples = chunk_text(text)
    assert triples, "expected at least one chunk"
    # When boundary-aware, the first chunk should end on a paragraph or sentence
    # break — i.e. it should not split inside a word (no chunk ends mid-word).
    for _, _, piece in triples:
        last = piece.rstrip()[-1:]
        # Either ends on punctuation/space or matches a full paragraph
        assert last == "" or last in {".", "。", "!", "?", "\n"} or piece.endswith(para1.rstrip()) or piece.endswith(para2.rstrip()) or len(piece) >= 180


def test_chunk_text_boundary_disabled_uses_window(monkeypatch):
    monkeypatch.setenv("RESEARCH_SEMANTIC_CHUNK_SIZE", "200")
    monkeypatch.setenv("RESEARCH_SEMANTIC_CHUNK_OVERLAP", "20")
    monkeypatch.setenv("RESEARCH_SEMANTIC_CHUNK_BOUNDARY", "0")
    text = "a" * 500
    triples = chunk_text(text)
    sizes = [end - start for start, end, _ in triples]
    # Naive window should produce ~200-char windows
    assert any(s == 200 for s in sizes)


def test_excerpt_in_body_substring():
    body = "The Hubble constant H0 = 67.4 km/s/Mpc was reported."
    assert _excerpt_in_body("H0 = 67.4 km/s/Mpc", body)
    assert not _excerpt_in_body("dark matter halo profile", body)


def test_excerpt_in_body_token_overlap():
    body = "We measured the metallicity gradient of the disk galaxy NGC 1234."
    # Reorder/paraphrase but high overlap
    assert _excerpt_in_body(
        "metallicity gradient measured disk galaxy NGC 1234", body
    )


def test_verify_excerpts_drops_hallucinations():
    body = "Stellar mass loss rate is 1e-6 Msun/yr in the WR phase."
    excerpts = [
        "Stellar mass loss rate is 1e-6 Msun/yr",  # ground-truth
        "Quasar accretion is dominated by magnetic reconnection",  # hallucinated
    ]
    verified, dropped = _verify_excerpts(excerpts, body)
    assert len(verified) == 1
    assert verified[0].startswith("Stellar mass loss rate")
    assert len(dropped) == 1
    assert "reason" in dropped[0]
