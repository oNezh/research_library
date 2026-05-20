"""Normalize reference inputs → StandardRef (DB hint + ADS resolution). No PDF I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Literal, Optional

from . import db as library_db
from research_library.lookup import (
    ADSAPIBlockedError,
    ads_query,
    ads_search_query,
    ads_search_reference,
    merge_and_rank,
    parse_reference,
)

BIBCODE_RE = re.compile(r"^\d{4}[A-Za-z][\S]+$")
ARXIV_RE = re.compile(r"^(?:arxiv:)?(?P<id>\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE)
DOI_RE = re.compile(r"^(?:doi:)?(?P<doi>10\.\d+\/\S+)$", re.IGNORECASE)


def strip_arxiv_version(aid: str) -> str:
    a = aid.strip()
    if a.lower().startswith("arxiv:"):
        a = a[6:].strip()
    return re.sub(r"v\d+$", "", a, flags=re.IGNORECASE)


def extract_arxiv_from_text(text: str) -> Optional[str]:
    m = re.search(
        r"(?:arxiv)[:/\s]*(\d{4}\.\d{4,5}(?:v\d+)?)",
        text,
        re.IGNORECASE,
    )
    if m:
        return strip_arxiv_version(m.group(1))
    m = re.search(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", text)
    if m:
        return strip_arxiv_version(m.group(1))
    return None


def extract_doi_from_text(text: str) -> Optional[str]:
    m = re.search(r"(10\.\d{4,}/[^\s\],;)\]]+)", text)
    if not m:
        return None
    return m.group(1).rstrip(".,;)")


def parse_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _db_lookup_bibcode(conn: Any, line: str) -> Optional[str]:
    if BIBCODE_RE.match(line):
        r = conn.execute(
            "SELECT bibcode FROM papers WHERE bibcode = ?", (line,)
        ).fetchone()
        if r and r[0]:
            return str(r[0])
    m = ARXIV_RE.match(line.strip())
    if m:
        aid = m.group("id")
        base = strip_arxiv_version(aid)
        for cand in {aid, base, base + "v1", base.upper()}:
            r = conn.execute(
                "SELECT bibcode FROM papers WHERE arxiv_id = ?", (cand,)
            ).fetchone()
            if r and r[0]:
                return str(r[0])
    return None


def db_lookup_bibcode_by_arxiv_variants(conn: Any, arxiv_id: str) -> Optional[str]:
    """Return stored bibcode if any arxiv_id variant matches a row."""
    library_db.ensure_schema(conn)
    aid = arxiv_id.strip()
    base = strip_arxiv_version(aid)
    for cand in {aid, base, base + "v1", base.upper()}:
        r = conn.execute(
            "SELECT bibcode FROM papers WHERE arxiv_id = ? AND bibcode IS NOT NULL AND TRIM(bibcode) != ''",
            (cand,),
        ).fetchone()
        if r and r[0]:
            return str(r[0])
    return None


def _resolve_bibcode_from_ads(line: str) -> tuple[Optional[str], str]:
    if BIBCODE_RE.match(line):
        bc_in = line.strip()
        try:
            result = ads_query(f'bibcode:"{bc_in}"', rows=1)
            docs = result.get("response", {}).get("docs", [])
            if docs:
                return docs[0].get("bibcode"), "bibcode_verified"
        except Exception as e:
            # Verified shape but ADS unreachable (TLS, WAF, etc.): still use for acquire/URLs.
            return bc_in, f"bibcode_assumed_ads_error:{e}"
        return None, "bibcode not in ADS"

    m = DOI_RE.match(line.strip())
    if m:
        doi = m.group("doi").rstrip(".,;)")
        try:
            result = ads_query(f'doi:"{doi}"', rows=1)
            docs = result.get("response", {}).get("docs", [])
            if docs and docs[0].get("bibcode"):
                return docs[0]["bibcode"], "doi"
        except Exception as e:
            return None, f"ADS doi search failed: {e}"
        return None, "doi not found in ADS"

    m = ARXIV_RE.match(line.strip())
    if m:
        qid = strip_arxiv_version(m.group("id"))
        try:
            result = ads_query(f"arxiv:{qid}", rows=1)
            docs = result.get("response", {}).get("docs", [])
            if docs and docs[0].get("bibcode"):
                return docs[0]["bibcode"], "arxiv"
        except Exception as e:
            return None, f"ADS arxiv search failed: {e}"
        return None, "arxiv not in ADS"

    parsed = parse_reference(line)
    try:
        primary = ads_search_reference(parsed)
    except ADSAPIBlockedError as e:
        return None, f"ads_blocked:{e}"
    fallback = fallback_arxiv_for_refline(line)
    ranked = merge_and_rank(primary, fallback, top_n=5)
    if not ranked:
        try:
            aq = ads_search_query(line[:500])
        except ADSAPIBlockedError as e:
            return None, f"ads_blocked:{e}"
        ranked = merge_and_rank(aq, fallback_arxiv_for_refline(line), top_n=5)
    if not ranked:
        return None, "no ADS match"
    top = ranked[0]
    if not top.bibcode:
        return None, "top hit missing bibcode"
    return top.bibcode, f"ref_parse:{top.reason or 'merged'}"


def fallback_arxiv_for_refline(line: str) -> List:
    from research_library.lookup import arxiv_query, similarity

    q = f'all:"{line.replace(chr(34), "")[:200]}"'
    try:
        cands = arxiv_query(q, max_results=4)
        for c in cands:
            c.score = similarity(c.title, line) * 40.0
        return cands
    except Exception:
        return []


def _classify_catalog_line(line: str) -> Literal["bibcode", "doi", "arxiv", "freeform"]:
    if BIBCODE_RE.match(line):
        return "bibcode"
    if DOI_RE.match(line.strip()):
        return "doi"
    if ARXIV_RE.match(line.strip()):
        return "arxiv"
    return "freeform"


def _extract_catalog_identifiers(line: str, kind: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (bibcode, arxiv_id, doi) normalized for catalog lines."""
    bib: Optional[str] = None
    arx: Optional[str] = None
    doi: Optional[str] = None
    if kind == "bibcode":
        bib = line.strip()
    elif kind == "doi":
        m = DOI_RE.match(line.strip())
        if m:
            doi = m.group("doi").rstrip(".,;)")
    elif kind == "arxiv":
        m = ARXIV_RE.match(line.strip())
        if m:
            arx = strip_arxiv_version(m.group("id"))
    return bib, arx, doi


