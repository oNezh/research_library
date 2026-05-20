"""Heuristic METHOD-style ref numbers using ref_classifier signals (optional chain assist)."""

from __future__ import annotations

import os
import re
from typing import Dict, Set


def method_hint_ref_numbers(full_text: str, ref_map: Dict[int, str]) -> Set[int]:
    """Return reference indices whose bibliography line appears near METHOD-classified text.

    Opt-in from ``analyze_pdf_reference_chain`` via ``RESEARCH_PDF_CHAIN_METHOD_HINTS=1``.
    """
    if not full_text or not ref_map:
        return set()

    from research_library.ref_classifier import classify_context, detect_section_headings

    sections = detect_section_headings(full_text)
    span_lo, span_hi = 0, len(full_text)
    for sec_name, bounds in sections.items():
        if not isinstance(bounds, (tuple, list)) or len(bounds) < 2:
            continue
        sl = str(sec_name).lower()
        if any(k in sl for k in ("method", "observation", "data", "technique", "analysis")):
            span_lo, span_hi = int(bounds[0]), int(bounds[1])
            break

    haystack = full_text[span_lo:span_hi]
    if len(haystack) < 200:
        haystack = full_text

    out: Set[int] = set()
    for num, line in ref_map.items():
        ln = (line or "").strip()
        if len(ln) < 12:
            continue
        lead = ln.split(",")[0].strip()[:48]
        if len(lead) < 4:
            continue
        if any(c in lead for c in "[](){}*+?|^$\\"):
            continue
        for m in re.finditer(re.escape(lead), haystack):
            start = max(0, m.start() - 280)
            end = min(len(haystack), m.end() + 280)
            window = haystack[start:end]
            if classify_context(window) == "METHOD":
                out.add(num)
                break
    cap_raw = (os.environ.get("RESEARCH_PDF_CHAIN_METHOD_HINTS_MAX", "") or "").strip()
    try:
        cap = int(cap_raw) if cap_raw else 24
    except ValueError:
        cap = 24
    if len(out) > cap:
        out = set(sorted(out)[:cap])
    return out
