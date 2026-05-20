#!/usr/bin/env python3
"""
ref_classifier.py — Classify citations by type from a PDF.

Reads a PDF via pdfminer, extracts:
  - The reference list (from ADS or parsed from PDF text)
  - Citation contexts from the full text
  - Classifies each reference as:
      BACKGROUND  — cited in Introduction only, general context
      METHOD     — cited in Methods/Observations sections, or as basis of approach
      RESULT     — cited in Results/Discussion sections comparing/contrasting findings
      BACKGROUND+METHOD — appears in both intro and methods
      BACKGROUND+RESULT — appears in intro and results/discussion

Usage:
  python3 ref_classifier.py /path/to/paper.pdf --refs_json ' [{"bibcode":"...","authors":[...]}] '
  python3 ref_classifier.py /path/to/paper.pdf --bibcode 2024A&A...689A.225N
"""

import argparse
import json
import os
import re
import sys
import textwrap
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ─── PDF extraction ──────────────────────────────────────────────────────────

def extract_clean_text(pdf_path: str) -> str:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(pdf_path)
    except Exception as e:
        print(f"[ref_classifier] pdfminer failed: {e}", file=sys.stderr)
        return ""


def extract_refs_from_pdf(text: str) -> List[Dict]:
    """Extract reference list from the 'References' section of a PDF.
    Handles multi-line entries by accumulating lines starting with author name patterns.
    Returns list of dicts with: author (last name), year, full_text.
    """
    ref_start = text.lower().find("references")
    if ref_start < 0:
        return []
    ref_section = text[ref_start:ref_start + 20000]  # limit to avoid going too far

    lines = ref_section.splitlines()
    entries_raw = []
    current: List[str] = []

    for line in lines[1:]:  # skip 'References' header
        line_stripped = line.strip()
        if not line_stripped:
            continue
        # New entry starts with a capitalized author name pattern: "LastName, FirstInitial."
        if re.match(r"[A-Z][a-zA-Z\-'\.]+,\s+[A-Z]", line_stripped):
            if current:
                entries_raw.append(" ".join(current))
            current = [line_stripped]
        else:
            current.append(line_stripped)
    if current:
        entries_raw.append(" ".join(current))

    refs = []
    for entry in entries_raw:
        # Extract year: either "(YYYY)" or "YYYY" following author name
        year_m = re.search(r"\((\d{4}[a-z]?)\)|(?<![a-z])\b(20\d{2}|19\d{2})(?!\d)", entry)
        year = year_m.group(1) or year_m.group(2) if year_m else ""
        # Skip clearly bogus years (e.g., OCR artifact "1001")
        if year and (int(year[:4]) < 1900 or int(year[:4]) > 2026):
            continue
        # First author last name
        author_m = re.match(r"^([A-Z][a-zA-Z\-'\.]+),", entry)
        first_author = author_m.group(1) if author_m else ""
        if first_author and year:
            refs.append({
                "author": first_author,
                "year": year,
                "text": entry[:150]
            })
    return refs


# ─── ADS fetch ──────────────────────────────────────────────────────────────

ADS_API_URL = "https://api.adsabs.harvard.edu/v1/search/query"
USER_AGENT = "ref_classifier/1.0 (OpenClaw)"

def http_get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}"
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

def fetch_refs_from_ads(bibcode: str, token: str) -> List[Dict]:
    """Fetch reference list for a bibcode from ADS."""
    params = urllib.parse.urlencode({
        "q": f"bibcode:{bibcode}",
        "rows": "1",
        "fl": "bibcode,title,author,year,pub,reference",
    })
    url = f"{ADS_API_URL}?{params}"
    payload = http_get(url, token)
    result = json.loads(payload.decode("utf-8"))
    docs = result.get("response", {}).get("docs", [])
    if not docs:
        return []
    ref_bibcodes = docs[0].get("reference", [])
    return ref_bibcodes