@dataclass
class StandardRef:
    raw_line: str
    kind: Literal["bibcode", "doi", "arxiv", "freeform", "fragment"]
    bibcode: Optional[str] = None
    arxiv_id: Optional[str] = None
    doi: Optional[str] = None
    library_hit: bool = False
    resolution_note: str = ""


def parse_catalog_line(
    line: str,
    conn: Optional[Any] = None,
    *,
    use_ads: bool = True,
) -> StandardRef:
    """One CLI-style line: bibcode / DOI / arXiv / freeform → StandardRef (library → ADS)."""
    raw = line.strip()
    kind = _classify_catalog_line(raw)
    bib, arx, doi = _extract_catalog_identifiers(raw, kind)

    if conn is not None:
        library_db.ensure_schema(conn)
        db_bib = _db_lookup_bibcode(conn, raw)
        if db_bib:
            return StandardRef(
                raw_line=raw,
                kind=kind,
                bibcode=db_bib,
                arxiv_id=arx,
                doi=doi,
                library_hit=True,
                resolution_note="database",
            )

    if conn is not None and kind == "freeform":
        library_db.ensure_schema(conn)
        pr = parse_reference(raw)
        if pr.first_author and pr.year:
            cands = library_db.fetch_paper_dicts_by_author_year(
                conn, pr.first_author, pr.year, limit=2
            )
            if len(cands) == 1:
                r0 = cands[0]
                bc = (r0.get("bibcode") or "").strip()
                if bc:
                    return StandardRef(
                        raw_line=raw,
                        kind=kind,
                        bibcode=bc,
                        arxiv_id=(r0.get("arxiv_id") or arx),
                        doi=doi,
                        library_hit=True,
                        resolution_note="library_author_year",
                    )

        try:
            fts_rows = library_db.search_fts(conn, raw, limit=5)
        except Exception:
            fts_rows = []
        if len(fts_rows) == 1:
            r0 = fts_rows[0]
            bc = (r0.get("bibcode") or "").strip()
            if bc:
                return StandardRef(
                    raw_line=raw,
                    kind=kind,
                    bibcode=bc,
                    arxiv_id=(r0.get("arxiv_id") or arx),
                    doi=doi,
                    library_hit=True,
                    resolution_note="library_fts",
                )

    if not use_ads:
        return StandardRef(
            raw_line=raw,
            kind=kind,
            arxiv_id=arx,
            doi=doi,
            resolution_note="no_ads_requested",
        )

    bc, note = _resolve_bibcode_from_ads(raw)
    return StandardRef(
        raw_line=raw,
        kind=kind,
        bibcode=bc,
        arxiv_id=arx,
        doi=doi,
        library_hit=False,
        resolution_note=note,
    )


