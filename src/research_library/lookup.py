#!/usr/bin/env python3
"""Search astronomy papers via ADS first, then arXiv.

Supports:
- title: exact or near-exact title search
- query: fuzzy free-text search
- ref: parse a bibliographic reference string and search it
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

ADS_API_URL = "https://api.adsabs.harvard.edu/v1/search/query"
ADS_RESOLVER_API = "https://api.adsabs.harvard.edu/v1/resolver"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "research-library/0.1 (+https://github.com/local)"


class ADSAPIBlockedError(RuntimeError):
    """ADS returned HTML challenge / HTTP error typical of blocked datacenter or captcha."""

    pass

JOURNAL_ALIASES = {
    "A&A": "A&A",
    "AA": "A&A",
    "ASTRONOMY & ASTROPHYSICS": "A&A",
    "APJ": "ApJ",
    "ASTROPHYSICAL JOURNAL": "ApJ",
    "APJL": "ApJL",
    "APJS": "ApJS",
    "MNRAS": "MNRAS",
    "MNRA": "MNRAS",
    "MONTHLY NOTICES OF THE ROYAL ASTRONOMICAL SOCIETY": "MNRAS",
    "MON NOT R ASTRON SOC": "MNRAS",
    "AJ": "AJ",
    "ASTRONOMICAL JOURNAL": "AJ",
    "PASP": "PASP",
    "PUBLICATIONS OF THE ASTRONOMICAL SOCIETY OF THE PACIFIC": "PASP",
    "NATURE": "Nature",
    "SCIENCE": "Science",
    "ARA&A": "ARA&A",
    "ANNUAL REVIEW OF ASTRONOMY AND ASTROPHYSICS": "ARA&A",
    "BAAA": "BAAA",
    "BOLETIN DE LA ASOCIACION ARGENTINA DE ASTRONOMIA": "BAAA",
    "ICARUS": "Icarus",
    "ASTRON ASTROPHYS REV": "Astron Astrophys Rev",
    "PASJ": "PASJ",
    "PUBLICATIONS OF THE ASTRONOMICAL SOCIETY OF JAPAN": "PASJ",
}

# Canonical journal label → ADS ``bibstem`` (bibcode segment after year). Used when ``pub:`` is too strict.
JOURNAL_BIBSTEM: Dict[str, str] = {
    "MNRAS": "MNRAS",
    "ApJ": "ApJ",
    "ApJL": "ApJL",
    "ApJS": "ApJS",
    "A&A": "A&A",
    "AJ": "AJ",
    "PASP": "PASP",
    "Nature": "Nature",
    "Science": "Science",
    "ARA&A": "ARA&A",
    "BAAA": "BAAA",
    "Icarus": "Icarus",
    "Astron Astrophys Rev": "A&ARv",
    "PASJ": "PASJ",
}

ADS_FIELDS = [
    "title",
    "author",
    "year",
    "pub",
    "volume",
    "issue",
    "page",
    "bibcode",
    "doi",
    "identifier",
    "eid",
    "first_author",
    "abstract",
]


@dataclass
class ParsedReference:
    raw: str
    authors_text: Optional[str] = None
    first_author: Optional[str] = None
    year: Optional[str] = None
    journal: Optional[str] = None
    volume: Optional[str] = None
    page: Optional[str] = None
    issue: Optional[str] = None


@dataclass
class Candidate:
    source: str
    score: float
    title: str
    authors: List[str] = field(default_factory=list)
    year: Optional[str] = None
    journal: Optional[str] = None
    volume: Optional[str] = None
    page: Optional[str] = None
    bibcode: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    ads_url: Optional[str] = None
    arxiv_url: Optional[str] = None
    reason: Optional[str] = None


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_title(text: str) -> str:
    text = text.replace("–", "-").replace("—", "-").replace("“", '"').replace("”", '"')
    text = normalize_space(text)
    text = re.sub(r"[:;,]+$", "", text)
    return text


def canonical_journal(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    key = normalize_space(text).upper().replace(".", "")
    return JOURNAL_ALIASES.get(key, normalize_space(text))


def journal_to_ads_bibstem(canonical: Optional[str]) -> Optional[str]:
    """Map our canonical journal label to ADS ``bibstem`` (bibcode segment after year)."""
    if not canonical:
        return None
    c = normalize_space(canonical)
    if c in JOURNAL_BIBSTEM:
        return JOURNAL_BIBSTEM[c]
    cup = c.upper().replace(".", "")
    for label, stem in JOURNAL_BIBSTEM.items():
        if label.upper().replace(".", "") == cup:
            return stem
    return None


def _ads_bibstem_query_field(stem: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9]+", stem):
        return f"bibstem:{stem}"
    return f'bibstem:"{stem}"'


def parse_reference(ref_text: str) -> ParsedReference:
    text = normalize_space(ref_text)
    parsed = ParsedReference(raw=text)
    work = re.sub(r"^\[\d+\]\s*", "", text)
    work = re.sub(r"^\d+\.\s+", "", work)
    work = re.sub(r"^\(?\d+\)?[\.\)]\s*", "", work)
    work = normalize_space(work)

    year_paren = re.search(r"\((19|20)\d{2}\)", work)
    year_plain = re.search(r"\b(19|20)\d{2}\b", work)
    if year_paren and (not year_plain or year_paren.start() <= year_plain.start()):
        parsed.year = year_paren.group(0)[1:-1]
        before = work[: year_paren.start()].strip(" ,.;")
        after = work[year_paren.end() :].strip(" ,.;")
    elif year_plain:
        parsed.year = year_plain.group(0)
        before = work[: year_plain.start()].strip(" ,.;")
        after = work[year_plain.end() :].strip(" ,.;")
    else:
        return parsed

    parsed.authors_text = before or None
    if before:
        head = re.split(r",|\bet\s+al\.?", before, maxsplit=1, flags=re.I)[0].strip()
        m = re.search(r"([A-Z][A-Za-z'`\-]+)", head)
        if m:
            parsed.first_author = m.group(1)

    parts = [p.strip(" .") for p in re.split(r",", after) if p.strip(" .")]
    if parts and parsed.year and re.fullmatch(r"\d{4}", parts[0]) and parts[0] == parsed.year:
        parts = parts[1:]

    journal = None
    volume = None
    page = None
    issue = None

    letter_idxs = [
        i
        for i, p in enumerate(parts)
        if re.search(r"[A-Za-z&]", p.replace("\\&", "&"))
    ]
    if letter_idxs:
        ji = letter_idxs[0]
        journal = parts[ji].replace("\\&", "&")
        tail = parts[ji + 1 :]
    else:
        tail = parts

    page_pat = r"[A-Za-z]?\d+[A-Za-z0-9\-–]*"
    if len(tail) == 1:
        t0 = tail[0].replace("–", "-")
        if re.fullmatch(r"\d+[A-Za-z]?", t0):
            volume = t0
        elif re.fullmatch(page_pat, t0):
            page = t0
    elif len(tail) == 2:
        t0, t1 = tail[0], tail[1].replace("–", "-")
        if re.fullmatch(r"\d+[A-Za-z]?", t0) and (
            re.fullmatch(page_pat, t1) or re.fullmatch(r"\d+", t1)
        ):
            volume, page = t0, t1
    elif len(tail) >= 3:
        t0, t1, t2 = tail[0], tail[1], tail[2].replace("–", "-")
        if (
            re.fullmatch(r"\d+[A-Za-z]?", t0)
            and re.fullmatch(r"\d+", t1)
            and re.fullmatch(page_pat, t2)
        ):
            volume, issue, page = t0, t1, t2
        elif re.fullmatch(r"\d+[A-Za-z]?", t0) and re.fullmatch(page_pat, t1):
            volume, page = t0, t1

    parsed.journal = canonical_journal(journal)
    parsed.volume = volume
    parsed.page = page
    parsed.issue = issue

    return parsed


def http_get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_retry_attempts() -> int:
    try:
        return max(1, int((os.environ.get("RESEARCH_HTTP_RETRY_ATTEMPTS") or "3").strip()))
    except ValueError:
        return 3


def _http_retry_base_delay() -> float:
    try:
        return max(0.1, float((os.environ.get("RESEARCH_HTTP_RETRY_BASE_DELAY") or "0.8").strip()))
    except ValueError:
        return 0.8


def http_get_with_retry(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    *,
    attempts: Optional[int] = None,
    retry_on_status: Tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504),
) -> bytes:
    """``http_get`` with exponential backoff on transient failures.

    Re-raises the last exception/HTTPError on permanent failure so callers can
    keep their existing error-handling paths. 4xx responses other than the
    retry set propagate immediately.
    """
    import time

    n = attempts if attempts is not None else _http_retry_attempts()
    base = _http_retry_base_delay()
    last_exc: Optional[BaseException] = None
    for i in range(max(1, n)):
        try:
            return http_get(url, headers=headers, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code not in retry_on_status or i == n - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if i == n - 1:
                raise
        sleep_s = base * (2 ** i)
        time.sleep(min(sleep_s, 10.0))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("http_get_with_retry: exhausted without an exception")


def _ads_challenge_response(http_code: int, body_text: str) -> bool:
    bl = body_text.lower()
    if http_code in (401, 403, 405, 429) and (
        "human verification" in bl
        or (body_text.lstrip().startswith("<!") and "html" in bl[:300])
    ):
        return True
    if "human verification" in bl and "html" in bl[:400]:
        return True
    return False


def _ads_query_via_curl(url: str, token: str, timeout: int = 60) -> bytes:
    """ADS/WAF often fingerprints urllib/requests; curl typically receives JSON."""
    import shutil
    import subprocess

    if not shutil.which("curl"):
        raise ADSAPIBlockedError(
            "ADS blocked Python's HTTP client (HTML challenge). Install curl for automatic fallback, "
            "or run from an environment where urllib is not fingerprinted."
        )
    r = subprocess.run(
        [
            "curl",
            "-sS",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Accept: application/json",
            url,
        ],
        capture_output=True,
        timeout=timeout,
    )
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace")[:400]
        raise ADSAPIBlockedError(f"curl ADS fallback failed (exit {r.returncode}): {err}") from None
    out = r.stdout or b""
    head = out[:900].decode("utf-8", errors="replace").lower()
    if out.lstrip().startswith(b"<") or "human verification" in head:
        raise ADSAPIBlockedError(
            "curl ADS fallback still got an HTML challenge page; check ADS_API_TOKEN and network."
        )
    return out


def _ads_api_get_json_optional(url: str) -> Optional[Any]:
    """GET an ADS API URL with token. Returns None on 404/400 or parse failure."""
    token = (os.environ.get("ADS_API_TOKEN") or "").strip()
    if not token:
        return None
    hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        try:
            payload = http_get(url, headers=hdrs, timeout=60)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            body = e.read().decode("utf-8", errors="replace")
            if _ads_challenge_response(e.code, body):
                payload = _ads_query_via_curl(url, token)
            else:
                return None
        else:
            if payload.lstrip().startswith(b"<!") or b"Human Verification" in payload[:1200]:
                payload = _ads_query_via_curl(url, token)
        return json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, ADSAPIBlockedError, OSError, urllib.error.URLError, TypeError):
        return None
    except Exception:
        return None


def _pdf_links_from_esource_records(records: Any) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {"pub": None, "ads": None, "eprint": None, "arxiv": None}
    if not isinstance(records, list):
        return out
    for rec in records:
        if not isinstance(rec, dict):
            continue
        lt = (rec.get("link_type") or "").upper()
        u = rec.get("url")
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        if "EPRINT_PDF" in lt:
            out["eprint"] = u
            if "arxiv.org/pdf/" in u.lower():
                out["arxiv"] = u
        elif "PUB_PDF" in lt:
            out["pub"] = u
        elif "ADS_PDF" in lt or "ADS_SCAN" in lt:
            out["ads"] = u
    return out


def _ads_resolver_redirect_link(bibcode: str, suffix: str) -> Optional[str]:
    enc = urllib.parse.quote(bibcode.strip(), safe=".")
    data = _ads_api_get_json_optional(f"{ADS_RESOLVER_API}/{enc}/{suffix}")
    if not isinstance(data, dict):
        return None
    link = data.get("link") or data.get("service")
    if isinstance(link, str) and link.startswith("http"):
        return link
    return None


def _resolver_pdf_urls_via_api(bibcode: str) -> Dict[str, Optional[str]]:
    """Resolve publisher/download URLs via api.adsabs (not ui link_gateway — WAF returns empty body)."""
    enc = urllib.parse.quote(bibcode.strip(), safe=".")
    data = _ads_api_get_json_optional(f"{ADS_RESOLVER_API}/{enc}/esource")
    records: Any = None
    if isinstance(data, dict):
        links_obj = data.get("links")
        if isinstance(links_obj, dict):
            records = links_obj.get("records")
    got = _pdf_links_from_esource_records(records)
    for suffix, key in (("ads_pdf", "ads"), ("eprint_pdf", "eprint"), ("pub_pdf", "pub")):
        if got.get(key):
            continue
        u = _ads_resolver_redirect_link(bibcode, suffix)
        if u:
            got[key] = u
            if key == "eprint" and "arxiv.org/pdf/" in u.lower() and not got.get("arxiv"):
                got["arxiv"] = u
    return got


def ads_query(
    query: str,
    rows: int = 10,
    *,
    fl: Optional[List[str]] = None,
) -> Dict[str, Any]:
    token = os.environ.get("ADS_API_TOKEN")
    if not token:
        raise RuntimeError("ADS_API_TOKEN is not set")
    fields = fl if fl is not None else ADS_FIELDS
    params = urllib.parse.urlencode({
        "q": query,
        "rows": str(rows),
        "fl": ",".join(fields),
        "sort": "score desc",
    })
    url = f"{ADS_API_URL}?{params}"
    hdrs = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    payload: bytes
    try:
        payload = http_get(url, headers=hdrs)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if _ads_challenge_response(e.code, body):
            payload = _ads_query_via_curl(url, token)
        else:
            raise
    else:
        if payload.lstrip().startswith(b"<!") or b"Human Verification" in payload[:1200]:
            payload = _ads_query_via_curl(url, token)
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise ADSAPIBlockedError(
            f"ADS response was not JSON (client may be blocked). Decode error: {e}"
        ) from e


def build_ads_link(bibcode: Optional[str]) -> Optional[str]:
    if not bibcode:
        return None
    return f"https://ui.adsabs.harvard.edu/abs/{urllib.parse.quote(bibcode)}/abstract"


def choose_identifier(identifiers: Iterable[str]) -> Tuple[Optional[str], Optional[str]]:
    doi = None
    arxiv_id = None
    for ident in identifiers or []:
        if ident.lower().startswith("doi:"):
            doi = ident[4:]
        if ident.lower().startswith("arxiv:"):
            arxiv_id = ident.split(":", 1)[1]
    return doi, arxiv_id


def candidate_from_ads(doc: Dict[str, Any], reason: str = "ADS match") -> Candidate:
    title = (doc.get("title") or [""])[0]
    authors = doc.get("author") or []
    year = str(doc.get("year")) if doc.get("year") is not None else None
    journal = doc.get("pub")
    volume = doc.get("volume")
    page = None
    if isinstance(doc.get("page"), list) and doc["page"]:
        page = doc["page"][0]
    elif isinstance(doc.get("page"), str):
        page = doc.get("page")
    if not page:
        page = doc.get("eid")
    doi = None
    arxiv_id = None
    if isinstance(doc.get("doi"), list) and doc["doi"]:
        doi = doc["doi"][0]
    elif isinstance(doc.get("doi"), str):
        doi = doc.get("doi")
    if not doi or not arxiv_id:
        d2, a2 = choose_identifier(doc.get("identifier") or [])
        doi = doi or d2
        arxiv_id = arxiv_id or a2
    arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None
    return Candidate(
        source="ADS",
        score=float(doc.get("score", 0.0)),
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        volume=volume,
        page=page,
        bibcode=doc.get("bibcode"),
        doi=doi,
        arxiv_id=arxiv_id,
        ads_url=build_ads_link(doc.get("bibcode")),
        arxiv_url=arxiv_url,
        reason=reason,
    )


def arxiv_query(search_query: str, max_results: int = 10) -> List[Candidate]:
    params = urllib.parse.urlencode({
        "search_query": search_query,
        "start": "0",
        "max_results": str(max_results),
        "sortBy": "relevance",
        "sortOrder": "descending",
    })
    url = f"{ARXIV_API_URL}?{params}"
    xml_data = http_get(url)
    root = ET.fromstring(xml_data)
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    out: List[Candidate] = []
    for entry in root.findall("a:entry", ns):
        title = normalize_space(entry.findtext("a:title", default="", namespaces=ns))
        authors = [normalize_space(a.findtext("a:name", default="", namespaces=ns)) for a in entry.findall("a:author", ns)]
        published = entry.findtext("a:published", default="", namespaces=ns)
        year = published[:4] if published else None
        entry_id = entry.findtext("a:id", default="", namespaces=ns)
        arxiv_id = entry_id.rsplit("/", 1)[-1] if entry_id else None
        doi = entry.findtext("arxiv:doi", default=None, namespaces=ns)
        out.append(Candidate(
            source="arXiv",
            score=0.0,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            arxiv_id=arxiv_id,
            arxiv_url=entry_id or None,
            reason="arXiv fallback",
        ))
    return out


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def score_title_candidates(candidates: List[Candidate], title: str) -> List[Candidate]:
    for c in candidates:
        c.score += similarity(c.title, title) * 100.0
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def ads_search_title(title: str) -> List[Candidate]:
    queries = [
        f'title:"{title}"',
        f'title:({title})',
        normalize_space(title),
    ]
    seen: Dict[Tuple[str, str], Candidate] = {}
    for q in queries:
        try:
            result = ads_query(q, rows=8)
        except ADSAPIBlockedError:
            raise
        except Exception:
            continue
        for doc in result.get("response", {}).get("docs", []):
            cand = candidate_from_ads(doc, reason=f'ADS title search: {q}')
            key = (cand.source, cand.bibcode or cand.title)
            if key not in seen or seen[key].score < cand.score:
                seen[key] = cand
    return score_title_candidates(list(seen.values()), title)


def ads_search_query(text: str) -> List[Candidate]:
    queries = [normalize_space(text)]
    seen: Dict[Tuple[str, str], Candidate] = {}
    for q in queries:
        try:
            result = ads_query(q, rows=10)
        except ADSAPIBlockedError:
            raise
        except Exception:
            continue
        for doc in result.get("response", {}).get("docs", []):
            cand = candidate_from_ads(doc, reason=f'ADS free-text search: {q}')
            key = (cand.source, cand.bibcode or cand.title)
            if key not in seen or seen[key].score < cand.score:
                seen[key] = cand
    return sorted(seen.values(), key=lambda c: c.score, reverse=True)


def ads_search_reference(parsed: ParsedReference) -> List[Candidate]:
    author_f = f'author:"{parsed.first_author}"' if parsed.first_author else None
    year_f = f"year:{parsed.year}" if parsed.year else None
    vol_f = f"volume:{parsed.volume}" if parsed.volume else None
    page_f = f"(page:{parsed.page} OR eid:{parsed.page})" if parsed.page else None
    issue_f = f"issue:{parsed.issue}" if parsed.issue else None
    pub_f = f'pub:"{parsed.journal}"' if parsed.journal else None
    stem = journal_to_ads_bibstem(parsed.journal) if parsed.journal else None
    bib_f = _ads_bibstem_query_field(stem) if stem else None

    queries: List[str] = []

    def add_and(parts: List[Optional[str]]) -> None:
        xs = [p for p in parts if p]
        if len(xs) >= 2:
            q = " AND ".join(xs)
            if q not in queries:
                queries.append(q)

    add_and([author_f, year_f, pub_f, vol_f, page_f, issue_f])
    add_and([author_f, year_f, bib_f, vol_f, page_f, issue_f])
    add_and([author_f, year_f, vol_f, page_f, issue_f])
    add_and([author_f, year_f, bib_f, vol_f, issue_f])
    add_and([author_f, year_f, pub_f, vol_f])
    add_and([author_f, year_f, bib_f, vol_f])
    add_and([author_f, year_f, pub_f])
    add_and([author_f, year_f, bib_f])

    fallback_parts = [
        p
        for p in [
            parsed.authors_text,
            parsed.year,
            parsed.journal,
            parsed.volume,
            parsed.issue,
            parsed.page,
        ]
        if p
    ]
    if fallback_parts:
        q = " ".join(fallback_parts)
        if q not in queries:
            queries.append(q)

    seen: Dict[Tuple[str, str], Candidate] = {}
    for q in queries:
        try:
            result = ads_query(q, rows=15)
        except ADSAPIBlockedError:
            raise
        except Exception:
            continue
        for doc in result.get("response", {}).get("docs", []):
            cand = candidate_from_ads(doc, reason=f'ADS reference search: {q}')
            bonus = 0.0
            if parsed.year and cand.year == parsed.year:
                bonus += 20
            if parsed.volume and (cand.volume or "") == parsed.volume:
                bonus += 15
            if parsed.page and (cand.page or "").upper() == parsed.page.upper():
                bonus += 20
            if parsed.issue and str(doc.get("issue") or "") == str(parsed.issue):
                bonus += 10
            if parsed.journal and cand.journal and parsed.journal.lower() in cand.journal.lower():
                bonus += 15
            cand.score += bonus
            key = (cand.source, cand.bibcode or cand.title)
            if key not in seen or seen[key].score < cand.score:
                seen[key] = cand
    return sorted(seen.values(), key=lambda c: c.score, reverse=True)


def fallback_arxiv_for_title(title: str) -> List[Candidate]:
    phrase = title.replace('"', '')
    queries = [f'ti:"{phrase}"', f'all:"{phrase}"']
    seen: Dict[str, Candidate] = {}
    for q in queries:
        try:
            for cand in arxiv_query(q, max_results=8):
                cand.score = similarity(cand.title, title) * 80.0
                seen[cand.arxiv_id or cand.title] = cand
        except Exception:
            continue
    return sorted(seen.values(), key=lambda c: c.score, reverse=True)


def fallback_arxiv_for_query(text: str) -> List[Candidate]:
    q = f'all:"{text.replace(chr(34), "")}"'
    try:
        return arxiv_query(q, max_results=8)
    except Exception:
        return []


def fallback_arxiv_for_reference(parsed: ParsedReference) -> List[Candidate]:
    terms = [
        t for t in [parsed.first_author, parsed.year, parsed.journal, parsed.volume, parsed.issue, parsed.page] if t
    ]
    if not terms and parsed.raw:
        terms = [parsed.raw]
    query = " AND ".join([f'all:"{str(t)}"' for t in terms])
    try:
        return arxiv_query(query, max_results=8)
    except Exception:
        return []


def merge_and_rank(primary: List[Candidate], fallback: List[Candidate], top_n: int = 8) -> List[Candidate]:
    seen: Dict[Tuple[str, str], Candidate] = {}
    for cand in primary + fallback:
        key = (cand.source, cand.bibcode or cand.arxiv_id or cand.title)
        if key not in seen or seen[key].score < cand.score:
            seen[key] = cand
    ranked = sorted(seen.values(), key=lambda c: (c.source != "ADS", -c.score, c.year or ""))
    return ranked[:top_n]


def search_candidates_auto(
    text: str,
    *,
    ads_enabled: Optional[bool] = None,
    top_n: int = 8,
) -> List[Candidate]:
    """Heuristic ADS + arXiv merge (catalog line / reference / free-text). Used by ``library.search``."""
    if ads_enabled is None:
        ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))
    from research_library.library.reference_parse import (
        _classify_catalog_line,
        _extract_catalog_identifiers,
    )

    raw = normalize_space(text)
    cat = _classify_catalog_line(raw)
    bib, arx, doi = _extract_catalog_identifiers(raw, cat)

    def from_docs(docs: List[Dict[str, Any]], reason: str) -> List[Candidate]:
        return [candidate_from_ads(d, reason) for d in docs]

    primary: List[Candidate] = []
    fallback: List[Candidate] = []

    if cat == "bibcode" and bib:
        if ads_enabled:
            try:
                r = ads_query(f'bibcode:"{bib}"', rows=max(top_n, 10))
                primary = from_docs(r.get("response", {}).get("docs", []), "ADS bibcode")
            except (ADSAPIBlockedError, Exception):
                primary = []
        return merge_and_rank(primary, fallback, top_n=top_n)

    if cat == "doi" and doi:
        if ads_enabled:
            try:
                r = ads_query(f'doi:"{doi}"', rows=max(top_n, 10))
                primary = from_docs(r.get("response", {}).get("docs", []), "ADS doi")
            except (ADSAPIBlockedError, Exception):
                primary = []
        try:
            fallback = arxiv_query(f'all:"{doi.replace(chr(34), "")}"', max_results=top_n)
        except Exception:
            fallback = []
        return merge_and_rank(primary, fallback, top_n=top_n)

    if cat == "arxiv" and arx:
        if ads_enabled:
            try:
                r = ads_query(f"arxiv:{arx}", rows=max(top_n, 10))
                primary = from_docs(r.get("response", {}).get("docs", []), "ADS arxiv")
            except (ADSAPIBlockedError, Exception):
                primary = []
        try:
            fallback = arxiv_query(f"all:{arx}", max_results=top_n)
        except Exception:
            fallback = []
        return merge_and_rank(primary, fallback, top_n=top_n)

    nt = normalize_title(raw)
    parsed = parse_reference(nt)
    if parsed.year and (parsed.journal or parsed.volume or parsed.page):
        primary = ads_search_reference(parsed) if ads_enabled else []
        fallback = fallback_arxiv_for_reference(parsed)
    else:
        primary = ads_search_query(nt) if ads_enabled else []
        fallback = fallback_arxiv_for_query(nt)
    return merge_and_rank(primary, fallback, top_n=top_n)


def fetch_bibtex(bibcode: str) -> Optional[str]:
    out = fetch_bibtex_bulk([bibcode])
    return out if out else None


def fetch_bibtex_bulk(bibcodes: List[str]) -> Optional[str]:
    """Fetch ADS-export BibTeX for many bibcodes (same format as single export)."""
    clean = [b.strip() for b in bibcodes if b and b.strip()]
    if not clean:
        return ""
    token = os.environ.get("ADS_API_TOKEN")
    if not token:
        return None
    chunks: List[str] = []
    try:
        import urllib.request

        for i in range(0, len(clean), 200):
            part = clean[i : i + 200]
            url = "https://api.adsabs.harvard.edu/v1/export/bibtex"
            payload = json.dumps({"bibcode": part}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                exp = (data.get("export") or "").strip()
                if exp:
                    chunks.append(exp)
        return "\n\n".join(chunks).strip() if chunks else None
    except Exception:
        return None


def ads_fetch_doc_by_bibcode(bibcode: str) -> Optional[Dict[str, Any]]:
    """One ADS record including abstract (for local ingest)."""
    try:
        result = ads_query(f'bibcode:"{bibcode}"', rows=1)
        docs = result.get("response", {}).get("docs", [])
        return docs[0] if docs else None
    except Exception:
        return None


def render_text(results: List[Candidate], ads_enabled: bool) -> str:
    lines = []
    if not ads_enabled:
        lines.append("Note: ADS_API_TOKEN is not set, so ADS was skipped and only arXiv fallback was used.\n")
    if not results:
        return "No candidates found."
    for i, c in enumerate(results, start=1):
        lines.append(f"[{i}] {c.title}")
        meta = []
        if c.authors:
            meta.append(", ".join(c.authors[:4]) + (" et al." if len(c.authors) > 4 else ""))
        if c.year:
            meta.append(c.year)
        if c.journal:
            j = c.journal
            if c.volume:
                j += f", {c.volume}"
            if c.page:
                j += f", {c.page}"
            meta.append(j)
        lines.append(f"    Source: {c.source} | Score: {c.score:.1f}")
        if meta:
            lines.append(f"    {' | '.join(meta)}")
        if c.bibcode:
            lines.append(f"    Bibcode: {c.bibcode}")
        if c.doi:
            lines.append(f"    DOI: {c.doi}")
        if c.ads_url:
            lines.append(f"    ADS: {c.ads_url}")
        if c.arxiv_url:
            lines.append(f"    arXiv: {c.arxiv_url}")
        if c.reason:
            lines.append(f"    Why: {c.reason}")
        lines.append("")
    return "\n".join(lines).rstrip()


def cmd_search(args: argparse.Namespace) -> int:
    ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))
    if args.mode == "title":
        title = normalize_title(args.title)
        primary = ads_search_title(title) if ads_enabled else []
        fallback = fallback_arxiv_for_title(title)
        results = merge_and_rank(primary, fallback)
        payload: Dict[str, Any] = {"mode": "title", "query": title, "results": [asdict(r) for r in results], "ads_enabled": ads_enabled}
    elif args.mode == "query":
        text = normalize_title(args.text)
        primary = ads_search_query(text) if ads_enabled else []
        fallback = fallback_arxiv_for_query(text)
        results = merge_and_rank(primary, fallback)
        payload = {"mode": "query", "query": text, "results": [asdict(r) for r in results], "ads_enabled": ads_enabled}
    else:
        parsed = parse_reference(args.text)
        primary = ads_search_reference(parsed) if ads_enabled else []
        fallback = fallback_arxiv_for_reference(parsed)
        results = merge_and_rank(primary, fallback)
        payload = {
            "mode": "ref",
            "query": parsed.raw,
            "parsed_reference": asdict(parsed),
            "results": [asdict(r) for r in results],
            "ads_enabled": ads_enabled,
        }

    if getattr(args, "json"):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_text(results, ads_enabled))
    return 0


def cmd_bibtex(args: argparse.Namespace) -> int:
    """Fetch and print BibTeX for a given bibcode or arXiv ID."""
    ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))
    bibcode = args.bibcode
    arxiv_id = args.arxiv

    if arxiv_id and not bibcode:
        # Try to resolve arXiv ID to bibcode via ADS
        if ads_enabled:
            try:
                result = ads_query(f"arxiv:{arxiv_id}", rows=1)
                docs = result.get("response", {}).get("docs", [])
                if docs:
                    bibcode = docs[0].get("bibcode")
            except Exception:
                pass
        if not bibcode:
            msg = "BibTeX lookup by arXiv ID requires ADS_API_TOKEN. Please provide --bibcode instead."
            if getattr(args, "json"):
                print(json.dumps({"ok": False, "error": msg}))
            else:
                print(msg)
            return 1

    if not bibcode:
        msg = "Please provide either --bibcode or --arxiv"
        if getattr(args, "json"):
            print(json.dumps({"ok": False, "error": msg}))
        else:
            print(msg)
        return 1

    bibtex = fetch_bibtex(bibcode)
    if bibtex:
        if getattr(args, "json"):
            print(json.dumps({"ok": True, "bibcode": bibcode, "bibtex": bibtex}, ensure_ascii=False, indent=2))
        else:
            print(bibtex)
        return 0
    else:
        msg = "Could not fetch BibTeX" if ads_enabled else "BibTeX lookup requires ADS_API_TOKEN"
        if getattr(args, "json"):
            print(json.dumps({"ok": False, "error": msg, "bibcode": bibcode}))
        else:
            print(msg)
        return 1


def fetch_pdf_links(bibcode: str, arxiv_id_hint: Optional[str] = None) -> Dict[str, Optional[str]]:
    """Get PDF URLs for a bibcode.

    With ``ADS_API_TOKEN``, uses ``api.adsabs…/v1/resolver`` (direct arXiv / publisher URLs).
    ``ui.adsabs…/link_gateway`` often triggers AWS WAF and returns an empty body for curl.
    """
    links: Dict[str, Optional[str]] = {
        "pub": None,
        "ads": None,
        "eprint": None,
        "arxiv": None,
    }

    if arxiv_id_hint:
        ax = re.sub(r"v\d+$", "", arxiv_id_hint.strip(), flags=re.IGNORECASE)
        links["arxiv"] = f"https://arxiv.org/pdf/{ax}.pdf"

    token = (os.environ.get("ADS_API_TOKEN") or "").strip()
    if token:
        try:
            resolved = _resolver_pdf_urls_via_api(bibcode)
            for k in ("pub", "ads", "eprint", "arxiv"):
                if resolved.get(k) and not links.get(k):
                    links[k] = resolved[k]
        except Exception:
            pass
        if not links.get("arxiv"):
            try:
                data = ads_query(
                    f"bibcode:{bibcode}",
                    rows=1,
                    fl=["identifier", "arxiv_url"],
                )
                docs = data.get("response", {}).get("docs", [])
                if docs:
                    doc = docs[0]
                    arxiv_url = doc.get("arxiv_url") or ""
                    arxiv_id_built: Optional[str] = None
                    if isinstance(arxiv_url, str) and "arxiv.org" in arxiv_url:
                        tail = arxiv_url.rstrip("/").split("/")[-1]
                        arxiv_id_built = re.sub(
                            r"^arx[i]v:",
                            "",
                            tail,
                            flags=re.IGNORECASE,
                        )
                    if not arxiv_id_built:
                        _, ax = choose_identifier(doc.get("identifier") or [])
                        if ax:
                            arxiv_id_built = re.sub(
                                r"v\d+$", "", ax.strip(), flags=re.IGNORECASE
                            )
                    if arxiv_id_built:
                        links["arxiv"] = (
                            f"https://arxiv.org/pdf/{arxiv_id_built}.pdf"
                        )
            except Exception:
                pass

    if not token:
        bc = bibcode.strip()
        links["pub"] = links["pub"] or f"https://ui.adsabs.harvard.edu/link_gateway/{bc}/PUB_PDF"
        links["ads"] = links["ads"] or f"https://ui.adsabs.harvard.edu/link_gateway/{bc}/ADS_PDF"
        links["eprint"] = links["eprint"] or f"https://ui.adsabs.harvard.edu/link_gateway/{bc}/EPRINT_PDF"

    return links


def download_pdf(output_path: str, url: str, timeout: int = 60) -> bool:
    """Download a PDF from a URL using curl.
    Uses -4 (IPv4 only) to avoid IPv6 connectivity issues in sandboxed environments.
    Returns True on success.
    """
    try:
        import shutil

        if not shutil.which("curl"):
            print("[download_pdf] curl not found", file=sys.stderr)
            return False
        cmd: List[str] = [
            "curl",
            "-sL",
            "-o",
            output_path,
            "--max-time",
            str(timeout),
            "--connect-timeout",
            "15",
            "-4",
            "--insecure",
            "-A",
            USER_AGENT,
        ]
        tok = (os.environ.get("ADS_API_TOKEN") or "").strip()
        if tok and "ui.adsabs.harvard.edu" in url:
            cmd.extend(["-H", f"Authorization: Bearer {tok}"])
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if result.returncode != 0:
            print(f"[download_pdf] curl RC={result.returncode} stderr={result.stderr[:200]}", file=sys.stderr)
            return False
        if not os.path.exists(output_path):
            print(f"[download_pdf] file not created", file=sys.stderr)
            return False
        sz = os.path.getsize(output_path)
        if sz == 0:
            os.remove(output_path)
            print(
                "[download_pdf] empty file (0 bytes); ui.adsabs link_gateway often returns WAF 202 with no body — "
                "set ADS_API_TOKEN so fetch_pdf_links uses api.adsabs resolver URLs",
                file=sys.stderr,
            )
            return False
        with open(output_path, "rb") as f:
            header = f.read(20)
        if b"%PDF" not in header:
            os.remove(output_path)
            hint = ""
            if "ui.adsabs.harvard.edu" in url:
                hint = " (likely HTML/WAF, not PDF; prefer resolver URLs from fetch_pdf_links)"
            print(f"[download_pdf] not a PDF, header={header!r} size_was={sz}{hint}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"[download_pdf] exception: {e}", file=sys.stderr)
        return False


def cmd_download(args: argparse.Namespace) -> int:
    """Download PDF(s) for a paper, trying all available sources in order.

    Try order (unless overridden by flags):
      1. PUB_PDF    — journal/publisher version
      2. ADS_PDF    — ADS scanned version (for very old papers, free)
      3. EPRINT_PDF — EPRINT link from ADS (often an arXiv mirror)
      4. arXiv.org  — direct arXiv PDF (if arXiv ID known)
    """
    ads_enabled = bool(os.environ.get("ADS_API_TOKEN"))

    bibcode = args.bibcode or ""
    arxiv_id = args.arxiv or ""

    if not bibcode and not arxiv_id:
        print("Error: provide either --bibcode or --arxiv")
        return 1

    # Resolve arXiv to bibcode
    if arxiv_id and not bibcode:
        if ads_enabled:
            try:
                result = ads_query(f"arxiv:{arxiv_id}", rows=1)
                docs = result.get("response", {}).get("docs", [])
                if docs:
                    bibcode = docs[0].get("bibcode") or ""
            except Exception:
                pass

    if getattr(args, "library", False):
        from research_library.config import get_pdfs_dir
        from research_library.library import db as library_db
        from research_library.library.reference_acquire import (
            acquire_pdf,
            standard_ref_from_cli_bibcode_arxiv,
        )

        dest_dir = str(get_pdfs_dir())
        os.makedirs(dest_dir, exist_ok=True)
        safe_name = re.sub(r"[^\w\-]", "_", bibcode) if bibcode else (
            re.sub(r"[^\w\-]", "_", arxiv_id) if arxiv_id else "paper"
        )
        dest_single = os.path.join(dest_dir, f"{safe_name}.pdf")
        conn = library_db.connect()
        ref = standard_ref_from_cli_bibcode_arxiv(bibcode=bibcode, arxiv_id=arxiv_id)
        path, _reason = acquire_pdf(ref, conn, dest_single, timeout=args.timeout)
        if path and not args.keep_both:
            print(path)
            return 0
        if path and args.keep_both:
            print(f"OK: {path}", file=sys.stderr)
            print(path)
            return 0
        print("[library/acquire] falling back to per-source download attempts", file=sys.stderr)
    else:
        dest_dir = os.path.expanduser(args.dest) if args.dest else "."
    os.makedirs(dest_dir, exist_ok=True)

    safe_name = re.sub(r"[^\w\-]", "_", bibcode) if bibcode else (re.sub(r"[^\w\-]", "_", arxiv_id) if arxiv_id else "paper")

    sources: Dict[str, Tuple[Optional[str], Optional[str]]] = {}

    if bibcode and not args.arxiv_only:
        links = fetch_pdf_links(bibcode, arxiv_id_hint=arxiv_id)
        # Priority: pub > ads > eprint > arxiv (determined by download order below)
        sources["PUB_PDF"]    = (links["pub"],    os.path.join(dest_dir, f"{safe_name}_pub.pdf"))
        sources["ADS_PDF"]     = (links["ads"],    os.path.join(dest_dir, f"{safe_name}_ads.pdf"))
        sources["EPRINT_PDF"]  = (links["eprint"], os.path.join(dest_dir, f"{safe_name}_eprint.pdf"))
        sources["arXiv.org"]   = (links["arxiv"],  os.path.join(dest_dir, f"{safe_name}_arxiv.pdf")) if links["arxiv"] else (None, None)

    if arxiv_id and not args.journal_only:
        arxiv_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        arxiv_dest_path = os.path.join(dest_dir, f"{safe_name}_arxiv.pdf")
        sources["arXiv.org"] = (arxiv_url, arxiv_dest_path)

    # Determine download order (pub first, then ads, eprint, arxiv)
    order = ["PUB_PDF", "ADS_PDF", "EPRINT_PDF", "arXiv.org"]
    if args.journal_only:
        order = ["PUB_PDF"]
    if args.arxiv_only:
        order = ["arXiv.org"]

    ok_path = None
    tried = 0
    for name in order:
        if name not in sources:
            continue
        url, dest_path = sources[name]
        if not url or not dest_path:
            continue
        tried += 1
        label = f"[{tried}/{len([n for n in order if n in sources])}] {name}"
        print(f"{label}: {url}", file=sys.stderr)
        if download_pdf(dest_path, url, timeout=args.timeout):
            print(f"OK: {dest_path}", file=sys.stderr)
            if not args.keep_both:
                print(dest_path)
                return 0
            ok_path = dest_path
        else:
            print(f"FAILED or paywalled: {url}", file=sys.stderr)

    if ok_path and args.keep_both:
        print(f"Saved: {ok_path}", file=sys.stderr)

    if tried == 0:
        print("Error: no PDF sources available for this paper.", file=sys.stderr)
        return 1
    if not ok_path:
        print("Error: all PDF downloads failed.", file=sys.stderr)
        return 1
    return 0


def cmd_pdf2ads(args: argparse.Namespace) -> int:
    """Extract metadata from a PDF and find the corresponding ADS entry.

    Extraction strategy (in order of priority):
      1. DOI  → direct ADS query by DOI
      2. arXiv ID → direct ADS query by arXiv
      3. Title  → ADS title search (from pdfminer clean text)

    With --refs flag: also fetch and display the reference list from ADS.
    """
    from research_library.config import load_ads_token, load_env
    from research_library.library.pdf_identifiers import extract_pdf_identifiers
    from research_library.library.pdf_ingest import ingest_pdf_file, resolve_extracted_to_ads_match

    load_env()
    tok = load_ads_token()
    if tok:
        os.environ.setdefault("ADS_API_TOKEN", tok)

    pdf_path = os.path.expanduser(args.pdf)
    if not os.path.isfile(pdf_path):
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        return 1

    ext = extract_pdf_identifiers(pdf_path, include_clean_text=True)
    clean_text = ext.get("_clean_text") or ""
    doi = ext.get("doi")
    arxiv_id = ext.get("arxiv_id")
    title = ext.get("title_candidate")
    clean_text_len = ext.get("clean_text_len") or 0
    if clean_text_len:
        print(f"[pdf2ads] pdfminer extracted {clean_text_len} chars", file=sys.stderr)
    if doi:
        print(f"[pdf2ads] DOI: {doi}", file=sys.stderr)
    if arxiv_id:
        print(f"[pdf2ads] arXiv: {arxiv_id}", file=sys.stderr)
    if title:
        print(f"[pdf2ads] Title candidate: {title}", file=sys.stderr)

    ads_token = os.environ.get("ADS_API_TOKEN")
    ads_enabled = bool(ads_token)

    def fetch_references(bibcode: str, pdf_path_for_refs: str = "", classify_refs: bool = False, pdf_text: str = "") -> None:
        """Fetch and display the reference list for a given ADS bibcode."""
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"Fetching references for {bibcode}...", file=sys.stderr)

        token = os.environ.get("ADS_API_TOKEN")

        # Step 1: get reference bibcodes from the paper
        try:
            params = urllib.parse.urlencode({
                "q": f"bibcode:{bibcode}",
                "rows": "1",
                "fl": "bibcode,title,author,year,pub,reference",
                "sort": "score desc",
            })
            url = f"{ADS_API_URL}?{params}"
            payload = http_get(url, headers={"Authorization": f"Bearer {token}"})
            result = json.loads(payload.decode("utf-8"))
            docs = result.get("response", {}).get("docs", [])
            if not docs:
                print("Could not retrieve paper metadata.", file=sys.stderr)
                return
            ref_bibcodes = docs[0].get("reference", [])
            if not ref_bibcodes:
                print("No references found in ADS for this paper.", file=sys.stderr)
                return
            print(f"Found {len(ref_bibcodes)} references. Resolving titles...", file=sys.stderr)
        except Exception as e:
            print(f"[refs] Failed to fetch reference list: {e}", file=sys.stderr)
            return

        # Step 2: batch-resolve bibcodes to get titles
        BATCH = 15
        refs_info: List[Dict] = []
        info_map: Dict[str, Dict] = {}

        for i in range(0, len(ref_bibcodes), BATCH):
            batch = ref_bibcodes[i:i+BATCH]
            bibcode_q = " OR ".join(f'bibcode:"{b}"' for b in batch)
            try:
                params2 = urllib.parse.urlencode({
                    "q": bibcode_q,
                    "rows": str(len(batch)),
                    "fl": "bibcode,title,author,year,pub",
                    "sort": "score desc",
                })
                url2 = f"{ADS_API_URL}?{params2}"
                payload2 = http_get(url2, headers={"Authorization": f"Bearer {token}"})
                res = json.loads(payload2.decode("utf-8"))
                for d in res.get("response", {}).get("docs", []):
                    bc = d.get("bibcode", "")
                    title_list = d.get("title", [])
                    t = " ".join(title_list[0].split()) if title_list else ""
                    info_map[bc] = {
                        "bibcode": bc,
                        "title": t,
                        "authors": d.get("author", [])[:3],
                        "year": d.get("year", ""),
                        "pub": d.get("pub", ""),
                        "ads_url": f"https://ui.adsabs.harvard.edu/abs/{bc}/abstract"
                    }
            except Exception as e:
                print(f"[refs] Batch {i//BATCH+1} failed: {e}", file=sys.stderr)
                for bc in batch:
                    info_map[bc] = {"bibcode": bc, "title": "", "authors": [], "ads_url": f"https://ui.adsabs.harvard.edu/abs/{bc}/abstract"}

        # Print
        print(f"\n📚 References ({len(ref_bibcodes)} total):\n")
        for i, bc in enumerate(ref_bibcodes, 1):
            info = info_map.get(bc, {})
            t = info.get("title", "")
            authors = info.get("authors", [])
            year = info.get("year", "")
            pub = info.get("pub", "")
            ads_url = info.get("ads_url", f"https://ui.adsabs.harvard.edu/abs/{bc}/abstract")
            if args.json:
                refs_info.append({**info, "_idx": i})
                continue
            author_str = ", ".join(authors) if authors else "(author unknown)"
            print(f"  [{i:3d}] {bc}")
            if t:
                print(f"        \"{t}\"")
            print(f"        {author_str}" + (f" ({year})" if year else ""))
            if pub:
                print(f"        {pub}")
            print(f"        {ads_url}")
            print()

        if args.json:
            import json as jsonmod
            print(jsonmod.dumps({"bibcode": bibcode, "references": refs_info}, indent=2))

        # ── Classify references via ref_classifier.py ────────────────────────
        if classify_refs and pdf_path_for_refs:
            print(f"\n{'='*60}", file=sys.stderr)
            print("Running reference classification...", file=sys.stderr)
            import subprocess as sp

            rc_cmd = [
                sys.executable,
                "-m",
                "research_library.ref_classifier",
                pdf_path_for_refs,
                "--bibcode",
                bibcode,
                "--token",
                token or "",
            ]
            env = {**os.environ, "ADS_API_TOKEN": token or ""}
            try:
                rc_proc = sp.run(
                    rc_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    env=env,
                )
                if rc_proc.stdout:
                    try:
                        import json as _j

                        rc_json = _j.loads(rc_proc.stdout)
                        # Parse and display classification
                        summary = rc_json.get("summary", {})
                        refs_out = rc_json.get("references", [])
                        cats = defaultdict(list)
                        for r in refs_out:
                            cats[r.get("category", "UNCITED")].append(r.get("bibcode", ""))
                        print(f"\n{'='*60}", file=sys.stderr)
                        print("CLASSIFICATION SUMMARY", file=sys.stderr)
                        print(f"Total references: {len(refs_out)}", file=sys.stderr)
                        for cat_name in ["METHOD", "RESULT", "BACKGROUND+METHOD", "BACKGROUND+RESULT", "METHOD+RESULT", "BACKGROUND", "UNCITED"]:
                            if cats.get(cat_name):
                                print(f"  {cat_name:25s} {len(cats[cat_name])} refs", file=sys.stderr)
                        print(f"\n📂 Classification by category:", file=sys.stderr)
                        for cat_name in ["METHOD", "RESULT", "BACKGROUND+METHOD", "BACKGROUND+RESULT", "METHOD+RESULT", "BACKGROUND", "UNCITED"]:
                            cat_refs = cats.get(cat_name, [])
                            if not cat_refs:
                                continue
                            print(f"\n【{cat_name}】— {len(cat_refs)} refs")
                            for bc in cat_refs[:8]:
                                info = next((r for r in refs_out if r.get("bibcode") == bc), {})
                                t = info.get("title", "")[:70]
                                authors = info.get("authors", [])[:2]
                                year = info.get("year", "")
                                ads_url = info.get("ads_url", f"https://ui.adsabs.harvard.edu/abs/{bc}/abstract")
                                author_str = ", ".join(authors) if authors else ""
                                print(f"  • {bc} ({author_str} {year})")
                                if t:
                                    print(f"    \"{t}\"")
                                print(f"    {ads_url}")
                            if len(cat_refs) > 8:
                                print(f"  ... and {len(cat_refs)-8} more")
                    except Exception as e:
                        print(f"[ref_classifier] JSON parse error: {e}", file=sys.stderr)
                        print(rc_proc.stdout[:1000], file=sys.stderr)
                if rc_proc.stderr:
                    for line in rc_proc.stderr.splitlines():
                        if "json" not in line.lower() and "traceback" not in line.lower():
                            print(f"[ref_classifier] {line}", file=sys.stderr)
            except sp.TimeoutExpired:
                print("[ref_classifier] timed out after 300s", file=sys.stderr)
            except Exception as e:
                print(f"[ref_classifier] failed: {e}", file=sys.stderr)

    # --- Helper to print result ---
    def print_result(doc: Dict, note: str = "") -> Optional[str]:
        """Print ADS result. Returns bibcode if found."""
        bibcode = doc.get("bibcode", "")
        t = " ".join(doc.get("title", [""])[0].split()) if doc.get("title") else ""
        authors = doc.get("author", [])[:3]
        year = doc.get("year", "")
        journal = doc.get("pub", "")
        ads_url = f"https://ui.adsabs.harvard.edu/abs/{bibcode}/abstract"
        if note:
            print(f"\n{note}")
        print(f"Title: {t}")
        print(f"Authors: {', '.join(authors)}")
        print(f"Year: {year} | Journal: {journal}")
        print(f"Bibcode: {bibcode}")
        print(f"ADS: {ads_url}")
        if args.json:
            import json as jsonmod
            print(jsonmod.dumps({
                "title": t, "authors": authors, "year": year,
                "journal": journal, "bibcode": bibcode,
                "doi": doi, "arxiv_id": arxiv_id, "ads_url": ads_url
            }, indent=2))
        else:
            print(f"\n✅ ADS: {ads_url}")
        return bibcode

    if ads_enabled:
        res = resolve_extracted_to_ads_match(ext, title_rows=3, require_strong_id=False)
        if res["ok"] and res.get("doc"):
            doc = res["doc"]
            mm = res.get("match_method")
            bibcode_out: Optional[str] = None
            if mm == "title":
                matched_title = " ".join(doc.get("title", [""])[0].split()) if doc.get("title") else ""
                authors = doc.get("author", [])[:3]
                bibcode_out = doc.get("bibcode", "") or None
                ads_url = f"https://ui.adsabs.harvard.edu/abs/{bibcode_out}/abstract"
                print(f"\n⚠️  DOI/arXiv not in PDF — matched by title:")
                print(f"  PDF title:  {title}")
                print(f"  ADS title: {matched_title}")
                print(f"  Authors: {', '.join(authors)}")
                print(f"  Bibcode: {bibcode_out}")
                print(f"  ADS: {ads_url}")
                cand = res.get("candidates") or []
                if len(cand) > 1:
                    print(f"\n  Other candidates ({len(cand)-1}):")
                    for c in cand[1:]:
                        b2 = c.get("bibcode", "")
                        t2 = c.get("title", "")
                        print(f"    [{b2}] {t2}")
                        print(f"    https://ui.adsabs.harvard.edu/abs/{b2}/abstract")
                if args.json:
                    import json as jsonmod

                    print(
                        jsonmod.dumps(
                            {
                                "title": title,
                                "matched_title": matched_title,
                                "authors": authors,
                                "bibcode": bibcode_out,
                                "ads_url": ads_url,
                                "ads_results": [
                                    {
                                        "bibcode": c.get("bibcode", ""),
                                        "title": c.get("title", ""),
                                        "ads_url": c.get("ads_url", ""),
                                    }
                                    for c in cand
                                ],
                            },
                            indent=2,
                        )
                    )
                else:
                    print(f"\n✅ ADS: {ads_url}")
            else:
                bibcode_out = print_result(doc) or doc.get("bibcode")

            if bibcode_out and args.refs:
                fetch_references(
                    bibcode_out,
                    pdf_path,
                    classify_refs=True,
                    pdf_text=clean_text or "",
                )
            if getattr(args, "ingest", False):
                from research_library.library import db as library_db

                ing_symlink = bool(getattr(args, "ingest_symlink", False))
                ing = ingest_pdf_file(
                    library_db.connect(),
                    pdf_path,
                    dry_run=bool(getattr(args, "ingest_dry_run", False)),
                    require_strong_id=False,
                    copy_to_pdfs=(not bool(getattr(args, "no_ingest_copy", False)))
                    and not ing_symlink,
                    symlink_to_pdfs=ing_symlink,
                    preresolved=res,
                    extracted_override=ext,
                    source="lookup_pdf2ads_ingest",
                )
                if ing.get("ok") and not ing.get("dry_run"):
                    rs = ing.get("references_sync") or {}
                    if rs.get("ok"):
                        rs_h = f" refs_edges={rs.get('edges_written', 0)}"
                    elif rs.get("skipped"):
                        rs_h = f" refs_skipped={rs.get('reason', '')}"
                    elif rs.get("error"):
                        rs_h = f" refs_err={(str(rs.get('error')) or '')[:120]}"
                    else:
                        rs_h = ""
                    print(
                        f"[pdf2ads] library ingest: paper_id={ing.get('paper_id')} "
                        f"pdf_relpath={ing.get('pdf_relpath')}{rs_h}",
                        file=sys.stderr,
                    )
                elif not ing.get("ok"):
                    print(f"[pdf2ads] library ingest failed: {ing.get('error')}", file=sys.stderr)
            return 0

    # --- No ADS or no match: give web search links ---
    if not ads_enabled:
        print("⚠️  ADS_API_TOKEN not set — showing web search link only.", file=sys.stderr)
    if doi:
        ads_url = f"https://ui.adsabs.harvard.edu/abs/search?q=doi%3A%22{urllib.parse.quote(doi)}%22&sort=score"
        print(f"\nDOI: {doi}")
        print(f"ADS search: {ads_url}")
    if arxiv_id:
        ads_url = f"https://ui.adsabs.harvard.edu/abs/search?q=arxiv%3A%22{arxiv_id}%22&sort=score"
        print(f"arXiv: {arxiv_id}")
        print(f"ADS search: {ads_url}")
    if not doi and not arxiv_id and not title:
        print("Error: no DOI, arXiv ID, or title found in PDF.", file=sys.stderr)
        return 1
    return 0


def build_lookup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search papers by title, query, reference, or PDF — with ADS first and arXiv fallback."
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_title = sub.add_parser("title", help="Search by exact or near-exact title")
    p_title.add_argument("--title", required=True)
    p_title.add_argument("--json", action="store_true")
    p_title.set_defaults(func=cmd_search)

    p_query = sub.add_parser("query", help="Search by fuzzy free-text")
    p_query.add_argument("--text", required=True)
    p_query.add_argument("--json", action="store_true")
    p_query.set_defaults(func=cmd_search)

    p_ref = sub.add_parser("ref", help="Search from a bibliography/reference entry")
    p_ref.add_argument("--text", required=True)
    p_ref.add_argument("--json", action="store_true")
    p_ref.set_defaults(func=cmd_search)

    p_bibtex = sub.add_parser("bibtex", help="Get BibTeX for a paper by bibcode or arXiv ID")
    p_bibtex.add_argument("--bibcode", help="ADS bibcode (e.g. 2026arXiv260326195B)")
    p_bibtex.add_argument("--arxiv", help="arXiv ID (e.g. 2603.26195)")
    p_bibtex.add_argument("--json", action="store_true")
    p_bibtex.set_defaults(func=cmd_bibtex)

    p_dl = sub.add_parser("download", help="Download PDF of a paper")
    p_dl.add_argument("--bibcode", help="ADS bibcode (e.g. 2024MNRAS.528..608F)")
    p_dl.add_argument("--arxiv", help="arXiv ID (e.g. 2401.07388)")
    p_dl.add_argument("--dest", default=".", help="Destination directory (default: current directory)")
    p_dl.add_argument("--timeout", type=int, default=60, help="Download timeout in seconds (default: 60)")
    p_dl.add_argument("--journal-only", action="store_true", help="Only try journal PDF, skip arXiv")
    p_dl.add_argument("--arxiv-only", action="store_true", help="Only try arXiv PDF, skip journal")
    p_dl.add_argument("--keep-both", action="store_true", help="Save both journal and arXiv PDFs if both succeed")
    p_dl.add_argument(
        "--library",
        action="store_true",
        help="Save PDF under configured library pdfs/ directory (see RESEARCH_LIBRARY_DATA_DIR)",
    )
    p_dl.set_defaults(func=cmd_download)

    p_pdf = sub.add_parser("pdf2ads", help="Extract metadata from a PDF and find its ADS entry")
    p_pdf.add_argument("pdf", help="Path to the PDF file")
    p_pdf.add_argument("--json", action="store_true", help="Output JSON")
    p_pdf.add_argument("--refs", action="store_true", help="Also fetch the reference list from ADS")
    p_pdf.add_argument("--classify", action="store_true", help="[For internal use] Output JSON for LLM classification instead of printing")
    p_pdf.add_argument(
        "--ingest",
        action="store_true",
        help="After ADS match, upsert into library.db (default: copy PDF to data/pdfs/)",
    )
    p_pdf.add_argument(
        "--ingest-dry-run",
        action="store_true",
        help="With --ingest, resolve and show pdf_relpath but do not write DB",
    )
    p_pdf.add_argument(
        "--no-ingest-copy",
        action="store_true",
        help="With --ingest, do not copy into data/pdfs/ (record original path)",
    )
    p_pdf.add_argument(
        "--ingest-symlink",
        action="store_true",
        help="With --ingest, symlink PDF into data/pdfs/ instead of copying",
    )
    p_pdf.set_defaults(func=cmd_pdf2ads)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    from research_library.config import load_ads_token, load_env

    load_env()
    tok = load_ads_token()
    if tok:
        os.environ.setdefault("ADS_API_TOKEN", tok)
    parser = build_lookup_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
