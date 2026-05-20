"""Unit tests for reference-chain helpers in analysis.pdf."""

from __future__ import annotations

from research_library.analysis.pdf import (
    _chain_follow_combine,
    _narrow_ref_map_for_llm,
    _parse_sequential_refs,
    _split_concatenated_ref_block,
)


def test_chain_follow_combine_loose_union():
    assert _chain_follow_combine([1, 2], [2, 3], [4], tight=False) == [1, 2, 3, 4]


def test_chain_follow_combine_tight_intersect_hints():
    assert _chain_follow_combine([1, 2, 3], [2, 5], [3], tight=True) == [2, 3]


def test_chain_follow_combine_tight_no_hints_keeps_model():
    assert _chain_follow_combine([9, 1], [], [], tight=True) == [1, 9]


def test_split_concatenated_ref_glued_two_papers():
    s = (
        "Awad P., et al., 2025, A&A, 693, A69 Balbinot E., Santiago B. X., 2020, MNRAS, 416, 393"
    )
    parts = _split_concatenated_ref_block(s)
    assert len(parts) == 2
    assert "A&A, 693, A69" in parts[0]
    assert parts[1].startswith("Balbinot")


def test_split_concatenated_ref_single_paper_unchanged():
    s = "Smith J., 2020, ApJ, 896, 2"
    assert _split_concatenated_ref_block(s) == [s]


def test_parse_sequential_splits_inline_merge():
    sec = """REFERENCES
Foo A., 2019, AJ, 157, 10 Bar B., 2020, ApJ, 700, 100
"""
    m = _parse_sequential_refs(sec)
    assert len(m) >= 2
    assert any("Bar B." in v for v in m.values())


def test_narrow_remap_and_identity():
    ref_map = {i: f"Ref line {i}" for i in range(1, 6)}
    pm, inv = _narrow_ref_map_for_llm(
        ref_map, "question", trigger_above=10, max_in_prompt=80, must_pick_refs=12
    )
    assert pm == ref_map
    assert inv == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}

    pm2, inv2 = _narrow_ref_map_for_llm(
        ref_map,
        "Ref line 4 Smith 1999",
        trigger_above=3,
        max_in_prompt=3,
        must_pick_refs=2,
    )
    assert len(pm2) == 3
    assert set(inv2.values()).issubset(set(ref_map.keys()))
    assert set(pm2.keys()) == {1, 2, 3}
