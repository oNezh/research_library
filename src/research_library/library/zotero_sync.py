"""Zotero Web API sync: pull/push bibliographic items vs local library.db."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from research_library.config import get_data_dir, get_pdfs_dir
from research_library.library import db as library_db
from research_library.library.reference_parse import strip_arxiv_version

_SKIP_ITEM_TYPES = frozenset(
    {
        "attachment",
        "note",
        "annotation",
        "highlight",
        "ink",
        "citation",
    }
)

_ARXIV_RE = re.compile(r"arxiv[:\s]+([\d.]+(?:v\d+)?)", re.I)
_ARXIV_URL_RE = re.compile(r"arxiv\.org/abs/([\d.]+(?:v\d+)?)", re.I)
_BIBCODE_EXTRA_RE = re.compile(r"Bibcode:\s*(\S{19})", re.I)
_YEAR_DATE_RE = re.compile(r"^\d{4}")
_DEFAULT_MAX_ERRORS = 20
_ADS_RETRY_SLEEP_SEC = 3


def _zotero_config() -> Tuple[Optional[str], Optional[str], str]:
    library_id = (os.environ.get("ZOTERO_LIBRARY_ID") or "").strip() or None
    api_key = (os.environ.get("ZOTERO_API_KEY") or "").strip() or None
    library_type = (os.environ.get("ZOTERO_LIBRARY_TYPE") or "user").strip() or "user"
    return library_id, api_key, library_type


def _client():
    library_id, api_key, library_type = _zotero_config()
    if not library_id or not api_key:
        return None, {
            "ok": False,
            "error": "ZOTERO_LIBRARY_ID and ZOTERO_API_KEY must be set in .env",
        }
    try:
        from pyzotero import zotero
    except ImportError as e:
        return None, {
            "ok": False,
            "error": f"pyzotero not installed: {e}. Run: uv sync --extra zotero",
        }
    return zotero.Zotero(library_id, library_type, api_key), None


def _item_type(item: Dict[str, Any]) -> str:
    data = item.get("data") or item
    return str(data.get("itemType") or item.get("itemType") or "").strip()


def _is_bibliographic(item: Dict[str, Any]) -> bool:
    it = _item_type(item)
    return bool(it) and it not in _SKIP_ITEM_TYPES


def _item_data(item: Dict[str, Any]) -> Dict[str, Any]:
    return item.get("data") or item


def _extract_ids(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    doi = library_db._norm_doi_value(data.get("DOI"))
    arxiv_id: Optional[str] = None
    archive = (data.get("archive") or "").strip().lower()
    if archive == "arxiv" and data.get("archiveLocation"):
        arxiv_id = strip_arxiv_version(str(data["archiveLocation"]).strip())
    if not arxiv_id:
        for src in (data.get("url"), data.get("extra"), data.get("libraryCatalog")):
            if not src:
                continue
            s = str(src)
            m = _ARXIV_RE.search(s)
            if not m:
                m = _ARXIV_URL_RE.search(s)
            if m:
                arxiv_id = strip_arxiv_version(m.group(1))
                break
    return doi, arxiv_id


def _extract_bibcode(data: Dict[str, Any]) -> Optional[str]:
    extra = (data.get("extra") or "").strip()
    if not extra:
        return None
    m = _BIBCODE_EXTRA_RE.search(extra)
    return m.group(1).strip() if m else None


def _authors_list_unknown_or_empty(authors: List[str]) -> bool:
    if not authors:
        return True
    return len(authors) == 1 and str(authors[0]).strip() == "Unknown"


def _published_looks_like_year(value: Optional[str]) -> bool:
    if not value:
        return False
    return bool(_YEAR_DATE_RE.match(str(value).strip()))


def _merge_existing_pull_fields(
    conn,
    existing_id: int,
    *,
    title: str,
    abstract: str,
    authors: List[str],
    published: Optional[str],
) -> Tuple[str, str, List[str], Optional[str]]:
    import json

    row = conn.execute(
        "SELECT title, abstract, authors_json, published FROM papers WHERE id = ?",
        (existing_id,),
    ).fetchone()
    if not row:
        return title, abstract, authors, published
    old_title = (row["title"] or "").strip()
    old_abstract = (row["abstract"] or "").strip()
    old_published = (row["published"] or "").strip()
    try:
        old_authors = json.loads(row["authors_json"] or "[]")
    except json.JSONDecodeError:
        old_authors = []
    if not (title or "").strip() and old_title:
        title = old_title
    if not (abstract or "").strip() and old_abstract:
        abstract = old_abstract
    if _authors_list_unknown_or_empty(authors) and not _authors_list_unknown_or_empty(old_authors):
        authors = old_authors
    if not _published_looks_like_year(published) and _published_looks_like_year(old_published):
        published = old_published
    return title, abstract, authors, published


def _zotero_creators_to_authors(creators: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(creators, list):
        return out
    for c in creators:
        if not isinstance(c, dict):
            continue
        first = (c.get("firstName") or "").strip()
        last = (c.get("lastName") or "").strip()
        if first and last:
            out.append(f"{last}, {first}")
        elif last:
            out.append(last)
        elif first:
            out.append(first)
    return out


def _row_authors_raw(row: Dict[str, Any]) -> Any:
    """``get_paper_row`` exposes parsed ``authors``; raw SQL rows use ``authors_json``."""
    if row.get("authors_json") is not None:
        return row.get("authors_json")
    return row.get("authors")


def _authors_json_to_creators(authors_json: Any) -> List[Dict[str, str]]:
    creators: List[Dict[str, str]] = []
    authors: List[str] = []
    if isinstance(authors_json, str):
        try:
            import json

            authors = json.loads(authors_json) if authors_json else []
        except json.JSONDecodeError:
            authors = []
    elif isinstance(authors_json, list):
        authors = authors_json
    for name in authors:
        s = str(name).strip()
        if not s:
            continue
        if "," in s:
            last, first = s.split(",", 1)
            creators.append(
                {
                    "creatorType": "author",
                    "lastName": last.strip(),
                    "firstName": first.strip(),
                }
            )
        else:
            creators.append(
                {"creatorType": "author", "lastName": s, "firstName": ""}
            )
    if not creators:
        creators.append({"creatorType": "author", "lastName": "Unknown", "firstName": ""})
    return creators


def _ads_scalar(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    s = str(value).strip()
    return s or None


def _ads_doc_fields(doc: Dict[str, Any]) -> Dict[str, Any]:
    from research_library.lookup import choose_identifier

    title_l = doc.get("title")
    title = title_l[0] if isinstance(title_l, list) and title_l else (title_l or "") or ""
    abs_raw = doc.get("abstract")
    if isinstance(abs_raw, list):
        abstract = abs_raw[0] if abs_raw else ""
    else:
        abstract = (abs_raw or "") or ""
    _, arxiv_id = choose_identifier(doc.get("identifier") or [])
    year = doc.get("year")
    published = f"{year}-01-01" if year else None
    doi = None
    raw_doi = doc.get("doi")
    if isinstance(raw_doi, list) and raw_doi:
        doi = library_db._norm_doi_value(str(raw_doi[0]))
    elif isinstance(raw_doi, str):
        doi = library_db._norm_doi_value(raw_doi)
    return {
        "bibcode": (doc.get("bibcode") or "").strip() or None,
        "title": title,
        "abstract": abstract,
        "authors": list(doc.get("author") or []),
        "arxiv_id": arxiv_id,
        "published": published,
        "doi": doi,
    }


def _ads_query_doc_retry(query: str) -> Optional[Dict[str, Any]]:
    import time

    from research_library.lookup import ads_query

    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            result = ads_query(query, rows=1)
            docs = result.get("response", {}).get("docs", [])
            return docs[0] if docs else None
        except Exception as e:
            last_exc = e
            if attempt == 0:
                time.sleep(_ADS_RETRY_SLEEP_SEC)
                continue
            raise last_exc
    return None


def _fetch_ads_doc(
    doi: Optional[str],
    arxiv_id: Optional[str],
    *,
    bibcode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not (os.environ.get("ADS_API_TOKEN") or "").strip():
        return None
    try:
        if doi:
            doc = _ads_query_doc_retry(f'doi:"{doi}"')
            if doc:
                return doc
        if arxiv_id:
            ax = strip_arxiv_version(arxiv_id)
            doc = _ads_query_doc_retry(f"arxiv:{ax}")
            if doc:
                return doc
        if bibcode:
            doc = _ads_query_doc_retry(f'bibcode:"{bibcode.strip()}"')
            if doc:
                return doc
    except Exception:
        return None
    return None


def _find_local_paper_id(
    conn,
    *,
    zotero_key: str,
    doi: Optional[str],
    arxiv_id: Optional[str],
    bibcode: Optional[str],
) -> Optional[int]:
    pid = library_db.get_paper_id_by_zotero_key(conn, zotero_key)
    if pid is not None:
        return pid
    if doi:
        pid = library_db.get_paper_id_by_doi(conn, doi)
        if pid is not None:
            return pid
    if arxiv_id:
        rows = library_db.fetch_paper_dicts_by_arxiv_id(conn, arxiv_id)
        if rows:
            return int(rows[0]["id"])
    if bibcode:
        row = conn.execute(
            "SELECT id FROM papers WHERE bibcode = ?",
            (bibcode.strip(),),
        ).fetchone()
        if row:
            return int(row[0])
    return None


def _upsert_from_zotero_item(
    conn,
    data: Dict[str, Any],
    *,
    doi: Optional[str],
    arxiv_id: Optional[str],
    ads_doc: Optional[Dict[str, Any]],
    existing_id: Optional[int],
) -> int:
    import json

    now = library_db._now_iso()
    if ads_doc:
        fields = _ads_doc_fields(ads_doc)
        title = fields.get("title") or (data.get("title") or "")
        abstract = fields.get("abstract") or (data.get("abstractNote") or "")
        authors = fields.get("authors") or _zotero_creators_to_authors(data.get("creators"))
        published = fields.get("published") or (data.get("date") or None)
        bibcode = fields.get("bibcode")
        ax = fields.get("arxiv_id") or arxiv_id
        d = fields.get("doi") or doi
        if existing_id is not None:
            title, abstract, authors, published = _merge_existing_pull_fields(
                conn,
                existing_id,
                title=title,
                abstract=abstract,
                authors=authors,
                published=published,
            )
            conn.execute(
                """
                UPDATE papers SET
                    arxiv_id = COALESCE(?, arxiv_id),
                    bibcode = COALESCE(?, bibcode),
                    doi = COALESCE(?, doi),
                    title = ?,
                    abstract = ?,
                    authors_json = ?,
                    published = COALESCE(?, published),
                    source = 'zotero_pull',
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    ax,
                    bibcode,
                    d,
                    title,
                    abstract,
                    json.dumps(authors, ensure_ascii=False),
                    published,
                    now,
                    existing_id,
                ),
            )
            conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (existing_id,))
            conn.execute(
                "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?, ?, ?)",
                (existing_id, title, abstract),
            )
            return existing_id
        return library_db.upsert_paper(
            conn,
            arxiv_id=ax,
            bibcode=bibcode,
            doi=d,
            title=title,
            abstract=abstract,
            authors=authors,
            published=published,
            source="zotero_pull",
            commit=False,
        )

    title = (data.get("title") or "").strip() or "Untitled"
    abstract = (data.get("abstractNote") or "").strip()
    authors = _zotero_creators_to_authors(data.get("creators"))
    published = (data.get("date") or "").strip() or None

    if existing_id is not None:
        title, abstract, authors, published = _merge_existing_pull_fields(
            conn,
            existing_id,
            title=title,
            abstract=abstract,
            authors=authors,
            published=published,
        )
        conn.execute(
            """
            UPDATE papers SET
                title = ?,
                abstract = ?,
                authors_json = ?,
                doi = COALESCE(?, doi),
                arxiv_id = COALESCE(?, arxiv_id),
                published = COALESCE(?, published),
                source = 'zotero_pull',
                updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                abstract,
                __import__("json").dumps(authors, ensure_ascii=False),
                doi,
                arxiv_id,
                published,
                library_db._now_iso(),
                existing_id,
            ),
        )
        conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (existing_id,))
        conn.execute(
            "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?, ?, ?)",
            (existing_id, title, abstract),
        )
        return existing_id

    if doi or arxiv_id:
        return library_db.upsert_paper(
            conn,
            arxiv_id=arxiv_id,
            bibcode=None,
            doi=doi,
            title=title,
            abstract=abstract,
            authors=authors,
            published=published,
            source="zotero_pull",
            commit=False,
        )

    return library_db.insert_paper_minimal(
        conn,
        title=title,
        abstract=abstract,
        authors=authors,
        published=published,
        doi=doi,
        arxiv_id=arxiv_id,
        source="zotero_pull",
        commit=False,
    )


def _download_item_pdf(zot, item_key: str, dest: Path) -> bool:
    try:
        children = zot.children(item_key)
    except Exception:
        return False
    for child in children or []:
        cdata = _item_data(child)
        if _item_type(child) != "attachment":
            continue
        ctype = (cdata.get("contentType") or "").lower()
        fname = (cdata.get("filename") or "").lower()
        if "pdf" not in ctype and not fname.endswith(".pdf"):
            continue
        ckey = child.get("key")
        if not ckey:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            zot.dump(ckey, dest=str(dest))
            return dest.is_file()
        except Exception:
            continue
    return False


def _pdf_dest_path(paper_id: int, bibcode: Optional[str], zotero_key: str) -> Path:
    pdfs = get_pdfs_dir()
    if bibcode:
        safe = re.sub(r"[^\w.\-]+", "_", bibcode.strip())
        return pdfs / f"{safe}.pdf"
    return pdfs / f"zotero_{zotero_key}.pdf"


def _iter_zotero_items(zot, *, full: bool, since: int) -> Iterable[Dict[str, Any]]:
    start = 0
    limit = 100
    while True:
        kwargs: Dict[str, Any] = {"limit": limit, "start": start}
        if not full and since > 0:
            kwargs["since"] = since
        batch = zot.items(**kwargs)
        if not batch:
            break
        for item in batch:
            yield item
        if len(batch) < limit:
            break
        start += len(batch)


def _count_zotero_bibliographic(zot) -> int:
    n = 0
    for item in _iter_zotero_items(zot, full=True, since=0):
        if _is_bibliographic(item):
            n += 1
    return n


def link(conn, *, max_errors: int = _DEFAULT_MAX_ERRORS) -> Dict[str, Any]:
    zot, err = _client()
    if err:
        return err
    assert zot is not None
    library_id, _, _ = _zotero_config()
    linked = 0
    errors: List[Dict[str, Any]] = []
    stopped = False

    for item in _iter_zotero_items(zot, full=True, since=0):
        if not _is_bibliographic(item):
            continue
        key = item.get("key")
        if not key:
            continue
        if library_db.get_paper_id_by_zotero_key(conn, key) is not None:
            continue
        data = _item_data(item)
        doi, arxiv_id = _extract_ids(data)
        extra_bc = _extract_bibcode(data)
        ads_doc = _fetch_ads_doc(doi, arxiv_id, bibcode=extra_bc)
        bibcode = None
        if ads_doc:
            bibcode = (ads_doc.get("bibcode") or "").strip() or None
        if not bibcode:
            bibcode = extra_bc
        pid = _find_local_paper_id(
            conn,
            zotero_key=key,
            doi=doi,
            arxiv_id=arxiv_id,
            bibcode=bibcode,
        )
        if pid is None:
            continue
        try:
            library_db.upsert_zotero_link(
                conn,
                paper_id=pid,
                zotero_key=key,
                zotero_version=int(item.get("version") or 0),
                doi=doi,
                arxiv_id=arxiv_id,
            )
            linked += 1
        except Exception as e:
            errors.append({"zotero_key": key, "error": str(e)})
            if len(errors) >= max_errors:
                stopped = True
                break

    conn.commit()
    return {
        "ok": not stopped,
        "linked": linked,
        "errors": errors,
        "stopped": stopped,
        "library_id": library_id,
    }


def pull(
    conn,
    *,
    full: bool = False,
    download_pdf: bool = True,
    do_index: bool = True,
    max_errors: int = _DEFAULT_MAX_ERRORS,
) -> Dict[str, Any]:
    zot, err = _client()
    if err:
        return err
    assert zot is not None
    library_id, _, _ = _zotero_config()
    since = 0 if full else library_db.get_zotero_sync_version(conn, library_id or "")
    imported = 0
    updated = 0
    skipped = 0
    pdf_downloaded = 0
    errors: List[Dict[str, Any]] = []
    stopped = False
    paper_ids: List[int] = []

    for item in _iter_zotero_items(zot, full=full, since=since):
        if not _is_bibliographic(item):
            skipped += 1
            continue
        key = item.get("key")
        if not key:
            skipped += 1
            continue
        data = _item_data(item)
        version = int(item.get("version") or 0)
        doi, arxiv_id = _extract_ids(data)
        extra_bc = _extract_bibcode(data)
        ads_doc = _fetch_ads_doc(doi, arxiv_id, bibcode=extra_bc)
        bibcode = None
        if ads_doc:
            bibcode = (ads_doc.get("bibcode") or "").strip() or None
        if not bibcode:
            bibcode = extra_bc

        existing_id = _find_local_paper_id(
            conn,
            zotero_key=key,
            doi=doi,
            arxiv_id=arxiv_id,
            bibcode=bibcode,
        )
        was_existing = existing_id is not None

        try:
            pid = _upsert_from_zotero_item(
                conn,
                data,
                doi=doi,
                arxiv_id=arxiv_id,
                ads_doc=ads_doc,
                existing_id=existing_id,
            )
            library_db.upsert_zotero_link(
                conn,
                paper_id=pid,
                zotero_key=key,
                zotero_version=version,
                doi=doi,
                arxiv_id=arxiv_id,
            )

            if download_pdf:
                row = library_db.get_paper_row(conn, pid)
                bc = (row or {}).get("bibcode") if row else bibcode
                dest = _pdf_dest_path(pid, bc, key)
                if _download_item_pdf(zot, key, dest):
                    from research_library.library.reference_ingest import library_pdf_relpath

                    rel = library_pdf_relpath(str(dest))
                    conn.execute(
                        "UPDATE papers SET pdf_relpath = ?, updated_at = ? WHERE id = ?",
                        (rel, library_db._now_iso(), pid),
                    )
                    pdf_downloaded += 1

            if was_existing:
                updated += 1
            else:
                imported += 1
            paper_ids.append(pid)
        except Exception as e:
            errors.append({"zotero_key": key, "error": str(e)})
            if len(errors) >= max_errors:
                stopped = True
                break

    new_version = zot.last_modified_version()
    library_db.set_zotero_sync_version(conn, library_id or "", new_version)
    conn.commit()

    indexed = 0
    index_errors = 0
    if do_index and paper_ids and not stopped:
        try:
            from research_library.library.semantic import index_papers

            idx = index_papers(conn, paper_ids, force=False)
            indexed = int(idx.get("indexed") or 0)
            index_errors = int(idx.get("errors") or 0)
        except Exception as e:
            errors.append({"phase": "index", "error": str(e)})

    return {
        "ok": not stopped,
        "full": full,
        "since": since,
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "pdf_downloaded": pdf_downloaded,
        "indexed": indexed,
        "index_errors": index_errors,
        "last_pull_version": new_version,
        "errors": errors,
        "stopped": stopped,
        "paper_ids": paper_ids,
    }


def _ads_authors_to_creators(authors: List[str]) -> List[Dict[str, str]]:
    creators: List[Dict[str, str]] = []
    for name in authors:
        s = str(name).strip()
        if not s:
            continue
        if "," in s:
            last, first = s.split(",", 1)
            creators.append(
                {
                    "creatorType": "author",
                    "lastName": last.strip(),
                    "firstName": first.strip(),
                }
            )
        else:
            parts = s.split()
            if len(parts) >= 2:
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": " ".join(parts[:-1]),
                        "lastName": parts[-1],
                    }
                )
            else:
                creators.append(
                    {"creatorType": "author", "lastName": s, "firstName": ""}
                )
    return creators or [{"creatorType": "author", "lastName": "Unknown", "firstName": ""}]


def _pick_richer(local: Optional[str], ads_val: Optional[str]) -> str:
    loc = (local or "").strip()
    ads = (ads_val or "").strip()
    return loc if loc else ads


def _fetch_ads_for_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    bc = (row.get("bibcode") or "").strip()
    if not bc:
        return None
    return _fetch_ads_doc(None, None, bibcode=bc)


def _apply_row_and_ads_to_zotero_data(
    tmpl: Dict[str, Any],
    row: Dict[str, Any],
    ads_doc: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    out = dict(tmpl)
    ads_fields = _ads_doc_fields(ads_doc) if ads_doc else {}

    out["title"] = _pick_richer(row.get("title"), ads_fields.get("title")) or "Untitled"
    out["abstractNote"] = _pick_richer(row.get("abstract"), ads_fields.get("abstract"))

    local_authors = _authors_json_to_creators(_row_authors_raw(row))
    ads_authors = _ads_authors_to_creators(ads_fields.get("authors") or [])
    if local_authors and local_authors[0].get("lastName") != "Unknown":
        out["creators"] = local_authors
    elif ads_authors:
        out["creators"] = ads_authors
    else:
        out["creators"] = local_authors

    doi = _pick_richer(row.get("doi"), ads_fields.get("doi"))
    if doi:
        out["DOI"] = doi

    ax = strip_arxiv_version(str(row.get("arxiv_id") or ads_fields.get("arxiv_id") or ""))
    if ax:
        out["url"] = f"https://arxiv.org/abs/{ax}"
        out["archive"] = "arXiv"
        out["archiveLocation"] = ax

    pub = (ads_doc or {}).get("pub")
    if isinstance(pub, str) and pub.strip():
        out["journalAbbreviation"] = pub.strip()
        out["publicationTitle"] = pub.strip()

    vol = _ads_scalar((ads_doc or {}).get("volume"))
    if vol:
        out["volume"] = vol

    issue = _ads_scalar((ads_doc or {}).get("issue"))
    if issue:
        out["issue"] = issue

    page = _ads_scalar((ads_doc or {}).get("page"))
    if page:
        out["pages"] = page

    published = _pick_richer(row.get("published"), ads_fields.get("published"))
    if published:
        out["date"] = str(published)[:10]
    elif ads_fields.get("published"):
        out["date"] = str(ads_fields["published"])[:10]

    bc = (row.get("bibcode") or ads_fields.get("bibcode") or "").strip()
    extra_parts: List[str] = []
    if bc:
        extra_parts.append(f"Bibcode: {bc}")
    if row.get("source"):
        extra_parts.append(f"research_library source: {row['source']}")
    if extra_parts:
        out["extra"] = "; ".join(extra_parts)

    return out


def _paper_row_to_template(zot, row: Dict[str, Any]) -> Dict[str, Any]:
    tmpl = zot.item_template("journalArticle")
    ads_doc = _fetch_ads_for_row(row)
    return _apply_row_and_ads_to_zotero_data(tmpl, row, ads_doc)


def _zotero_item_for_update(
    zot, zkey: str, row: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Build update payload. Returns (payload, trashed). Skip silently when trashed."""
    try:
        current = zot.item(zkey)
    except Exception:
        return None, False
    if not isinstance(current, dict):
        return None, False
    base_data = dict(current.get("data") or {})
    if base_data.get("deleted"):
        return None, True
    ads_doc = _fetch_ads_for_row(row)
    merged = _apply_row_and_ads_to_zotero_data(base_data, row, ads_doc)
    merged.pop("deleted", None)
    merged["key"] = zkey
    merged["version"] = current.get("version")
    merged["itemType"] = base_data.get("itemType") or "journalArticle"
    out = dict(current)
    out["data"] = merged
    return out, False


def _flush_zotero_updates(
    zot,
    batch: List[Dict[str, Any]],
    meta: List[Tuple[int, str]],
    errors: List[Dict[str, Any]],
) -> int:
    """Flush a batch of Zotero updates; per-item retry on batch failure."""
    if not batch:
        return 0
    try:
        zot.update_items(batch)
        return len(batch)
    except Exception:
        updated = 0
        for payload, (pid, zkey) in zip(batch, meta):
            try:
                zot.update_items([payload])
                updated += 1
            except Exception as e:
                errors.append({"paper_id": pid, "zotero_key": zkey, "error": str(e)})
        return updated


def push(
    conn,
    *,
    with_pdf: bool = True,
    with_notes: bool = False,
    refresh_linked: bool = False,
    max_errors: int = _DEFAULT_MAX_ERRORS,
) -> Dict[str, Any]:
    zot, err = _client()
    if err:
        return err
    assert zot is not None
    created = 0
    updated = 0
    pdf_uploaded = 0
    notes_added = 0
    errors: List[Dict[str, Any]] = []
    stopped = False
    err_limit = 10_000 if refresh_linked else max_errors

    if refresh_linked:
        batch: List[Dict[str, Any]] = []
        batch_meta: List[Tuple[int, str]] = []
        for link in library_db.list_zotero_links(conn):
            pid = int(link["paper_id"])
            zkey = str(link["zotero_key"])
            row = library_db.get_paper_row(conn, pid)
            if not row:
                continue
            try:
                payload, trashed = _zotero_item_for_update(zot, zkey, row)
                if trashed:
                    continue
                if not payload:
                    errors.append(
                        {"paper_id": pid, "zotero_key": zkey, "error": "fetch item failed"}
                    )
                    if len(errors) >= err_limit:
                        stopped = True
                        break
                    continue
                batch.append(payload)
                batch_meta.append((pid, zkey))
                if len(batch) >= 50:
                    updated += _flush_zotero_updates(zot, batch, batch_meta, errors)
                    batch = []
                    batch_meta = []
                    if len(errors) >= err_limit:
                        stopped = True
                        break
            except Exception as e:
                errors.append({"paper_id": pid, "zotero_key": zkey, "error": str(e)})
                if len(errors) >= err_limit:
                    stopped = True
                    break
        if batch and not stopped:
            updated += _flush_zotero_updates(zot, batch, batch_meta, errors)
            if len(errors) >= err_limit:
                stopped = True
        conn.commit()
        if refresh_linked and not library_db.list_unlinked_paper_ids(conn):
            return {
                "ok": not stopped,
                "created": created,
                "updated": updated,
                "pdf_uploaded": pdf_uploaded,
                "notes_added": notes_added,
                "errors": errors,
                "stopped": stopped,
            }

    for pid in library_db.list_unlinked_paper_ids(conn):
        row = library_db.get_paper_row(conn, pid)
        if not row:
            continue
        title = (row.get("title") or "").strip()
        if not title:
            continue
        try:
            tmpl = _paper_row_to_template(zot, row)
            checked = zot.check_items([tmpl])
            resp = zot.create_items(checked)
            successful = (resp or {}).get("successful") or {}
            if not successful:
                failed = (resp or {}).get("failed") or {}
                errors.append({"paper_id": pid, "error": str(failed)})
                if len(errors) >= max_errors:
                    stopped = True
                    break
                continue
            first = successful.get("0") or next(iter(successful.values()))
            zkey = first.get("key")
            if not zkey:
                errors.append({"paper_id": pid, "error": "create_items missing key"})
                continue

            library_db.upsert_zotero_link(
                conn,
                paper_id=pid,
                zotero_key=zkey,
                zotero_version=int(first.get("version") or 0),
                doi=row.get("doi"),
                arxiv_id=row.get("arxiv_id"),
            )
            conn.execute(
                "UPDATE zotero_links SET last_push_at = ? WHERE paper_id = ?",
                (library_db._now_iso(), pid),
            )
            created += 1

            if with_pdf and row.get("pdf_relpath"):
                abs_pdf = get_data_dir() / str(row["pdf_relpath"])
                if abs_pdf.is_file():
                    try:
                        zot.attachment_simple([str(abs_pdf)], parentid=zkey)
                        pdf_uploaded += 1
                    except Exception as e:
                        errors.append({"paper_id": pid, "error": f"pdf_upload: {e}"})

            if with_notes:
                abstract = (row.get("abstract") or "").strip()
                if abstract:
                    note_tmpl = zot.item_template("note")
                    note_tmpl["note"] = abstract[:50000]
                    note_tmpl["parentItem"] = zkey
                    zot.create_items([note_tmpl])
                    notes_added += 1
        except Exception as e:
            errors.append({"paper_id": pid, "error": str(e)})
            if len(errors) >= max_errors:
                stopped = True
                break

    conn.commit()
    return {
        "ok": not stopped,
        "created": created,
        "updated": updated,
        "pdf_uploaded": pdf_uploaded,
        "notes_added": notes_added,
        "errors": errors,
        "stopped": stopped,
    }


def sync_all(
    conn,
    *,
    full: bool = False,
    with_pdf: bool = True,
    do_index: bool = True,
    max_errors: int = _DEFAULT_MAX_ERRORS,
) -> Dict[str, Any]:
    phases: List[Dict[str, Any]] = []
    r_link = link(conn, max_errors=max_errors)
    phases.append({"name": "link", **r_link})
    if r_link.get("stopped"):
        return {"ok": False, "phases": phases, "stopped": True}

    r_pull = pull(
        conn,
        full=full,
        download_pdf=with_pdf,
        do_index=do_index,
        max_errors=max_errors,
    )
    phases.append({"name": "pull", **r_pull})
    if r_pull.get("stopped"):
        return {"ok": False, "phases": phases, "stopped": True}

    r_push = push(conn, with_pdf=with_pdf, max_errors=max_errors)
    phases.append({"name": "push", **r_push})
    ok = all(p.get("ok", True) for p in phases)
    return {"ok": ok, "phases": phases, "stopped": r_push.get("stopped", False)}


def backfill_unknown(
    conn,
    *,
    delay: float = 1.0,
    push_zotero: bool = True,
) -> Dict[str, Any]:
    import json
    import time

    zot = None
    if push_zotero:
        zot, err = _client()
        if err:
            return err

    repaired = 0
    ads_miss = 0
    zotero_updated = 0
    errors: List[Dict[str, Any]] = []
    stopped = False
    consecutive_ads_errors = 0

    rows = conn.execute(
        """
        SELECT p.id, p.bibcode, p.title, p.abstract, p.published, p.doi, z.zotero_key
        FROM papers p
        LEFT JOIN zotero_links z ON z.paper_id = p.id
        WHERE p.authors_json LIKE '%Unknown%'
          AND p.bibcode IS NOT NULL
          AND TRIM(p.bibcode) != ''
        ORDER BY p.id
        """
    ).fetchall()

    for row in rows:
        pid = int(row["id"])
        bc = str(row["bibcode"]).strip()
        try:
            ads_doc = _ads_query_doc_retry(f'bibcode:"{bc}"')
            consecutive_ads_errors = 0
        except Exception as e:
            consecutive_ads_errors += 1
            errors.append({"paper_id": pid, "bibcode": bc, "error": str(e)})
            if consecutive_ads_errors >= 5:
                stopped = True
                break
            time.sleep(delay)
            continue

        if not ads_doc:
            ads_miss += 1
            time.sleep(delay)
            continue

        fields = _ads_doc_fields(ads_doc)
        authors = fields.get("authors") or []
        if not authors:
            ads_miss += 1
            time.sleep(delay)
            continue

        title = _pick_richer(row["title"], fields.get("title")) or (row["title"] or "Untitled")
        abstract = _pick_richer(row["abstract"], fields.get("abstract"))
        published = fields.get("published")
        if not _published_looks_like_year(published):
            published = row["published"] if _published_looks_like_year(row["published"]) else published
        doi = row["doi"] or fields.get("doi")
        now = library_db._now_iso()
        authors_json = json.dumps(authors, ensure_ascii=False)

        conn.execute(
            """
            UPDATE papers SET
                title = ?,
                abstract = COALESCE(NULLIF(?, ''), abstract),
                authors_json = ?,
                published = COALESCE(?, published),
                doi = COALESCE(?, doi),
                updated_at = ?
            WHERE id = ?
            """,
            (title, abstract, authors_json, published, doi, now, pid),
        )
        conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (pid,))
        conn.execute(
            "INSERT INTO papers_fts(paper_id, title, abstract) VALUES (?, ?, ?)",
            (pid, title, abstract or (row["abstract"] or "")),
        )
        repaired += 1

        zkey = row["zotero_key"]
        if push_zotero and zot is not None and zkey:
            try:
                paper_row = library_db.get_paper_row(conn, pid)
                if paper_row:
                    payload, trashed = _zotero_item_for_update(zot, str(zkey), paper_row)
                    if payload and not trashed:
                        zot.update_items([payload])
                        zotero_updated += 1
            except Exception as e:
                errors.append({"paper_id": pid, "zotero_key": zkey, "error": str(e)})

        time.sleep(delay)

    conn.commit()
    return {
        "ok": not stopped,
        "candidates": len(rows),
        "repaired": repaired,
        "ads_miss": ads_miss,
        "zotero_updated": zotero_updated,
        "errors": errors,
        "stopped": stopped,
    }


def status(conn) -> Dict[str, Any]:
    library_id, api_key, library_type = _zotero_config()
    local = library_db.zotero_sync_stats(conn)
    out: Dict[str, Any] = {
        "configured": bool(library_id and api_key),
        "library_id": library_id,
        "library_type": library_type,
        "local": local,
        "last_pull_version": library_db.get_zotero_sync_version(conn, library_id or ""),
    }
    if not (library_id and api_key):
        out["zotero_total"] = None
        return out
    zot, err = _client()
    if err:
        out["zotero_error"] = err.get("error")
        return out
    assert zot is not None
    try:
        out["zotero_reachable"] = True
        out["zotero_total"] = _count_zotero_bibliographic(zot)
    except Exception as e:
        out["zotero_reachable"] = False
        out["zotero_error"] = str(e)
    return out
