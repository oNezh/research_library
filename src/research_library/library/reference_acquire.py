"""Acquire PDF: local library → ADS link_gateway → arXiv direct."""

from __future__ import annotations

import os
import shutil
from typing import Any, Optional, Tuple

from research_library.config import load_env
from . import db as library_db
from research_library.library.reference_parse import StandardRef, enrich_bibcode_for_acquire
from research_library.lookup import ads_query, choose_identifier, download_pdf, fetch_pdf_links


def _acquire_pdf_timeout_seconds(explicit: Optional[int] = None) -> int:
    load_env()
    if explicit is not None:
        return max(15, int(explicit))
    raw = (os.environ.get("RESEARCH_PDF_ACQUIRE_TIMEOUT") or "").strip()
    try:
        return max(30, int(raw)) if raw else 120
    except ValueError:
        return 120


def acquire_pdf(
    ref: StandardRef,
    conn: Any,
    dest_path: str,
    *,
    timeout: Optional[int] = None,
) -> Tuple[Optional[str], str]:
    """
    Order: local library → publisher PDF → ADS scan → arXiv eprint mirror → arXiv direct.

    Publisher-first matches the "library archive" intent: prefer the journal copy
    of record when ADS exposes it; fall back to open-access sources otherwise.
    """
    load_env()
    ddir = os.path.dirname(dest_path) or "."
    os.makedirs(ddir, exist_ok=True)

    to = _acquire_pdf_timeout_seconds(timeout)
    ref = enrich_bibcode_for_acquire(ref)
    arxiv_id = ref.arxiv_id
    doi = ref.doi
    bibcode = ref.bibcode
    token = (os.environ.get("ADS_API_TOKEN") or "").strip()

    if token and (doi or arxiv_id):
        try:
            if doi:
                r = ads_query(f'doi:"{doi}"', rows=1, fl=["bibcode", "identifier"])
            elif arxiv_id:
                r = ads_query(f"arxiv:{arxiv_id}", rows=1, fl=["bibcode", "identifier"])
            else:
                r = None
            if r:
                docs = r.get("response", {}).get("docs", [])
                if docs:
                    bc = docs[0].get("bibcode")
                    if bc:
                        bibcode = str(bc)
                    if not arxiv_id:
                        _, ax = choose_identifier(docs[0].get("identifier") or [])
                        if ax:
                            import re

                            arxiv_id = re.sub(
                                r"v\d+$", "", ax.strip(), flags=re.IGNORECASE
                            )
        except Exception:
            pass

    library_db.ensure_schema(conn)
    local = library_db.find_local_pdf_path(
        conn, arxiv_id=arxiv_id, bibcode=bibcode
    )
    if local:
        try:
            shutil.copy2(local, dest_path)
            return dest_path, "library_cached"
        except OSError:
            pass

    tried_ads_bibcode = False
    if token and bibcode:
        tried_ads_bibcode = True
        try:
            links = fetch_pdf_links(str(bibcode), arxiv_id_hint=arxiv_id)
            order = [
                ("pub", links.get("pub")),
                ("ads", links.get("ads")),
                ("eprint", links.get("eprint")),
                ("arxiv", links.get("arxiv")),
            ]
            for _label, u in order:
                if u and download_pdf(dest_path, u, timeout=to):
                    return dest_path, _label
        except Exception as e:
            if not arxiv_id:
                return None, f"ads_error:{e}"

    if arxiv_id:
        url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
        if download_pdf(dest_path, url, timeout=to):
            return dest_path, "arxiv"
        return None, "arxiv_download_failed"

    if doi and token:
        return None, "ads_no_pdf_and_no_arxiv"
    if tried_ads_bibcode:
        return None, "ads_all_pdf_downloads_failed"
    return None, "no_arxiv_or_doi"


_KNOWN_PUB_VERSION_LABELS: frozenset[str] = frozenset(
    {"pub", "ads", "eprint", "arxiv", "library_cached"}
)


def normalize_pub_version_label(reason: str) -> Optional[str]:
    """Map ``acquire_pdf`` reason → canonical ``pub_version`` value or ``None``."""
    r = (reason or "").strip().lower()
    if not r:
        return None
    if r.startswith("ads_") and len(r) > 4:
        r = r[4:]
    return r if r in _KNOWN_PUB_VERSION_LABELS else None


def standard_ref_from_cli_bibcode_arxiv(
    bibcode: str = "", arxiv_id: str = ""
) -> StandardRef:
    """Build StandardRef for lookup-style download CLI."""
    from .reference_parse import strip_arxiv_version

    b = (bibcode or "").strip()
    a = (arxiv_id or "").strip()
    raw = b or a or ""
    if b:
        kind: Any = "bibcode"
    elif a:
        kind = "arxiv"
        a = strip_arxiv_version(a)
    else:
        kind = "freeform"
    return StandardRef(raw_line=raw, kind=kind, bibcode=b or None, arxiv_id=a or None)