def resolve_ref_titles(ref_bibcodes: List[str], token: str) -> Dict[str, Dict]:
    """Batch-resolve bibcode → title/author/year."""
    info_map = {}
    BATCH = 15
    for i in range(0, len(ref_bibcodes), BATCH):
        batch = ref_bibcodes[i:i+BATCH]
        q = " OR ".join(f'bibcode:"{b}"' for b in batch)
        params = urllib.parse.urlencode({"q": q, "rows": str(len(batch)), "fl": "bibcode,title,author,year,pub"})
        url = f"{ADS_API_URL}?{params}"
        try:
            payload = http_get(url, token)
            res = json.loads(payload.decode("utf-8"))
            for d in res.get("response", {}).get("docs", []):
                bc = d.get("bibcode", "")
                title_list = d.get("title", [])
                t = " ".join(title_list[0].split()) if title_list else ""
                info_map[bc] = {
                    "title": t,
                    "authors": d.get("author", [])[:3],
                    "year": d.get("year", ""),
                    "pub": d.get("pub", ""),
                }
        except Exception as e:
            print(f"[resolve] batch {i//BATCH+1} failed: {e}", file=sys.stderr)
            for bc in batch:
                info_map[bc] = {"title": "", "authors": [], "year": "", "pub": ""}
    return info_map


def resolve_refs_via_author_year(pdf_refs: List[Dict], token: str) -> Tuple[List[Dict], Dict[str, Dict]]:
    """Resolve PDF-parsed references (author+year) to ADS bibcodes via author-year search.

    Returns: (list of resolved refs with bibcodes, info_map)
    """
    resolved = []
    info_map: Dict[str, Dict] = {}
    BATCH = 8

    for i in range(0, len(pdf_refs), BATCH):
        batch = pdf_refs[i:i+BATCH]
        queries = []
        for ref in batch:
            author_q = f'author:"{ref["author"]}"'
            year_q = f'year:{ref["year"]}'
            queries.append(f"({author_q} AND {year_q})")

        big_q = " OR ".join(queries)
        try:
            params = urllib.parse.urlencode({
                "q": big_q, "rows": str(len(batch) * 4),
                "fl": "bibcode,title,author,year,pub", "sort": "score desc"
            })
            url = f"{ADS_API_URL}?{params}"
            payload = http_get(url, token)
            res = json.loads(payload.decode("utf-8"))
            ads_docs = {d.get("bibcode", ""): d for d in res.get("response", {}).get("docs", [])}
        except Exception as e:
            print(f"[resolve_refs_via_author_year] batch {i//BATCH+1} failed: {e}", file=sys.stderr)
            ads_docs = {}

        for ref in batch:
            best_bc = None
            best_info = None
            for bc, doc in ads_docs.items():
                doc_authors = doc.get("author", [])
                if doc_authors:
                    first = doc_authors[0].split(",")[0].strip() if "," in doc_authors[0] else doc_authors[0].split()[0]
                    if first == ref["author"]:
                        best_bc = bc
                        best_info = doc
                        break
            if best_bc:
                info_map[best_bc] = {
                    "title": " ".join(best_info.get("title", [""])[0].split()) if best_info.get("title") else "",
                    "authors": best_info.get("author", [])[:3],
                    "year": best_info.get("year", ""),
                    "pub": best_info.get("pub", ""),
                    "ads_url": f"https://ui.adsabs.harvard.edu/abs/{best_bc}/abstract"
                }
                resolved.append({
                    "author": ref["author"], "year": ref["year"],
                    "bibcode": best_bc, "text": ref.get("text", "")
                })

    return resolved, info_map


# ─── Citation extraction ─────────────────────────────────────────────────────