def parse_bibliography_fragment(line: str, conn: Optional[Any] = None) -> StandardRef:
    """Bibliography entry line: embedded arXiv/DOI; optional whole-line catalog; DB → ADS (no free-form ADS)."""
    raw = line.strip()
    kind_cat = _classify_catalog_line(raw)
    if kind_cat != "freeform":
        ref = parse_catalog_line(raw, conn, use_ads=True)
        ref.kind = kind_cat  # type: ignore[assignment]
        return ref

    arx = extract_arxiv_from_text(raw)
    doi = extract_doi_from_text(raw)

    if conn is not None and arx:
        library_db.ensure_schema(conn)
        db_bib = db_lookup_bibcode_by_arxiv_variants(conn, arx)
        if db_bib:
            return StandardRef(
                raw_line=raw,
                kind="fragment",
                bibcode=db_bib,
                arxiv_id=arx,
                doi=doi,
                library_hit=True,
                resolution_note="database",
            )

    bib: Optional[str] = None
    note = ""
    token = __import__("os").environ.get("ADS_API_TOKEN")
    if token:
        try:
            if doi:
                r = ads_query(f'doi:"{doi}"', rows=1, fl=["bibcode", "identifier"])
                docs = r.get("response", {}).get("docs", [])
                if docs and docs[0].get("bibcode"):
                    bib = str(docs[0]["bibcode"])
                    note = "ads_doi"
                    if not arx:
                        from research_library.lookup import choose_identifier

                        _, ax = choose_identifier(docs[0].get("identifier") or [])
                        if ax:
                            arx = strip_arxiv_version(ax)
            elif arx:
                r = ads_query(f"arxiv:{arx}", rows=1, fl=["bibcode", "identifier"])
                docs = r.get("response", {}).get("docs", [])
                if docs and docs[0].get("bibcode"):
                    bib = str(docs[0]["bibcode"])
                    note = "ads_arxiv"
        except Exception as e:
            note = f"ads_error:{e}"

    return StandardRef(
        raw_line=raw,
        kind="fragment",
        bibcode=bib,
        arxiv_id=arx,
        doi=doi,
        library_hit=False,
        resolution_note=note or ("fragment" if (arx or doi) else "fragment_no_ids"),
    )


def _ads_doc_first_doi(raw: object) -> Optional[str]:
    """ADS ``doi`` field may be str or list of str."""
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        return s or None
    if isinstance(raw, list) and raw:
        s = str(raw[0]).strip()
        return s or None
    return None


def enrich_bibcode_for_acquire(ref: StandardRef) -> StandardRef:
    """Backfill arXiv and DOI from ADS when bibcode is known (improves resolver / PDF URLs)."""
    import os

    token = (os.environ.get("ADS_API_TOKEN") or "").strip()

    if token and ref.bibcode and (not ref.arxiv_id or not ref.doi):
        try:
            r = ads_query(
                f'bibcode:"{ref.bibcode}"',
                rows=1,
                fl=["identifier", "arxiv_url", "doi"],
            )
            docs = r.get("response", {}).get("docs", [])
            if docs:
                doc = docs[0]
                from research_library.lookup import choose_identifier

                ax = ref.arxiv_id
                if not ax:
                    _, ax_id = choose_identifier(doc.get("identifier") or [])
                    if ax_id:
                        ax = strip_arxiv_version(ax_id)
                doi_val = ref.doi or _ads_doc_first_doi(doc.get("doi"))
                if ax != ref.arxiv_id or doi_val != ref.doi:
                    note = (ref.resolution_note or "").strip()
                    bits: list[str] = []
                    if ax and not ref.arxiv_id:
                        bits.append("ads_arxiv_backfill")
                    if doi_val and not ref.doi:
                        bits.append("ads_doi_backfill")
                    extra = "|".join(bits)
                    resolution: Optional[str]
                    if note and extra:
                        resolution = f"{note}|{extra}"
                    elif note:
                        resolution = note
                    elif extra:
                        resolution = extra
                    else:
                        resolution = ref.resolution_note
                    return StandardRef(
                        raw_line=ref.raw_line,
                        kind=ref.kind,
                        bibcode=ref.bibcode,
                        arxiv_id=ax or ref.arxiv_id,
                        doi=doi_val or ref.doi,
                        library_hit=ref.library_hit,
                        resolution_note=resolution,
                    )
        except Exception:
            pass

    if ref.bibcode:
        return ref

    if not token:
        return ref
    try:
        if ref.doi:
            r = ads_query(f'doi:"{ref.doi}"', rows=1, fl=["bibcode", "identifier"])
            docs = r.get("response", {}).get("docs", [])
            if docs and docs[0].get("bibcode"):
                bib = str(docs[0]["bibcode"])
                arx = ref.arxiv_id
                if not arx:
                    from research_library.lookup import choose_identifier

                    _, ax = choose_identifier(docs[0].get("identifier") or [])
                    if ax:
                        arx = strip_arxiv_version(ax)
                return StandardRef(
                    raw_line=ref.raw_line,
                    kind=ref.kind,
                    bibcode=bib,
                    arxiv_id=arx,
                    doi=ref.doi,
                    library_hit=ref.library_hit,
                    resolution_note=ref.resolution_note or "ads_doi_enrich",
                )
        if ref.arxiv_id:
            r = ads_query(f"arxiv:{ref.arxiv_id}", rows=1, fl=["bibcode", "identifier"])
            docs = r.get("response", {}).get("docs", [])
            if docs and docs[0].get("bibcode"):
                return StandardRef(
                    raw_line=ref.raw_line,
                    kind=ref.kind,
                    bibcode=str(docs[0]["bibcode"]),
                    arxiv_id=ref.arxiv_id,
                    doi=ref.doi,
                    library_hit=ref.library_hit,
                    resolution_note=ref.resolution_note or "ads_arxiv_enrich",
                )
    except Exception:
        pass
    return ref
