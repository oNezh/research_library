"""Offline tests for reference-chain use of paper_references."""

from __future__ import annotations

import json

import pytest


def test_list_paper_reference_edges_joins_cited_paper(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    from research_library.library import db as library_db

    conn = library_db.connect()
    library_db.init_schema(conn)
    library_db.upsert_paper(
        conn,
        arxiv_id="0704.0007",
        title="Citing work",
        bibcode="2007PhRvD..76l4016B",
        abstract="",
        pdf_relpath="seed.pdf",
    )
    library_db.upsert_paper(
        conn,
        arxiv_id="0704.0008",
        title="Cited target",
        bibcode="2008ApJ...678...99C",
        abstract="",
        pdf_relpath=None,
    )
    row = conn.execute(
        "SELECT id FROM papers WHERE bibcode = ?", ("2007PhRvD..76l4016B",)
    ).fetchone()
    pid = int(row[0])
    library_db.replace_paper_references(conn, pid, ["2008ApJ...678...99C", "2099ApJ...999..9Z"])
    conn.commit()

    edges = library_db.list_paper_reference_edges(conn, pid)
    assert len(edges) == 2
    by_bc = {e["ref_bibcode"]: e for e in edges}
    assert by_bc["2008ApJ...678...99C"]["to_paper_id"] is not None
    assert by_bc["2008ApJ...678...99C"]["title"] == "Cited target"
    assert by_bc["2008ApJ...678...99C"]["has_local_pdf"] is False
    assert by_bc["2099ApJ...999..9Z"]["to_paper_id"] is None


def test_analyze_pdf_reference_chain_respects_use_library_references_false(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    from research_library.analysis import pdf as pdf_mod
    from research_library.library import db as library_db

    conn = library_db.connect()
    library_db.init_schema(conn)
    data_root = tmp_path
    pdf_rel = "papers/citing.pdf"
    (data_root / "papers").mkdir(parents=True)
    seed = data_root / pdf_rel
    seed.write_bytes(
        b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    )
    abs_seed = str(seed.resolve())
    library_db.upsert_paper(
        conn,
        arxiv_id="0704.0011",
        title="Local seed",
        bibcode="2011ARA&A..49..173E",
        abstract="",
        pdf_relpath=pdf_rel.replace("\\", "/"),
    )
    row = conn.execute("SELECT id FROM papers WHERE arxiv_id = ?", ("0704.0011",)).fetchone()
    pid = int(row[0])
    library_db.replace_paper_references(conn, pid, ["2008ApJ...678...99C"])
    conn.commit()
    conn.close()

    class _FakeLLM:
        def __init__(self):
            self.last_usage = None

        def chat(self, messages, **kwargs):  # noqa: ARG002
            return json.dumps(
                {
                    "excerpts": ["x"],
                    "follow_ref_numbers": [],
                    "rationale": "t",
                },
                ensure_ascii=False,
            )

    tex = (
        "Intro text.\n\n"
        "REFERENCES\n\n"
        "Author, A., et al. 2020, Journal, 1, 1\n"
        "Boson, B. 2021, Other, 2, 2\n"
    )
    monkeypatch.setattr(pdf_mod, "extract_pdf_text", lambda _p: tex)
    monkeypatch.setattr(pdf_mod, "get_chat_client", lambda _p=None: _FakeLLM())

    out_lib = pdf_mod.analyze_pdf_reference_chain(
        abs_seed, "q?", client=_FakeLLM(), max_hops=0, use_library_references=True
    )
    tr = [x for x in out_lib["trace"] if x.get("depth") == 0]
    assert tr
    assert tr[0].get("ref_parse_mode") == "library_graph"
    assert tr[0].get("ref_list_size") == 1

    out_pdf = pdf_mod.analyze_pdf_reference_chain(
        abs_seed, "q?", client=_FakeLLM(), max_hops=0, use_library_references=False
    )
    tr2 = [x for x in out_pdf["trace"] if x.get("depth") == 0]
    assert tr2[0].get("ref_parse_mode") in ("sequential", "numbered")
    assert tr2[0].get("ref_list_size") == 2


def test_chain_library_mode_no_pdf_biblio_fallback_without_edges(tmp_path, monkeypatch):
    """With library refs on but empty paper_references, do not parse PDF REFERENCES body."""
    monkeypatch.setenv("RESEARCH_LIBRARY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADS_API_TOKEN", raising=False)
    from research_library.analysis import pdf as pdf_mod
    from research_library.library import db as library_db

    conn = library_db.connect()
    library_db.init_schema(conn)
    data_root = tmp_path
    pdf_rel = "papers/only.pdf"
    (data_root / "papers").mkdir(parents=True)
    seed = data_root / pdf_rel
    seed.write_bytes(
        b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    )
    abs_seed = str(seed.resolve())
    library_db.upsert_paper(
        conn,
        arxiv_id="0704.0099",
        title="No ref edges",
        bibcode="2099ApJ...999..9Z",
        abstract="",
        pdf_relpath=pdf_rel.replace("\\", "/"),
    )
    conn.commit()
    conn.close()

    class _FakeLLM:
        def chat(self, messages, **kwargs):  # noqa: ARG002
            return json.dumps(
                {
                    "excerpts": [],
                    "follow_ref_numbers": [],
                    "rationale": "t",
                },
                ensure_ascii=False,
            )

    tex = (
        "Intro.\n\nREFERENCES\n\n"
        "Smith, S. 2020, ApJ, 1, 1\n"
        "Jones, J. 2021, MNRAS, 2, 2\n"
    )
    monkeypatch.setattr(pdf_mod, "extract_pdf_text", lambda _p: tex)
    monkeypatch.setattr(pdf_mod, "get_chat_client", lambda _p=None: _FakeLLM())

    out = pdf_mod.analyze_pdf_reference_chain(
        abs_seed, "q?", client=_FakeLLM(), max_hops=0, use_library_references=True
    )
    tr = [x for x in out["trace"] if x.get("depth") == 0]
    assert tr
    assert tr[0].get("ref_parse_mode") == "empty"
    assert tr[0].get("ref_list_size") == 0
