"""stdio MCP server for OpenClaw.

Reference resolution (library package):
- ``library_bib_export`` → ``bib_export.list_to_bibtex_export`` → ``reference_parse.parse_catalog_line``:
  full ADS resolution for free-form journal lines (_resolve_bibcode_from_ads), same as CLI export.
- ``lookup_ref`` → lookup ``ref`` subcommand: ``parse_reference`` + ``ads_search_reference`` (full string).
- ``pdf_reference_chain`` / ``pdf_analyze(reference_chain=True)`` → multi-hop chain (CLI ``--reference-chain``); refs from ``paper_references`` when the PDF matches a library row and edges exist (else PDF bibliography); ``parse_catalog_line`` + ``acquire_pdf``, or reuse ``find_local_pdf_path`` for in-library cited bibcodes.
- ``reference_acquire`` / ``reference_ingest`` consume ``StandardRef``; they do not change which parser ran upstream.
"""

from __future__ import annotations

import io
import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP


def _run_lookup(argv: list[str]) -> dict[str, Any]:
    from research_library.config import load_ads_token, load_env
    from research_library.lookup import main as lookup_main

    load_env()
    tok = load_ads_token()
    if tok:
        os.environ.setdefault("ADS_API_TOKEN", tok)

    out, err = io.StringIO(), io.StringIO()
    old_o, old_e = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        code = lookup_main(argv)
    finally:
        sys.stdout, sys.stderr = old_o, old_e
    return {"exit_code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}


def _run_refs_classify(argv: list[str]) -> dict[str, Any]:
    from research_library.config import load_env
    from research_library.refs_classify import main as rc_main

    load_env()
    out, err = io.StringIO(), io.StringIO()
    old_o, old_e = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        code = rc_main(argv)
    finally:
        sys.stdout, sys.stderr = old_o, old_e
    return {"exit_code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}


def _run_ads_products(argv: list[str]) -> dict[str, Any]:
    from research_library.ads_data_products import main as ads_main
    from research_library.config import load_env

    load_env()
    out, err = io.StringIO(), io.StringIO()
    old_o, old_e = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        ads_main(argv)
        code = 0
    finally:
        sys.stdout, sys.stderr = old_o, old_e
    return {"exit_code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}


def _run_pdf_extract(argv: list[str]) -> dict[str, Any]:
    from research_library.config import load_env
    from research_library.pdf_extract import main as pdf_main

    load_env()
    out, err = io.StringIO(), io.StringIO()
    old_o, old_e = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        pdf_main(argv)
        code = 0
    finally:
        sys.stdout, sys.stderr = old_o, old_e
    return {"exit_code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}


_RESEARCH_LIBRARY_MCP_INSTRUCTIONS = """\
ADS/arXiv research-library tools. Reference resolution differs by tool:
- lookup_ref: full bibliography-line parse + ADS reference search (lookup CLI ``ref``).
- library_bib_export: each line via parse_catalog_line (full free-form ADS bibcode resolution + optional ingest).
- pdf_reference_chain **or** pdf_analyze(reference_chain=True): multi-hop citation chain — same as CLI ``research-lib pdf-analyze PATH --reference-chain -q '…'``. Needs non-empty ``question``. Uses library ``paper_references`` when the PDF is in ``library.db`` and edges exist (env ``RESEARCH_PDF_CHAIN_LIBRARY_REFS``); else PDF bibliography. Child PDFs: ``parse_catalog_line`` + ``acquire_pdf``, or reuse ``find_local_pdf_path``. Returns JSON with ``markdown_report``, ``trace``, ``library_ingested_ok``, etc.
Semantic chunk tools (set ``semantic_backend``: empty = ``RESEARCH_SEMANTIC_BACKEND`` env; ``fts`` = SQLite FTS5/BM25, no Chroma/embeddings; ``vector`` = Chroma + embedding API, needs ``pip install -e '.[semantic]'``).
- library_semantic_status: chunk counts + active backend.
- library_semantic_index / library_semantic_search / library_get_related_papers / library_compare_topic / library_topic_dossier / **library_semantic_report**: pass ``semantic_backend`` optional override. ``library_semantic_report`` bundles chunks + ``paper_references`` and asks the LLM for markdown with **[S1]** source tags.
- library_ingest_pdf: PDF → ADS match → upsert ``papers`` + ``pdf_relpath`` (default: copy into ``data/pdfs/``). Optional ``manual_doi`` / ``manual_arxiv`` / ``manual_match_title`` / ``manual_bibcode`` for retries when auto extraction fails.
- library_fetch_source: pull arXiv TeX-derived clean text (ar5iv → local tarball backend). Populates ``source_text_relpath``/``source_kind``; ``library_semantic_index`` also auto-fetches when missing (then PDF fallback).
- library_update_pdf: refetch publisher PDF (overwrites file). ``source=auto`` tries pub > ads > eprint > arxiv. Use ``reindex=true`` to force a re-embed when the paper is still indexed from PDF.
Pipeline modules: reference_parse → reference_acquire (PDF) / bib_export; reference_ingest for DB upserts."""


def _run_pdf_reference_chain_mcp(
    pdf_path: str,
    question: str,
    *,
    max_hops: int = 2,
    persist_library: bool = True,
    use_library_references: bool | None = None,
    max_chars_per_pdf: int | None = None,
    max_step_tokens: int | None = None,
    max_synth_tokens: int | None = None,
    provider: str | None = None,
) -> str:
    from research_library.config import load_env
    from research_library.analysis.llm.base import LLMError
    from research_library.analysis.pdf import analyze_pdf_reference_chain

    load_env()
    prov = (provider or "").strip() or None
    try:
        q = (question or "").strip()
        if not q:
            return json.dumps(
                {
                    "error": "question_required",
                    "message": "reference_chain requires a non-empty question",
                },
                ensure_ascii=False,
            )
        out = analyze_pdf_reference_chain(
            pdf_path.strip(),
            q,
            provider=prov,
            max_hops=max(1, min(int(max_hops), 10)),
            max_chars_per_pdf=max_chars_per_pdf,
            max_step_tokens=max_step_tokens,
            max_synth_tokens=max_synth_tokens,
            persist_library=bool(persist_library),
            use_library_references=use_library_references,
        )
        return json.dumps(out, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)
    except FileNotFoundError:
        return json.dumps({"error": "not_found", "path": pdf_path}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": "pdf_text", "message": str(e)}, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "runtime", "message": str(e)}, ensure_ascii=False)


mcp = FastMCP("research-library", instructions=_RESEARCH_LIBRARY_MCP_INSTRUCTIONS)


@mcp.tool()
def lookup_ref(text: str, as_json: bool = True) -> str:
    """Parse a full bibliography/reference line; ADS reference search + arXiv fallback (lookup ``ref``). pdf_analyze reference_chain uses ``parse_catalog_line`` instead."""
    from research_library.services import lookup_ref_search

    out = lookup_ref_search(text)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def lookup_title(title: str, as_json: bool = True) -> str:
    """Search by paper title."""
    from research_library.services import lookup_title_search

    out = lookup_title_search(title)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def lookup_query(text: str, as_json: bool = True) -> str:
    """Fuzzy free-text search (ADS + arXiv)."""
    from research_library.services import lookup_query_search

    out = lookup_query_search(text)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def fetch_bibtex(bibcode: str = "", arxiv: str = "", as_json: bool = True) -> str:
    """Fetch BibTeX from ADS by bibcode or arXiv id."""
    from research_library.services import lookup_bibtex

    out = lookup_bibtex(bibcode=bibcode or None, arxiv_id=arxiv or None)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def download_pdf(
    bibcode: str = "",
    arxiv: str = "",
    dest: str = ".",
    use_library: bool = False,
    arxiv_only: bool = False,
    journal_only: bool = False,
    timeout: int = 60,
) -> str:
    """Download PDF (tries ADS link gateway then arXiv). use_library saves under configured pdfs/ dir."""
    args = ["download", "--dest", dest, "--timeout", str(timeout)]
    if bibcode:
        args += ["--bibcode", bibcode]
    if arxiv:
        args += ["--arxiv", arxiv]
    if use_library:
        args.append("--library")
    if arxiv_only:
        args.append("--arxiv-only")
    if journal_only:
        args.append("--journal-only")
    return json.dumps(_run_lookup(args), ensure_ascii=False)


@mcp.tool()
def pdf_to_ads(pdf_path: str, include_refs: bool = False, as_json: bool = False) -> str:
    """Resolve a local PDF to ADS metadata (DOI / arXiv / title heuristic)."""
    args = ["pdf2ads", pdf_path]
    if include_refs:
        args.append("--refs")
    if as_json:
        args.append("--json")
    return json.dumps(_run_lookup(args), ensure_ascii=False)


@mcp.tool()
def references_classify(
    pdf_path: str,
    bibcode: str,
    doi: str = "",
    json_only: bool = True,
) -> str:
    """Build structured reference list + citation contexts (stdout JSON when json_only)."""
    args = [pdf_path, "--bibcode", bibcode]
    if doi:
        args += ["--doi", doi]
    if json_only:
        args.append("--json-only")
    return json.dumps(_run_refs_classify(args), ensure_ascii=False)


@mcp.tool()
def ads_data_products(
    bibcode: str,
    pdf_path: str = "",
    download_catalogs: bool = False,
    dest: str = "./catalogs",
    as_json: bool = True,
) -> str:
    """ADS data-product counts + optional PDF catalog mention scan."""
    args = [bibcode]
    if pdf_path:
        args += ["--pdf", pdf_path]
    if download_catalogs:
        args.append("--download")
    args += ["--dest", dest]
    if as_json:
        args.append("--json")
    return json.dumps(_run_ads_products(args), ensure_ascii=False)


@mcp.tool()
def pdf_extract_tables_or_images(
    pdf_path: str,
    mode: str = "list",
    pages: str = "",
    output_format: str = "md",
    output_dir: str = "",
) -> str:
    """mode: list | tables | images | all. Needs pymupdf."""
    args = [pdf_path]
    if mode == "list":
        args.append("--list")
    elif mode == "tables":
        args.append("--tables")
    elif mode == "images":
        args.append("--images")
    elif mode == "all":
        args.append("--all")
    else:
        args.append("--list")
    if pages:
        args += ["--pages", pages]
    if output_format and mode in ("tables", "all"):
        args += ["--format", output_format]
    if output_dir:
        args += ["--output-dir", output_dir]
    args.append("--json")
    return json.dumps(_run_pdf_extract(args), ensure_ascii=False)


@mcp.tool()
def arxiv_keyword_scan(
    category: str = "all",
    days_back: int = 365,
    persist_db: bool = True,
    max_results: int = 500,
    max_pages_per_category: int = 1,
) -> str:
    """Scan recent arXiv for configured keywords (stdout + stderr captured). max_pages_per_category=0 paginates until days_back or API end (capped)."""
    from research_library import arxiv_keywords as ak

    out, err = io.StringIO(), io.StringIO()
    old_o, old_e = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = out, err
        ak.run(
            category=category,
            days_back=days_back,
            cache_enabled=True,
            persist_db=persist_db,
            max_results=max_results,
            max_pages_per_category=max_pages_per_category,
        )
        code = 0
    finally:
        sys.stdout, sys.stderr = old_o, old_e
    return json.dumps(
        {"exit_code": code, "stdout": out.getvalue(), "stderr": err.getvalue()},
        ensure_ascii=False,
    )


@mcp.tool()
def library_search(query: str, limit: int = 20) -> str:
    """Full-text search in local library.db (title + abstract, FTS5)."""
    from research_library.library import db
    from research_library.config import load_env

    load_env()
    conn = db.connect()
    rows = db.search_fts(conn, query, limit)
    return json.dumps(rows, ensure_ascii=False)


@mcp.tool()
def library_stats() -> str:
    """Row count, last update, and path to library.db."""
    from research_library.library import db
    from research_library.config import load_env

    load_env()
    conn = db.connect()
    return json.dumps(db.stats(conn), ensure_ascii=False)


@mcp.tool()
def library_import_cache() -> str:
    """Import entries from arxiv_cache.json into library.db (upsert by arxiv id)."""
    from research_library import arxiv_keywords as ak
    from research_library.library import db
    from research_library.config import load_env

    load_env()
    conn = db.connect()
    entries = ak.load_cache()
    n = db.import_cache_json(conn, entries)
    return json.dumps({"imported": n, "cache_entries": len(entries)}, ensure_ascii=False)


@mcp.tool()
def pdf_reference_chain(
    pdf_path: str,
    question: str,
    max_hops: int = 2,
    persist_ref_pdfs_to_library: bool = True,
    use_library_references: bool | None = None,
    llm_provider: str | None = None,
    max_chars_per_pdf: int | None = None,
    max_step_tokens: int | None = None,
    max_synth_tokens: int | None = None,
) -> str:
    """Multi-hop reference chain: same as CLI ``research-lib pdf-analyze PATH --reference-chain -q '…'``. ``question`` is required. JSON includes ``markdown_report``, ``trace``, ``library_ingested_ok``. Tuning: env ``RESEARCH_PDF_CHAIN_*``, ``RESEARCH_LLM_*`` (see README / .env.example)."""
    return _run_pdf_reference_chain_mcp(
        pdf_path,
        question,
        max_hops=max_hops,
        persist_library=persist_ref_pdfs_to_library,
        use_library_references=use_library_references,
        max_chars_per_pdf=max_chars_per_pdf,
        max_step_tokens=max_step_tokens,
        max_synth_tokens=max_synth_tokens,
        provider=llm_provider,
    )


@mcp.tool()
def pdf_analyze(
    pdf_path: str,
    question: str = "",
    reference_chain: bool = False,
    max_hops: int = 2,
    persist_ref_pdfs_to_library: bool = True,
    use_library_references: bool | None = None,
    llm_provider: str | None = None,
    max_chars: int | None = None,
    max_step_tokens: int | None = None,
    max_synth_tokens: int | None = None,
) -> str:
    """Chinese summary + optional question excerpts, or (``reference_chain=True``) multi-hop report. For the chain, use dedicated ``pdf_reference_chain`` if clearer. ``use_library_references``: null = env ``RESEARCH_PDF_CHAIN_LIBRARY_REFS``; ``False`` = PDF bibliography only. ``max_chars``: truncate PDF text (non-chain); or per-PDF cap when chain."""
    from research_library.config import load_env
    from research_library.analysis.llm.base import LLMError
    from research_library.analysis.pdf import analyze_pdf

    load_env()
    prov = (llm_provider or "").strip() or None
    try:
        if reference_chain:
            return _run_pdf_reference_chain_mcp(
                pdf_path,
                question,
                max_hops=max_hops,
                persist_library=persist_ref_pdfs_to_library,
                use_library_references=use_library_references,
                max_chars_per_pdf=max_chars,
                max_step_tokens=max_step_tokens,
                max_synth_tokens=max_synth_tokens,
                provider=prov,
            )
        out = analyze_pdf(
            pdf_path,
            question=question or None,
            provider=prov,
            max_chars=max_chars,
        )
        return json.dumps(out, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)
    except FileNotFoundError:
        return json.dumps({"error": "not_found", "path": pdf_path}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": "pdf_text", "message": str(e)}, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "runtime", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def library_bib_export(references_list: str, ingest_missing: bool = True) -> str:
    """Newline-separated bibcodes, arXiv ids, DOIs, or reference lines. Each line: parse_catalog_line(use_ads=True). Returns ADS BibTeX + optional DB ingest. reference_chain uses the same resolver."""
    from research_library.config import load_env
    from research_library.library.bib_export import list_to_bibtex_export

    load_env()
    out = list_to_bibtex_export(references_list.strip(), ingest_missing=ingest_missing)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def library_ingest_pdf(
    pdf_path: str,
    dry_run: bool = False,
    require_strong_id: bool = False,
    copy_to_pdfs: bool = True,
    symlink_to_pdfs: bool = False,
    title_rows: int = 3,
    manual_doi: str | None = None,
    manual_arxiv: str | None = None,
    manual_match_title: str | None = None,
    manual_bibcode: str | None = None,
    no_sync_references: bool = False,
) -> str:
    """Extract DOI/arXiv/title from PDF, query ADS, upsert papers + pdf_relpath. Optional manual_* fields retry failed ingests (same as CLI --doi/--arxiv/--match-title/--bibcode). By default copies PDF into data/pdfs/."""
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.pdf_ingest import (
        ingest_pdf_file,
        preresolved_from_manual_bibcode,
    )
    from research_library.library.reference_parse import strip_arxiv_version

    load_env()
    conn = library_db.connect()

    extracted_override: dict[str, str] = {}
    if (manual_doi or "").strip():
        d = manual_doi.strip()
        if d.lower().startswith("doi:"):
            d = d[4:].strip()
        if d:
            extracted_override["doi"] = d
    if (manual_arxiv or "").strip():
        ax = strip_arxiv_version(manual_arxiv.strip())
        if ax:
            extracted_override["arxiv_id"] = ax
    if (manual_match_title or "").strip():
        extracted_override["title_candidate"] = manual_match_title.strip()

    preresolved = None
    if (manual_bibcode or "").strip():
        preresolved = preresolved_from_manual_bibcode(manual_bibcode.strip())

    out = ingest_pdf_file(
        conn,
        pdf_path.strip(),
        dry_run=bool(dry_run),
        require_strong_id=True if require_strong_id else None,
        title_rows=max(1, min(int(title_rows), 50)),
        copy_to_pdfs=bool(copy_to_pdfs) and not bool(symlink_to_pdfs),
        symlink_to_pdfs=bool(symlink_to_pdfs),
        source="mcp_library_ingest_pdf",
        sync_references=False if no_sync_references else None,
        preresolved=preresolved,
        extracted_override=extracted_override if extracted_override else None,
    )
    conn.commit()
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def library_citation_sync(
    resolve_arxiv_bibcodes: bool = False,
    sleep_s: float = 0.0,
    missing_only: bool = False,
) -> str:
    """Populate paper_references from ADS (needs bibcode per paper; optional arXiv→bibcode via ADS)."""
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.citations import sync_references_from_ads

    load_env()
    conn = library_db.connect()
    out = sync_references_from_ads(
        conn,
        resolve_arxiv_bibcodes=resolve_arxiv_bibcodes,
        sleep_s=sleep_s,
        missing_only=missing_only,
    )
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def library_citation_graph(
    min_hub_citations: int = 2, mermaid_max_nodes: int = 48
) -> str:
    """Citation graph: nodes, edges, missing_hubs (bibcodes cited by ≥N papers but not in DB), Mermaid mindmap."""
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.citations import build_citation_graph

    load_env()
    conn = library_db.connect()
    g = build_citation_graph(
        conn,
        min_hub_citing_papers=min_hub_citations,
        mermaid_max_nodes=mermaid_max_nodes,
    )
    return json.dumps(g, ensure_ascii=False)


@mcp.tool()
def library_semantic_status() -> str:
    """PDF/chunk index status: semantic_backend (fts vs vector), counts, db path."""
    from research_library.config import effective_semantic_backend, get_data_dir, load_env
    from research_library.library import db as library_db

    load_env()
    conn = library_db.connect()
    library_db.ensure_schema(conn)
    n_pdf = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM papers
            WHERE pdf_relpath IS NOT NULL AND TRIM(pdf_relpath) != ''
            """
        ).fetchone()[0]
    )
    n_chunks = int(conn.execute("SELECT COUNT(*) FROM paper_chunks").fetchone()[0])
    n_fts = int(conn.execute("SELECT COUNT(*) FROM paper_chunks_fts").fetchone()[0])
    be = effective_semantic_backend(None)
    raw = (os.environ.get("RESEARCH_SEMANTIC_BACKEND") or "").strip()
    return json.dumps(
        {
            "semantic_backend": be,
            "RESEARCH_SEMANTIC_BACKEND": raw or None,
            "papers_with_pdf": n_pdf,
            "paper_chunks_rows": n_chunks,
            "paper_chunks_fts_rows": n_fts,
            "library_db_path": str(library_db.db_path()),
            "data_dir": str(get_data_dir()),
        },
        ensure_ascii=False,
    )


@mcp.tool()
def library_semantic_search(
    query: str, limit: int = 10, semantic_backend: str = ""
) -> str:
    """Search indexed PDF chunks (BM25 if semantic_backend=fts, else embedding similarity)."""
    from research_library.analysis.llm.base import LLMError
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.semantic import semantic_search

    load_env()
    ovr = (semantic_backend or "").strip() or None
    try:
        conn = library_db.connect()
        rows = semantic_search(
            conn,
            query.strip(),
            limit=max(1, min(int(limit), 100)),
            semantic_backend=ovr,
        )
        return json.dumps(rows, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "semantic_runtime", "message": str(e)}, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def library_semantic_index(
    paper_ids: str = "", force: bool = False, semantic_backend: str = ""
) -> str:
    """Build/rebuild PDF chunk index. semantic_backend=fts avoids Chroma and embedding APIs."""
    from research_library.analysis.llm.base import LLMError
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.semantic import index_papers

    load_env()
    ovr = (semantic_backend or "").strip() or None

    try:
        conn = library_db.connect()
        ids_list: list[int] | None = None
        raw = (paper_ids or "").strip()
        if raw:
            ids_list = []
            for ln in raw.replace(",", "\n").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                ids_list.append(int(ln))
        out = index_papers(conn, ids_list, force=bool(force), semantic_backend=ovr)
        return json.dumps(out, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "semantic_runtime", "message": str(e)}, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": "bad_paper_ids", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def library_get_related_papers(
    paper_id: int,
    limit: int = 8,
    related_mode: str = "semantic",
    semantic_backend: str = "",
) -> str:
    """Related papers from chunk similarity (FTS keyword overlap or embedding neighbors)."""
    from research_library.analysis.llm.base import LLMError
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.semantic import get_related_papers

    load_env()
    ovr = (semantic_backend or "").strip() or None
    try:
        conn = library_db.connect()
        rows = get_related_papers(
            conn,
            int(paper_id),
            limit=max(1, min(int(limit), 50)),
            related_mode=(related_mode or "semantic").strip(),
            semantic_backend=ovr,
        )
        return json.dumps(rows, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "semantic_runtime", "message": str(e)}, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)
    except ValueError as e:
        return json.dumps({"error": "related", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def library_compare_topic(
    topic: str,
    paper_ids: str,
    schema_hint: str = "",
    chunks_per_paper: int = 4,
    semantic_backend: str = "",
) -> str:
    """LLM compares papers using top chunks per paper (FTS or vector chunk retrieval + chat)."""
    from research_library.analysis.llm.base import LLMError
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.semantic_compare import extract_topic_metrics

    load_env()
    ovr = (semantic_backend or "").strip() or None
    lines: list[str] = []
    for part in (paper_ids or "").replace(",", "\n").splitlines():
        s = part.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    try:
        pids = [int(x) for x in lines]
    except ValueError as e:
        return json.dumps({"error": "bad_paper_ids", "message": str(e)}, ensure_ascii=False)
    if not pids:
        return json.dumps({"error": "paper_ids_required"}, ensure_ascii=False)
    try:
        conn = library_db.connect()
        out = extract_topic_metrics(
            conn,
            topic.strip(),
            pids,
            schema_hint=schema_hint or "",
            chunks_per_paper=max(1, min(int(chunks_per_paper), 20)),
            semantic_backend=ovr,
        )
        return json.dumps(out, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "semantic_runtime", "message": str(e)}, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def library_topic_dossier(
    topic: str,
    extra_queries: str = "",
    per_query_limit: int = 12,
    synthesize: bool = True,
    semantic_backend: str = "",
) -> str:
    """Multi-query semantic chunk gather, dedupe, optional LLM markdown synthesis."""
    from research_library.analysis.llm.base import LLMError
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.topic_dossier import build_topic_dossier

    load_env()
    ovr = (semantic_backend or "").strip() or None
    extra: list[str] = []
    for ln in (extra_queries or "").splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            extra.append(s)
    try:
        conn = library_db.connect()
        out = build_topic_dossier(
            conn,
            topic.strip(),
            extra_queries=extra or None,
            per_query_limit=max(1, min(int(per_query_limit), 40)),
            semantic_backend=ovr,
            synthesize=bool(synthesize),
        )
        return json.dumps(out, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "semantic_runtime", "message": str(e)}, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def library_semantic_report(
    query: str,
    extra_queries: str = "",
    expand_queries: bool = False,
    per_query_limit: int = 12,
    ref_limit_per_paper: int = 30,
    max_context_chars: int = 56000,
    synthesize: bool = True,
    semantic_backend: str = "",
) -> str:
    """Semantic chunk search, attach each paper's ``paper_references``, LLM markdown with **[S1]** tags."""
    from research_library.analysis.llm.base import LLMError
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.report import build_semantic_report

    load_env()
    ovr = (semantic_backend or "").strip() or None
    extra: list[str] = []
    for ln in (extra_queries or "").splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            extra.append(s)
    try:
        conn = library_db.connect()
        out = build_semantic_report(
            conn,
            query.strip(),
            extra_queries=extra or None,
            expand_queries=bool(expand_queries),
            per_query_limit=max(1, min(int(per_query_limit), 80)),
            ref_limit_per_paper=max(1, min(int(ref_limit_per_paper), 200)),
            semantic_backend=ovr,
            synthesize=bool(synthesize),
            max_context_chars=max(4000, min(int(max_context_chars), 200_000)),
        )
        return json.dumps(out, ensure_ascii=False)
    except RuntimeError as e:
        return json.dumps({"error": "semantic_runtime", "message": str(e)}, ensure_ascii=False)
    except LLMError as e:
        return json.dumps({"error": "llm", "message": str(e)}, ensure_ascii=False)


@mcp.tool()
def library_citation_ingest_hubs(bibcodes: str) -> str:
    """After reviewing missing_hubs, ingest newline-separated bibcodes from ADS into library.db."""
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.citations import ingest_hub_bibcodes

    load_env()
    conn = library_db.connect()
    lines = [ln.strip() for ln in bibcodes.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    out = ingest_hub_bibcodes(conn, lines)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
def library_fetch_source(
    paper_ids: str = "",
    backend: str = "",
    force: bool = False,
) -> str:
    """Pull TeX-derived clean text (ar5iv HTML by default) for papers with arxiv_id.

    Writes ``data/sources/<paper_id>/main.txt`` + ``sections.json`` and updates
    ``papers.source_text_relpath`` / ``source_kind`` / ``source_backend``.
    ``paper_ids``: newline- or comma-separated ints; empty = all papers with arxiv_id.
    """
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.tex_to_text import fetch_source_for_paper

    load_env()
    conn = library_db.connect()
    raw = (paper_ids or "").strip()
    if raw:
        target_ids: list[int] = []
        for ln in raw.replace(",", "\n").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            try:
                target_ids.append(int(ln))
            except ValueError:
                continue
    else:
        target_ids = library_db.list_paper_ids_with_arxiv(conn)
    be = (backend or "").strip() or None
    items = []
    errors = 0
    for pid in target_ids:
        try:
            r = fetch_source_for_paper(conn, int(pid), backend=be, force=bool(force))
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "paper_id": int(pid), "error": str(e)}
        items.append(r)
        if not r.get("ok"):
            errors += 1
    return json.dumps(
        {"requested": len(target_ids), "errors": errors, "items": items},
        ensure_ascii=False,
    )


@mcp.tool()
def library_update_pdf(
    paper_ids: str = "",
    source: str = "auto",
    timeout: int = 120,
    reindex: bool = False,
) -> str:
    """Refetch PDF (publisher version preferred); overwrites existing file under data/pdfs/.

    ``paper_ids``: newline- or comma-separated ints; empty = all papers with current pdf_relpath.
    ``source``: ``auto`` (pub > ads > eprint > arxiv) or one specific label.
    """
    from research_library.config import load_env
    from research_library.library import db as library_db
    from research_library.library.pdf_update import update_pdfs

    load_env()
    conn = library_db.connect()
    raw = (paper_ids or "").strip()
    if raw:
        target_ids: list[int] = []
        for ln in raw.replace(",", "\n").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            try:
                target_ids.append(int(ln))
            except ValueError:
                continue
    else:
        target_ids = library_db.list_paper_ids_with_pdf(conn)
    out = update_pdfs(
        conn,
        target_ids,
        source=(source or "auto"),
        timeout=max(15, int(timeout)),
        reindex=bool(reindex),
    )
    return json.dumps(out, ensure_ascii=False)


def main() -> None:
    from research_library.config import load_env

    load_env()
    tok = os.environ.get("ADS_API_TOKEN", "")
    if tok:
        os.environ.setdefault("ADS_API_TOKEN", tok.strip())
    mcp.run()


if __name__ == "__main__":
    main()