def build_author_year_patterns(ref_bibcodes: List[str], ref_info: Dict[str, Dict]) -> Dict[str, str]:
    """Build a mapping from short author-year strings to bibcodes.
    
    Handles patterns like:
      "Neha et al. 2016"
      "McKee & Ostriker 2007"
      "Soler et al. 2013"
      "Zhang et al. 1989"
    Also handles comma-style author list: "Garcia-Segura, G." → "Garcia-Segura"
    """
    patterns = {}
    for bc, info in ref_info.items():
        authors = info.get("authors", [])
        year = info.get("year", "")
        if not authors or not year:
            continue
        
        year_match = re.search(r"\d{4}", year)
        if not year_match:
            continue
        y = year_match.group()
        
        # Extract last names from both first and second author
        def last_name(author_str: str) -> str:
            # "Neha, S." → "Neha"
            # "Garcia-Segura, Guillermo" → "Garcia-Segura"
            # "McKee, Christopher F." → "McKee"
            author_str = author_str.strip()
            if "," in author_str:
                return author_str.split(",")[0].strip()
            return author_str.split()[0].strip()
        
        first = last_name(authors[0])
        second = last_name(authors[1]) if len(authors) > 1 else ""
        
        # Variations: "Neha et al. 2016" / "Neha et al.2016" / "Neha+ 2016"
        for sep in [" et al. ", " et al.", " et al ", " + ", " +"]:
            patterns[f"{first}{sep}{y}"] = bc
        
        # Two-author: "McKee & Ostriker 2007"
        if second:
            for amp in [" & ", " &", " and ", " and"]:
                patterns[f"{first}{amp}{second} {y}"] = bc
        
        # Single author: "Bertoldi 1989"
        patterns[f"{first} {y}"] = bc
        # With parens: "(Bertoldi 1989)"
        patterns[f"({first} {y})"] = bc
        patterns[f"({first} {y})"] = bc
        patterns[f"({first} et al. {y})"] = bc
        patterns[f"({first} & {second} {y})"] = bc

    return patterns


def normalize_for_match(s: str) -> str:
    """Normalize string for loose matching: lowercase, collapse whitespace,
    but preserve '&' and 'et al' as significant signals."""
    s = s.lower()
    # Normalize multiple spaces/whitespace to single space
    s = re.sub(r'\s+', ' ', s)
    # Remove dots, commas, hyphens, apostrophes but preserve '&' and 'et al'
    for ch in ["-", "'", ",", '"']:
        s = s.replace(ch, "")
    # Collapse remaining spaces
    s = re.sub(r' +', '', s).strip()
    return s


def is_noise_context(chunk: str) -> bool:
    """Return True if chunk looks like a table, figure caption, or PDF artifact."""
    chunk_lower = chunk.lower()
    # Skip chunks that are mostly numbers/columns (table data)
    alpha = sum(1 for c in chunk if c.isalpha())
    num = sum(1 for c in chunk if c.isdigit())
    if num > alpha * 2:
        return True
    # Skip figure/table captions
    if re.search(r'(fig\.|figure|table|plate)\s+\d', chunk_lower):
        return True
    # Skip PDF page headers/footers (short, all caps, very repetitive)
    if len(chunk) < 30 and sum(1 for c in chunk if c.isupper()) > len(chunk) * 0.6:
        return True
    # Skip chunks that are just author+year with no prose
    if re.match(r'^[A-Z][a-z]+ et al\.?\s+\d{4}[a-z]?$', chunk.strip()):
        return True
    return False


