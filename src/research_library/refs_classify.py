#!/usr/bin/env python3
from __future__ import annotations

"""
refs_classify.py — Full pipeline for reference extraction + LLM citation classification.

Strategy:
  1. PDF text → extract DOIs (for papers that have them in PDF)
  2. ADS reference field (if populated)
  3. PDF parsing fallback (author-year extraction)
  4. For refs with bibcodes: extract citation contexts from PDF text
  5. LLM classification via OpenClaw subagent

Usage:
    python3 refs_classify.py /path/to/paper.pdf --bibcode 2024A&A...689A.225N [--doi 10.xxx]
    python3 refs_classify.py /path/to/paper.pdf --bibcode XXX --json-only  # just data, no LLM
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

ADS_API_URL = "https://api.adsabs.harvard.edu/v1/search/query"
CROSSREF_API = "https://api.crossref.org/works"
USER_AGENT = "refs_classify/1.0 (OpenClaw)"

# ── Helpers ────────────────────────────────────────────────────────────────────

def http_get(url, token="", headers_ext=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if headers_ext:
        headers.update(headers_ext)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_env_token():
    from research_library.config import load_ads_token

    return load_ads_token()


def normalize(s):
    s = s.lower()
    s = re.sub(r'\s+', ' ', s)
    for ch in ["-", "'", ",", '"']:
        s = s.replace(ch, "")
    return re.sub(r' +', '', s).strip()


def is_noise(chunk):
    alpha = sum(1 for c in chunk if c.isalpha())
    num = sum(1 for c in chunk if c.isdigit())
    if num > alpha * 2:
        return True
    if re.search(r'(fig\.|figure|table)\s+\d', chunk.lower()):
        return True
    if len(chunk) < 30 and sum(1 for c in chunk if c.isupper()) / max(len(chunk), 1) > 0.6:
        return True
    return False


# ── PDF Text ─────────────────────────────────────────────────────────────────

def extract_pdf_text(pdf_path):
    try:
        from pdfminer.high_level import extract_text
        return extract_text(pdf_path)
    except Exception as e:
        print(f"[pdf] failed: {e}", file=sys.stderr)
        return ""


# ── CrossRef ─────────────────────────────────────────────────────────────────

def fetch_crossref_refs(doi):
    try:
        url = f"{CROSSREF_API}/{urllib.parse.quote(doi)}?mailto=test@example.com"
        data = http_get(url)
        refs = data.get("message", {}).get("reference", [])
        results = []
        for r in refs:
            authors_raw = r.get("author", [])
            first_author = ""
            if authors_raw:
                first = authors_raw[0]
                if isinstance(first, dict):
                    first_author = first.get("family", "")
                elif isinstance(first, str):
                    first_author = first.split(",")[0].strip()
            year_raw = str(r.get("published-print", r.get("published-online", {})).get("date-parts", [[""]])[0][0] or r.get("year", ""))
            year = re.sub(r'\D', '', year_raw)[:4]
            results.append({
                "doi": (r.get("DOI") or "").lower(),
                "author": first_author,
                "year": year,
                "title": (r.get("title", [""])[0] if isinstance(r.get("title"), list) else r.get("title", ""))[:100],
                "journal": (r.get("container-title", [""])[0] if isinstance(r.get("container-title"), list) else r.get("journal", "")),
            })
        return results
    except Exception as e:
        print(f"[crossref] failed: {e}", file=sys.stderr)
        return []


# ── ADS reference field ───────────────────────────────────────────────────────

def fetch_ads_refs(bibcode, token):
    try:
        params = urllib.parse.urlencode({"q": f"bibcode:{bibcode}", "rows": "1", "fl": "bibcode,reference"})
        data = http_get(f"{ADS_API_URL}?{params}", token)
        return data.get("response", {}).get("docs", [[]])[0].get("reference", [])
    except Exception as e:
        print(f"[ads-refs] failed: {e}", file=sys.stderr)
        return []


# ── Resolve DOIs to ADS bibcodes ─────────────────────────────────────────────────

def resolve_dois_to_ads(dois, token):
    """Batch resolve DOIs to ADS bibcodes. Returns dict {doi_lower: {bibcode, title, author, year, pub, ads_url}}."""
    results = {}
    dois_clean = [d.strip().lower() for d in dois if d.strip()]
    if not dois_clean:
        return results
    BATCH = 10
    for i in range(0, len(dois_clean), BATCH):
        batch = dois_clean[i:i+BATCH]
        queries = [f'doi:"{d}"' for d in batch]
        q = " OR ".join(queries)
        try:
            params = urllib.parse.urlencode({"q": q, "rows": str(len(batch)), "fl": "bibcode,doi,title,author,year,pub", "sort": "score desc"})
            data = http_get(f"{ADS_API_URL}?{params}", token)
            for doc in data.get("response", {}).get("docs", []):
                doc_doi = (doc.get("doi") or "").lower()
                results[doc_doi] = {
                    "bibcode": doc.get("bibcode", ""),
                    "title": " ".join(doc.get("title", [""])[0].split()) if doc.get("title") else "",
                    "authors": doc.get("author", [])[:3],
                    "year": doc.get("year", ""),
                    "pub": doc.get("pub", ""),
                    "ads_url": f"https://ui.adsabs.harvard.edu/abs/{doc.get('bibcode','')}/abstract"
                }
        except Exception as e:
            print(f"[ads-doi] batch {i//BATCH+1} failed: {e}", file=sys.stderr)
    return results


# ── Resolve author-year pairs to ADS bibcodes ─────────────────────────────────────

def resolve_author_year_to_ads(pdf_refs, token):
    """Resolve PDF-parsed refs (author+year) to ADS bibcodes via individual queries.
    
    ADS doesn't support deeply nested OR groups, so we query each author-year pair
    individually to avoid parse errors.
    """
    info_map = {}
    seen_bcs = set()
    BATCH = 10
    for i in range(0, len(pdf_refs), BATCH):
        batch = pdf_refs[i:i+BATCH]
        for ref in batch:
            author_key = ref["author"]
            year_key = ref["year"]
            q = "author:\"" + author_key + "\" AND year:" + year_key
            try:
                params = urllib.parse.urlencode({"q": q, "rows": "5", "fl": "bibcode,title,author,year,pub", "sort": "score desc"})
                data = http_get(ADS_API_URL + "?" + params, token)
                for doc in data.get("response", {}).get("docs", []):
                    bc = doc.get("bibcode", "")
                    if bc and bc not in seen_bcs:
                        info_map[bc] = {
                            "bibcode": bc,
                            "title": " ".join(doc.get("title",[""])[0].split()) if doc.get("title") else "",
                            "authors": doc.get("author",[])[:3],
                            "year": doc.get("year",""),
                            "pub": doc.get("pub",""),
                            "ads_url": "https://ui.adsabs.harvard.edu/abs/" + bc + "/abstract"
                        }
                        seen_bcs.add(bc)
                        break  # take first (best) match per ref
            except Exception as e:
                pass  # skip failed queries silently
    return info_map, []


# ── Parse references from PDF ────────────────────────────────────────────────────

def parse_refs_from_pdf(pdf_path):
    """Extract (author, year) from PDF references section using line accumulation.
    
    Algorithm: find References section, accumulate lines into entries,
    extract first author + first year from each, deduplicate.
    Returns list of {author, year, text}.
    """
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        return []
    
    try:
        full_text = extract_text(pdf_path)
    except Exception:
        return []
    
    ref_idx = full_text.rfind('References')
    if ref_idx < 0:
        return []
    
    section = full_text[ref_idx:]
    lines = section.split('\n')
    
    entries = []
    current = []
    
    for line in lines[1:]:  # skip header
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r'^(?:A225|Page\s+\d+|\d{4})$', stripped):
            continue
        
        has_author = re.match(r'^([A-Z][a-zA-Z\-\']+),\s+[A-Z]', stripped)
        has_year = re.search(r'\b(20\d{2}|19\d{2})\b', stripped)
        
        if has_author and has_year:
            if current:
                entries.append(' '.join(current))
            current = [stripped]
        else:
            current.append(stripped)
    
    if current:
        entries.append(' '.join(current))
    
    refs = []
    for entry in entries:
        first_word_m = re.match(r'^([A-Z][a-zA-Z\-\']+)[^,]*', entry)
        year_m = re.search(r'\b(20\d{2}|19\d{2})\b', entry)
        if first_word_m and year_m:
            refs.append({
                'author': first_word_m.group(1),
                'year': year_m.group(1),
                'text': entry[:150]
            })
    
    seen = set()
    unique = []
    for r in refs:
        key = (r['author'], r['year'])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    
    print(f"[parse_refs] PDF: {len(unique)} unique refs from {len(entries)} entries", file=sys.stderr)
    return unique


def extract_contexts(text, patterns):
    """Extract citation contexts using normalized author-year matching."""
    contexts = defaultdict(list)
    norm_to_bc = {normalize(k): v for k, v in patterns.items()}
    text_norm = normalize(text)
    WINDOW = 250
    found = set()

    for norm_pat, bc in norm_to_bc.items():
        if not norm_pat:
            continue
        start = 0
        while True:
            idx = text_norm.find(norm_pat, start)
            if idx < 0:
                break
            ostart = max(0, idx - WINDOW)
            oend = min(len(text), idx + WINDOW)
            chunk = re.sub(r'\s+', ' ', text[ostart:oend]).strip()
            key = (bc, ostart // WINDOW)
            if not is_noise(chunk) and key not in found and len(chunk) > 20:
                contexts[bc].append(chunk)
                found.add(key)
            start = idx + 1
    return dict(contexts)


def build_patterns(info_map):
    """Build author-year → bibcode patterns."""
    patterns = {}
    for bc, info in info_map.items():
        authors = info.get("authors", [])
        year = str(info.get("year", ""))[:4]
        if not authors or not year:
            continue
        first = authors[0].split(",")[0].strip() if "," in authors[0] else authors[0].split()[0]
        second = ""
        if len(authors) > 1:
            second = authors[1].split(",")[0].strip() if "," in authors[1] else authors[1].split()[0]
        for sep in [" et al. ", " et al.", " et al ", " + ", " +"]:
            patterns[f"{first}{sep}{year}"] = bc
        if second:
            for amp in [" & ", " and "]:
                patterns[f"{first}{amp}{second} {year}"] = bc
        patterns[f"{first} {year}"] = bc
    return patterns


# ── Paper metadata from ADS ──────────────────────────────────────────────────────

def fetch_paper_info(bibcode, token):
    try:
        params = urllib.parse.urlencode({"q": f"bibcode:{bibcode}", "rows": "1", "fl": "bibcode,title,author,year,pub,abstract,doi"})
        data = http_get(f"{ADS_API_URL}?{params}", token)
        docs = data.get("response", {}).get("docs", [])
        if docs:
            doc = docs[0]
            return {
                "bibcode": doc.get("bibcode", bibcode),
                "title": " ".join(doc.get("title", [""])[0].split()),
                "authors": doc.get("author", [])[:5],
                "year": doc.get("year", ""),
                "abstract": (doc.get("abstract") or "")[:500],
                "doi": doc.get("doi", ""),
                "ads_url": f"https://ui.adsabs.harvard.edu/abs/{doc.get('bibcode', bibcode)}/abstract"
            }
    except Exception as e:
        print(f"[ads-paper] failed: {e}", file=sys.stderr)
    return {"bibcode": bibcode, "title": "", "authors": [], "year": "", "abstract": "", "doi": "", "ads_url": f"https://ui.adsabs.harvard.edu/abs/{bibcode}/abstract"}


# ── Main ─────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None):
    from research_library.config import load_env

    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", help="Path to PDF")
    parser.add_argument("--bibcode", required=True)
    parser.add_argument("--doi", help="Paper DOI")
    parser.add_argument("--token")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--pdf-only", action="store_true")
    args = parser.parse_args(argv)

    ads_token = args.token or load_env_token()

    # 1. PDF text
    print(f"[refs_classify] Reading PDF...", file=sys.stderr)
    text = extract_pdf_text(args.pdf)
    if not text:
        print("Error: no PDF text", file=sys.stderr)
        return 1
    print(f"[refs_classify] PDF: {len(text)} chars", file=sys.stderr)

    # Extract DOI/arXiv from PDF
    if not args.doi:
        m = re.search(r"10\.\d{4,}/[^\s<>\"'\)\\\]]+", text)
        if m:
            args.doi = m.group(0).rstrip(".,;:)")
    arxiv_m = re.search(r"arXiv:\s*(\d{4}\.\d+)", text)

    # 2. Paper info
    paper = fetch_paper_info(args.bibcode, ads_token)
    if arxiv_m:
        paper["arxiv"] = arxiv_m.group(1)
    print(f"[refs_classify] Paper: {paper.get('title','')[:60]}", file=sys.stderr)

    # 3. Get references from multiple sources
    crossref_refs = []
    if args.doi:
        crossref_refs = fetch_crossref_refs(args.doi)
        print(f"[refs_classify] CrossRef: {len(crossref_refs)} refs", file=sys.stderr)

    ads_refs = []
    if ads_token:
        ads_refs = fetch_ads_refs(args.bibcode, ads_token)
        print(f"[refs_classify] ADS refs field: {len(ads_refs)} refs", file=sys.stderr)

    pdf_refs = parse_refs_from_pdf(args.pdf)
    print(f"[refs_classify] PDF parsed: {len(pdf_refs)} refs", file=sys.stderr)

    # 4. Build combined reference list
    # Priority: CrossRef > PDF-parsed (for bibcode resolution)
    ref_bibcodes = set()
    dois_to_resolve = []
    dois_found = {}

    # From CrossRef
    if crossref_refs:
        dois_to_resolve = [r.get("doi","") for r in crossref_refs]
        if dois_to_resolve and ads_token:
            dois_found = resolve_dois_to_ads(dois_to_resolve, ads_token)
            print(f"[refs_classify] CrossRef→ADS resolved: {len(dois_found)} / {len(dois_to_resolve)}", file=sys.stderr)

    # Build reference list
    all_refs = []
    seen_bibcodes = set()
    seen_doi_author = set()

    # First: CrossRef refs with ADS bibcodes
    for ref in crossref_refs:
        doi = (ref.get("doi") or "").lower()
        info = dois_found.get(doi, {})
        bc = info.get("bibcode", "")
        if bc and bc not in seen_bibcodes:
            seen_bibcodes.add(bc)
            all_refs.append({
                "bibcode": bc,
                "doi": doi,
                "author": ref.get("author",""),
                "authors": info.get("authors", [ref.get("author","")]),
                "year": info.get("year", ref.get("year","")),
                "title": info.get("title", ref.get("title","")),
                "journal": info.get("pub", ref.get("journal","")),
                "ads_url": info.get("ads_url", f"https://ui.adsabs.harvard.edu/abs/{bc}/abstract"),
                "context": "",
                "num_citations": 0,
                "source": "crossref",
            })

    # Second: PDF-parsed refs (author-year)
    if pdf_refs and ads_token:
        # Resolve PDF refs via author-year search
        info_map, _ = resolve_author_year_to_ads(pdf_refs, ads_token)
        print(f"[refs_classify] PDF→ADS (author-year) resolved: {len(info_map)} refs", file=sys.stderr)

        for ref in pdf_refs:
            # Find matching bibcode in info_map
            matched_bc = None
            matched_info = None
            for bc, info in info_map.items():
                doc_authors = info.get("authors", [])
                if doc_authors:
                    first = doc_authors[0].split(",")[0].strip() if "," in doc_authors[0] else doc_authors[0].split()[0]
                    if first == ref["author"] and str(info.get("year","")).startswith(str(ref["year"])[:4]):
                        if bc not in seen_bibcodes:
                            matched_bc = bc
                            matched_info = info
                            break
            if matched_bc:
                seen_bibcodes.add(matched_bc)
                all_refs.append({
                    "bibcode": matched_bc,
                    "author": ref["author"],
                    "authors": matched_info.get("authors", [ref["author"]]),
                    "year": matched_info.get("year", ref["year"]),
                    "title": matched_info.get("title", ""),
                    "journal": matched_info.get("pub", ""),
                    "ads_url": matched_info.get("ads_url", f"https://ui.adsabs.harvard.edu/abs/{matched_bc}/abstract"),
                    "context": "",
                    "num_citations": 0,
                    "source": "pdf",
                })

    # 5. Extract citation contexts
    info_for_patterns = {r["bibcode"]: r for r in all_refs if r.get("bibcode")}
    patterns = build_patterns(info_for_patterns)
    print(f"[refs_classify] Citation patterns: {len(patterns)}", file=sys.stderr)

    citation_contexts = extract_contexts(text, patterns)
    print(f"[refs_classify] Citation contexts: {len(citation_contexts)} refs with ctx", file=sys.stderr)

    # Update ref list with contexts
    for r in all_refs:
        bc = r.get("bibcode", "")
        ctxs = citation_contexts.get(bc, [])
        r["num_citations"] = len(ctxs)
        r["context"] = ctxs[0][:300].replace("\n", " ") if ctxs else ""

    # 6. Output
    data = {
        "paper": paper,
        "references": all_refs,
        "stats": {
            "total": len(all_refs),
            "resolved_ads": sum(1 for r in all_refs if r.get("bibcode")),
            "with_context": sum(1 for r in all_refs if r.get("context")),
            "from_crossref": sum(1 for r in all_refs if r.get("source") == "crossref"),
            "from_pdf": sum(1 for r in all_refs if r.get("source") == "pdf"),
        }
    }

    if args.json_only:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    # Human-readable output
    print(f"\n{'='*60}")
    print(f"📚 Reference Classification")
    print(f"{'='*60}")
    print(f"Paper: {paper.get('title','')}")
    print(f"Authors: {', '.join(paper.get('authors',[])[:3])} ({paper.get('year','')})")
    print(f"Bibcode: {paper.get('bibcode','')}")
    print(f"Total refs: {data['stats']['total']} | ADS resolved: {data['stats']['resolved_ads']} | With ctx: {data['stats']['with_context']}")

    print(f"\n[REF_JSON_START]", file=sys.stderr)
    print(json.dumps(data, indent=2, ensure_ascii=False), file=sys.stderr)
    print(f"[REF_JSON_END]", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