def extract_citation_contexts(text: str, patterns: Dict[str, str]) -> Dict[str, List[str]]:
    """Find all citation contexts for each bibcode.

    Scans the full text with a sliding 200-char window, finding each pattern
    and capturing surrounding context. Filters out table/figure noise.
    """
    citation_contexts: Dict[str, List[str]] = defaultdict(list)

    # Build normalized pattern → bibcode mapping
    norm_to_bibcode: Dict[str, str] = {}
    for pattern, bibcode in patterns.items():
        norm_to_bibcode[normalize_for_match(pattern)] = bibcode

    text_norm = normalize_for_match(text)
    WINDOW = 250

    found_positions = set()  # (bibcode, pos) to avoid duplicate windows

    for norm_pat, bibcode in norm_to_bibcode.items():
        start = 0
        while True:
            idx = text_norm.find(norm_pat, start)
            if idx < 0:
                break
            # Get original (un-normalized) text window
            orig_start = max(0, idx - WINDOW)
            orig_end = min(len(text), idx + WINDOW)
            chunk = text[orig_start:orig_end]
            # Clean up: collapse whitespace
            chunk_clean = re.sub(r'\s+', ' ', chunk).strip()
            # Filter out table/figure noise
            if not is_noise_context(chunk_clean) and len(chunk_clean) > 20:
                key = (bibcode, orig_start // WINDOW)
                if key not in found_positions:
                    citation_contexts[bibcode].append(chunk_clean)
                    found_positions.add(key)
            start = idx + 1

    return dict(citation_contexts)


# ─── Section detection ───────────────────────────────────────────────────────

def detect_section_headings(text: str) -> Dict[str, Tuple[int, int]]:
    """Detect section headings and their character ranges.

    Looks for patterns like "1. Introduction", "2. Observation and data reduction"
    that appear at the start of a line, followed by newline.
    """
    # Match lines that start with a section number + period + capitalized title
    section_pattern = re.compile(r'(?<=\n)\d{1,2}\.\s+[A-Z][^\n]{3,100}(?=\n)', re.MULTILINE)
    matches = list(section_pattern.finditer(text))

    sections = {}
    for i, m in enumerate(matches):
        title = m.group().strip().lower()
        # Skip very long "titles" that are actually paragraphs (no newline immediately after)
        start = m.start()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        # Only keep if followed by newline (real section heading)
        sections[title] = (start, end)

    return sections


def which_section(char_pos: int, sections: Dict[str, Tuple[int, int]]) -> str:
    """Return the section name for a given character position."""
    for name, (start, end) in sections.items():
        if start <= char_pos < end:
            return name
    return "unknown"


# ─── Classification ───────────────────────────────────────────────────────────

# ─── Classification signals ───────────────────────────────────────────────────
# High-precision signals: specific phrase → clear semantic meaning
METHOD_SIGNALS = [
    # Explicit methodology phrases
    "we use", "we adopt", "we apply", "we employ",
    "we follow", "we perform",
    "based on the method", "based on the technique",
    "based on observations", "based on the analysis",
    "derived from", "obtained from",
    "using the method", "using the technique",
    "following the method", "following the approach",
    "as described in", "as detailed in", "as proposed by",
    "described in", "described by", "detailed in",
    "developed by", "proposed by",
    "calibrated with", "calibrated using",
    "observed with", "measured with",
    "instrument", "mounted at", "installed at",
    # Specific named methods (when cited alongside technique name)
    "serkowski law",
    "chandrasekhar-fermi",
    "structure function",
    "modified bec",
    "nicer extinction",
    "gaia edr3", "gaia dr2",
    "hipparcos",
    "balmer decrement",
    "polarimetric",
    "aimpol",
]

# Named technique names → also trigger METHOD when near a citation
TECHNIQUE_NAMES = [
    "aimpol", "gaia edr3", "gaia dr2", "gaia dr1", "gaia parallax",
    "serkowski law", "structure function", "chandrasekhar-fermi",
    "nicer", "bec formula", "bec relation",
    "balmer decrement", "polarimeter",
    "2mass", "hipparcos",
]


def is_near_technique(ctx_lower: str, author_last: str, year: str) -> bool:
    """Check if a citation is near a known technique name in the context."""
    # Look for technique names within ~100 chars of the author-year citation
    for tech in TECHNIQUE_NAMES:
        idx = ctx_lower.find(tech)
        if idx >= 0:
            # Check if author-year is near this technique mention
            # Simple: if both are in the same ~150-char window, count it
            return True
    return False

RESULT_COMPARISON_SIGNALS = [
    # Direct comparison
    "in contrast to", "in contrast with",
    "unlike", "whereas", "while we", "however we find",
    "compared to", "compared with",
    "our results", "our findings", "our values",
    "we find that", "we find:", "we obtain:",
    "we estimate", "we derive",
    "agree with", "consistent with", "in agreement",
    "in good agreement", "in qualitative agreement",
    "disagree with", "inconsistent with",
    "higher than", "lower than", "much larger", "much smaller",
    "significantly", "substantially",
    "previously reported", "earlier found",
    "first detected", "first discovered",
    "revised", "updated",
    "similar to", "similar to those",
]

BACKGROUND_SIGNALS = [
    "see also", "see e.g.", "see",
    "for a review", "for overview",
    "has been shown", "have been shown",
    "it is known that", "it has been demonstrated",
    "extensively studied", "well studied",
    "previous", "earlier work", "past work",
    "generally", "typically", "commonly",
    "for instance", "such as", "including", "among others",
    "introduction", "background",
    # Generic descriptive phrases
    "the nature of", "the role of",
    "the effect of", "the influence of",
]


def classify_context(sent: str) -> str:
    sent_lower = sent.lower()
    # METHOD has highest priority when specific signal is found
    for sig in METHOD_SIGNALS:
        if sig in sent_lower:
            return "METHOD"
    # RESULT comparison phrases
    for sig in RESULT_COMPARISON_SIGNALS:
        if sig in sent_lower:
            return "RESULT"
    # Default: background/introduction context
    for sig in BACKGROUND_SIGNALS:
        if sig in sent_lower:
            return "BACKGROUND"
    return "BACKGROUND"


def classify_reference(
    bibcode: str,
    contexts: List[str],
    sections: Dict[str, Tuple[int, int]],
    text: str,
    info_map: Dict[str, Dict]
) -> Tuple[str, str]:
    """Classify a single reference based on its citation contexts.

    Classification rules (in priority order):
      - If any context triggers METHOD signal  → METHOD
      - If any context triggers RESULT signal  → RESULT
      - If both METHOD and RESULT appear       → RESULT
      - Otherwise                              → BACKGROUND
      - If no contexts found                  → UNCITED

    Returns: (category, summary)
    """
    if not contexts:
        return "UNCITED", "No citation context found in text"

    class_counts = defaultdict(int)
    for ctx in contexts:
        cls = classify_context(ctx)
        class_counts[cls] += 1

    # Priority: METHOD > RESULT > BACKGROUND
    if class_counts["METHOD"] > 0 and class_counts["RESULT"] == 0:
        cat = "METHOD"
    elif class_counts["RESULT"] > 0:
        cat = "RESULT"
    elif class_counts["BACKGROUND"] > 0:
        cat = "BACKGROUND"
    else:
        cat = "UNCITED"

    # Build summary
    info = info_map.get(bibcode, {})
    title = info.get("title", "")[:80]
    authors = info.get("authors", [])
    author_str = ", ".join(authors[:2]) if authors else "(unknown)"
    year = info.get("year", "")
    sample_ctx = contexts[0][:150].replace("\n", " ")

    summary = f'{bibcode} ({author_str} {year}) — "{title}"'
    summary += f'\n  [{cat}] citations: {len(contexts)} (METHOD={class_counts["METHOD"]}, RESULT={class_counts["RESULT"]}, BACKGROUND={class_counts["BACKGROUND"]})'
    summary += f'\n  Sample: "{sample_ctx}"'

    return cat, summary


# ─── Main ────────────────────────────────────────────────────────────────────

def run(pdf_path: str, refs_json: Optional[str], bibcode: Optional[str], ads_token: str, resolve: bool = True, pdf_text_arg: str = "") -> None:
    if pdf_text_arg:
        text = pdf_text_arg
        print(f"[ref_classifier] Using pre-provided PDF text ({len(text)} chars)", file=sys.stderr)
    else:
        print(f"[ref_classifier] Reading PDF: {pdf_path}", file=sys.stderr)
        text = extract_clean_text(pdf_path)
        if not text:
            print("Error: could not extract text from PDF", file=sys.stderr)
            sys.exit(1)
        print(f"[ref_classifier] Extracted {len(text)} chars", file=sys.stderr)

    # Detect sections
    sections = detect_section_headings(text)
    print(f"[ref_classifier] Detected {len(sections)} sections: {list(sections.keys())}", file=sys.stderr)

    # Get reference bibcodes
    if refs_json:
        try:
            refs_data = json.loads(refs_json)
            ref_bibcodes = [r.get("bibcode", "") for r in refs_data if r.get("bibcode")]
        except Exception:
            print("Error: could not parse refs_json", file=sys.stderr)
            sys.exit(1)
    elif bibcode and ads_token:
        print(f"[ref_classifier] Fetching refs from ADS for {bibcode}...", file=sys.stderr)
        ref_bibcodes = fetch_refs_from_ads(bibcode, ads_token)
        print(f"[ref_classifier] Got {len(ref_bibcodes)} reference bibcodes from ADS", file=sys.stderr)

        # Also parse refs from PDF (to get author+year for classification patterns)
        pdf_refs_parsed = extract_refs_from_pdf(text)
        print(f"[ref_classifier] Also parsed {len(pdf_refs_parsed)} refs from PDF for classification", file=sys.stderr)

        if not ref_bibcodes:
            print("[ref_classifier] ADS returned empty reference list. Falling back to PDF-only mode...", file=sys.stderr)
            resolved, info_map = resolve_refs_via_author_year(pdf_refs_parsed, ads_token)
            ref_bibcodes = [r["bibcode"] for r in resolved if r.get("bibcode")]
        else:
            # ADS returned refs: use those bibcodes. Also resolve their titles via ADS.
            # Merge info from ADS bibcode resolution with PDF-parsed author/year for pattern building.
            ads_info = resolve_ref_titles(ref_bibcodes, ads_token)
            # Build a lookup: first_author_lastname → (bibcode, info)
            first_author_map: Dict[str, Dict] = {}
            for bc, info in ads_info.items():
                authors = info.get("authors", [])
                if authors:
                    lastname = authors[0].split(",")[0].strip() if "," in authors[0] else authors[0].split()[0]
                    first_author_map[lastname] = (bc, info)

            # For PDF-parsed refs, try to match to ADS bibcodes by first author + year
            resolved_refs: List[Dict] = []
            for pref in pdf_refs_parsed:
                lastname = pref["author"]
                year = pref["year"]
                matched_bc = None
                matched_info = None
                for ln, (bc, info) in first_author_map.items():
                    if ln == lastname and info.get("year", "").startswith(year[:4]):
                        matched_bc = bc
                        matched_info = info
                        break
                if matched_bc:
                    resolved_refs.append({**pref, "bibcode": matched_bc})
                    ads_info[matched_bc] = matched_info
                    first_author_map.pop(lastname, None)  # prevent duplicates

            ref_bibcodes = list(ads_info.keys())
            info_map = ads_info
    else:
        # Try to parse from PDF
        refs_parsed = extract_refs_from_pdf(text)
        ref_bibcodes = [r["bibcode"] for r in refs_parsed if r.get("bibcode")]
        if not ref_bibcodes:
            print("Error: no refs_json, --bibcode, or ADS token provided, and could not parse refs from PDF", file=sys.stderr)
            sys.exit(1)

    # Resolve reference titles
    info_map: Dict = {}
    if ads_token and resolve:
        try:
            info_map = resolve_ref_titles(ref_bibcodes, ads_token)
        except Exception as e:
            print(f"[ref_classifier] resolve_titles failed: {e}. Using PDF-parsed references for classification.", file=sys.stderr)
            # Fall back to PDF-parsed refs + author-year resolution
            pdf_refs = extract_refs_from_pdf(text)
            if pdf_refs:
                resolved, info_map = resolve_refs_via_author_year(pdf_refs, ads_token)
                ref_bibcodes = [r["bibcode"] for r in resolved if r.get("bibcode")]
            else:
                info_map = {}

    # Build citation patterns
    patterns = build_author_year_patterns(ref_bibcodes, info_map)
    print(f"[ref_classifier] Built {len(patterns)} citation patterns", file=sys.stderr)

    # Extract citation contexts
    citation_contexts = extract_citation_contexts(text, patterns)
    print(f"[ref_classifier] Found citations for {len(citation_contexts)} references", file=sys.stderr)

    # Classify
    results = {}
    for bc in ref_bibcodes:
        ctxs = citation_contexts.get(bc, [])
        cat, summary = classify_reference(bc, ctxs, sections, text, info_map)
        results[bc] = {"category": cat, "contexts": ctxs, "info": info_map.get(bc, {})}

    # Print summary
    cats = defaultdict(list)
    for bc, r in results.items():
        cats[r["category"]].append(bc)

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"CLASSIFICATION SUMMARY", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Total references: {len(ref_bibcodes)}", file=sys.stderr)
    print(f"  METHOD:        {len(cats.get('METHOD', []))} refs", file=sys.stderr)
    print(f"  RESULT:        {len(cats.get('RESULT', []))} refs", file=sys.stderr)
    print(f"  BACKGROUND:    {len(cats.get('BACKGROUND', []))} refs", file=sys.stderr)
    print(f"  METHOD+RESULT: {len(cats.get('METHOD+RESULT', []))} refs", file=sys.stderr)
    print(f"  BACKGROUND+METHOD: {len(cats.get('BACKGROUND+METHOD', []))} refs", file=sys.stderr)
    print(f"  BACKGROUND+RESULT: {len(cats.get('BACKGROUND+RESULT', []))} refs", file=sys.stderr)
    print(f"  UNCITED:       {len(cats.get('UNCITED', []))} refs", file=sys.stderr)
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"DETAILED RESULTS (first 5 per category)", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    CATEGORIES = ["METHOD", "RESULT", "BACKGROUND+METHOD", "BACKGROUND+RESULT", "METHOD+RESULT", "BACKGROUND", "UNCITED"]
    DETAIL_LIMIT = 5

    for cat_name in CATEGORIES:
        refs_in_cat = cats.get(cat_name, [])
        if not refs_in_cat:
            continue
        print(f"\n{'─'*50}", file=sys.stderr)
        print(f"【{cat_name}】— {len(refs_in_cat)} refs", file=sys.stderr)
        print(f"{'─'*50}", file=sys.stderr)
        for bc in refs_in_cat[:DETAIL_LIMIT]:
            r = results[bc]
            info = r["info"]
            title = info.get("title", "")[:80]
            authors = info.get("authors", [])
            author_str = ", ".join(authors[:2]) if authors else "(unknown)"
            year = info.get("year", "")
            ctxs = r["contexts"]
            print(f"\n  [{bc}]", file=sys.stderr)
            print(f"   \"{title}\"", file=sys.stderr)
            print(f"   {author_str} ({year})", file=sys.stderr)
            if ctxs:
                print(f"   Citations ({len(ctxs)}):", file=sys.stderr)
                for ctx in ctxs[:2]:
                    print(f"    → \"{ctx[:120].replace(chr(10), ' ')}\"", file=sys.stderr)
            else:
                print(f"   No citation context found in text", file=sys.stderr)
        if len(refs_in_cat) > DETAIL_LIMIT:
            print(f"\n  ... and {len(refs_in_cat)-DETAIL_LIMIT} more in this category", file=sys.stderr)

    # JSON output
    print(f"\n{'='*60}")
    print(f"JSON OUTPUT")
    print(f"{'='*60}")
    output = {
        "summary": {cat: len(refs) for cat, refs in cats.items()},
        "references": [
            {
                "bibcode": bc,
                "category": r["category"],
                "title": r["info"].get("title", ""),
                "authors": r["info"].get("authors", []),
                "year": r["info"].get("year", ""),
                "ads_url": f"https://ui.adsabs.harvard.edu/abs/{bc}/abstract",
                "num_citations": len(r["contexts"]),
                "sample_contexts": [c[:200] for c in r["contexts"][:3]],
            }
            for bc, r in results.items()
        ]
    }
    print(json.dumps(output, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify paper references by citation type")
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--refs_json", help='JSON string or file path containing reference list (bibcodes)')
    parser.add_argument("--bibcode", help="ADS bibcode to fetch references from ADS")
    parser.add_argument("--token", default=os.environ.get("ADS_API_TOKEN", ""), help="ADS API token")
    parser.add_argument("--no_resolve", action="store_true", help="Skip title/author resolution from ADS")
    args = parser.parse_args()

    if not args.token:
        print("Error: ADS_API_TOKEN not set", file=sys.stderr)
        return 1

    run(args.pdf, args.refs_json, args.bibcode, args.token, resolve=not args.no_resolve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
