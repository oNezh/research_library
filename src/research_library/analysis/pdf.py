"""Summarize a paper PDF, extract passages for a user question, optional multi-hop ref chain + MD report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from research_library.config import get_data_dir, load_env
from research_library.log import get_logger, log_event
from research_library.library.reference_acquire import acquire_pdf
from research_library.library.reference_ingest import ingest_downloaded_reference
from research_library.library.reference_parse import (
    StandardRef,
    extract_arxiv_from_text,
    extract_doi_from_text,
    parse_bibliography_fragment,
    parse_catalog_line,
)

from .llm.base import ChatClient, ChatMessage, LLMError
from .llm.minimax import _default_max_completion_tokens
from .llm.registry import get_chat_client


_PDF_TEXT_CACHE: Dict[Tuple[str, float, int], str] = {}
_PDF_TEXT_CACHE_MAX = 32


def extract_pdf_text(pdf_path: str) -> str:
    """Extract PDF text with a small per-process cache keyed by ``(path, mtime, size)``."""
    try:
        st = os.stat(pdf_path)
        key = (os.path.abspath(pdf_path), st.st_mtime, st.st_size)
        cached = _PDF_TEXT_CACHE.get(key)
        if cached is not None:
            return cached
    except OSError:
        key = None
    try:
        from pdfminer.high_level import extract_text

        out = extract_text(pdf_path) or ""
    except ImportError as e:
        raise RuntimeError("pdfminer.six is required for pdf-analyze") from e
    except Exception:
        out = ""
    if key is not None:
        if len(_PDF_TEXT_CACHE) >= _PDF_TEXT_CACHE_MAX:
            # FIFO eviction — drop arbitrary oldest entry
            try:
                _PDF_TEXT_CACHE.pop(next(iter(_PDF_TEXT_CACHE)))
            except StopIteration:
                pass
        _PDF_TEXT_CACHE[key] = out
    return out


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    t = text.strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t, False
    omitted = len(t) - max_chars + 120
    head = int(max_chars * 0.55)
    tail = max(4000, max_chars - head - 120)
    frag = (
        t[:head]
        + f"\n\n---\n[... 已省略约 {omitted} 字符 ...]\n---\n\n"
        + t[-tail:]
    )
    return frag, True


def _usage_row(llm: Any, call: str) -> Dict[str, Any] | None:
    raw = getattr(llm, "last_usage", None)
    if not raw:
        return None
    return {"call": call, **dict(raw)}


def _sum_llm_usage(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")
    out: Dict[str, int] = {k: 0 for k in keys}
    for r in rows:
        for k in keys:
            v = r.get(k)
            if v is not None:
                try:
                    out[k] += int(v)
                except (TypeError, ValueError):
                    pass
    return out


def analyze_pdf(
    pdf_path: str,
    *,
    question: Optional[str] = None,
    client: Optional[ChatClient] = None,
    provider: Optional[str] = None,
    max_chars: Optional[int] = None,
    max_summary_tokens: Optional[int] = None,
    max_answer_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    load_env()
    path = os.path.abspath(os.path.expanduser(pdf_path))
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    text = extract_pdf_text(path)
    if not text.strip():
        raise ValueError("Could not extract text from PDF (empty or unreadable).")

    lim_raw = max_chars
    if lim_raw is None:
        lim_raw = int(os.environ.get("RESEARCH_PDF_ANALYZE_MAX_CHARS") or "100000")

    llm = client or get_chat_client(provider)
    cap = _default_max_completion_tokens()
    summary_cap = max_summary_tokens if max_summary_tokens is not None else cap
    answer_cap = max_answer_tokens if max_answer_tokens is not None else cap
    usage_rows: List[Dict[str, Any]] = []

    from research_library.library import db as _library_db
    from research_library.library.semantic import retrieve_context_for_paper_question as _retrieve_chunks

    _conn = _library_db.connect()
    _pid = _library_db.paper_id_for_absolute_pdf(_conn, path)
    q = (question or "").strip()
    question_body = None
    question_ctx_source = None
    if (
        q
        and _pid is not None
        and _library_db.paper_chunk_count(_conn, _pid) > 0
    ):
        ctx, tag = _retrieve_chunks(
            _conn,
            _pid,
            q,
            max_chars=min(lim_raw, 36_000),
            max_chunks=18,
        )
        if tag and ctx.strip():
            question_body = ctx
            question_ctx_source = tag

    body, truncated = truncate_text(text, lim_raw)

    summary_messages: List[ChatMessage] = [
        {
            "role": "system",
            "content": (
                "你是学术文献助手。只根据给出的论文文本写中文摘要：研究背景与问题、方法、"
                "主要结果与结论。不要编造文中没有的内容。"
            ),
        },
        {
            "role": "user",
            "content": (
                "以下是从 PDF 解析的文本（可能经过首尾截断以适配长度）：\n\n"
                f"{body}\n\n请用若干短段落输出中文摘要。"
            ),
        },
    ]
    summary = llm.chat(
        summary_messages,
        max_completion_tokens=summary_cap,
        temperature=0.35,
    )
    u0 = _usage_row(llm, "summary")
    if u0:
        usage_rows.append(u0)

    focused: str | None = None
    if q:
        body_for_q = question_body if question_body is not None else body
        qa_messages: List[ChatMessage] = [
            {
                "role": "system",
                "content": (
                    "你是学术文献助手。只能依据给定文本回答：先列出与问题最直接相关的原文摘录"
                    "（用中文引号「」标出连续原文，可多条），再用简短中文说明这些文字如何回应问题。"
                    "若几乎无关，说明「文中未直接涉及该问题」，并可简述最接近的表述（若有）。"
                ),
            },
            {
                "role": "user",
                "content": f"论文文本摘录：\n\n{body_for_q}\n\n用户问题：{q}\n\n请按要求输出。",
            },
        ]
        focused = llm.chat(
            qa_messages,
            max_completion_tokens=answer_cap,
            temperature=0.25,
        )
        u1 = _usage_row(llm, "question_focused")
        if u1:
            usage_rows.append(u1)

    return {
        "pdf_path": path,
        "source_chars": len(text),
        "llm_input_chars": len(body),
        "llm_question_context_chars": len(question_body) if question_body else len(body),
        "truncated": truncated,
        "library_paper_id": _pid,
        "question_context_source": question_ctx_source,
        "summary": summary.strip(),
        "question": q or None,
        "question_focused": focused.strip() if focused else None,
        "llm_usage": usage_rows,
        "llm_usage_totals": _sum_llm_usage(usage_rows),
    }


# --- Reference chain (multi-hop) ------------------------------------------------

_REF_HEAD_RE = re.compile(
    r"(?ms)^\s*(?:References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*\n"
)
_LINE_NUM_BRACKET = re.compile(r"^\s*\[(\d{1,3})\]\s*(.*)$")
_LINE_NUM_DOT = re.compile(r"^\s*(\d{1,3})\.\s+(.*)$")
# New bibliography entry often starts with "Surname, Initial." or "Surname, A. B.,"
_REF_NEW_ENTRY = re.compile(
    r"^\s*[A-Z][a-zA-Z'\u2019`-]{0,28},\s+(?:[A-Z]\.|\[[\d,-]+\]|[A-Z][a-z]+|[A-Z][a-z]+\s*&)"
)


def _find_references_section(full_text: str) -> str:
    m = _REF_HEAD_RE.search(full_text)
    if not m:
        return ""
    return full_text[m.end() :]


def _parse_numbered_refs(section: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    if not section.strip():
        return out
    cur: Optional[int] = None
    buf: List[str] = []

    def flush() -> None:
        nonlocal cur, buf
        if cur is not None and buf:
            out[cur] = " ".join(buf).strip()
        buf = []

    for line in section.splitlines():
        mb = _LINE_NUM_BRACKET.match(line)
        md = _LINE_NUM_DOT.match(line) if not mb else None
        if mb:
            flush()
            cur = int(mb.group(1))
            rest = mb.group(2).strip()
            buf = [rest] if rest else []
        elif md:
            flush()
            cur = int(md.group(1))
            rest = (md.group(2) or "").strip()
            buf = [rest] if rest else []
        elif cur is not None and line.strip():
            buf.append(line.strip())
    flush()
    return out


_REF_GLUE_NEXT_AUTHOR = re.compile(
    r"\s+"
    # Next ref starts with ``Surname, I.`` or ``Surname I.,`` (common in MNRAS-style layouts).
    r"(?=[A-Z][a-zA-Z'\u2019`-]{1,24}(?:,\s+[A-Z]\.|\s+[A-Z]\.,))"
)


def _split_concatenated_ref_block(block: str) -> List[str]:
    """Split two author--year entries glued in one line (column / wrap artefacts)."""
    b = re.sub(r"\s+", " ", block.strip())
    if not b or len(b) < 35:
        return [b] if b else []
    cuts: List[int] = []
    for m in _REF_GLUE_NEXT_AUTHOR.finditer(b):
        left = b[: m.start()].strip()
        if len(left) < 18:
            continue
        if not re.search(r",\s*(?:19|20)\d{2}\s*,\s*[A-Za-z&]", left):
            continue
        cuts.append(m.start())
    if not cuts:
        return [b]
    pieces: List[str] = []
    start = 0
    for c in cuts:
        piece = b[start:c].strip()
        if piece:
            pieces.append(piece)
        start = c
    tail = b[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces if len(pieces) >= 2 else [b]


def _parse_sequential_refs(section: str) -> Dict[int, str]:
    """Bibliography without [n] / 'n.' — split into entries by author-line boundaries or blank lines."""
    s = section.strip()
    if not s:
        return {}
    lines0 = s.splitlines()
    if lines0 and re.match(r"(?i)^\s*(references|bibliography)\s*$", lines0[0].strip()):
        s = "\n".join(lines0[1:]).lstrip()
    if not s.strip():
        return {}
    blocks: List[str] = []
    buf = []
    for line in s.splitlines():
        st = line.strip()
        if not st:
            if buf:
                blocks.append(" ".join(buf))
                buf = []
            continue
        if buf and _REF_NEW_ENTRY.match(line):
            blocks.append(" ".join(buf))
            buf = [st]
        else:
            buf.append(st)
    if buf:
        blocks.append(" ".join(buf))
    expanded: List[str] = []
    for b in blocks:
        expanded.extend(_split_concatenated_ref_block(b))
    out = {
        i + 1: re.sub(r"\s+", " ", b.strip())
        for i, b in enumerate(expanded)
        if b.strip()
    }
    if len(out) >= 2:
        return out
    paras = [p.strip() for p in re.split(r"\n\s*\n+", s) if p.strip()]
    if len(paras) >= 2:
        exp2: List[str] = []
        for p in paras:
            exp2.extend(_split_concatenated_ref_block(p))
        return {i + 1: re.sub(r"\s+", " ", x.strip()) for i, x in enumerate(exp2) if x.strip()}
    return out


def _build_ref_map(section: str) -> Tuple[Dict[int, str], str]:
    """Prefer explicit numbering (e.g. Nature); else sequential indices for author–year bib lists."""
    numbered = _parse_numbered_refs(section)
    sequential = _parse_sequential_refs(section)
    if len(numbered) >= 3:
        return numbered, "numbered"
    if len(sequential) >= len(numbered) and len(sequential) >= 2:
        return sequential, "sequential"
    if numbered:
        return numbered, "numbered"
    if sequential:
        return sequential, "sequential"
    return {}, "empty"


def _ref_map_from_library_edges(
    edges: List[Dict[str, Any]],
) -> Tuple[Dict[int, str], Dict[int, str], str]:
    """Build numbered ref prompt lines from ``paper_references`` + optional cited-paper join."""
    ref_map: Dict[int, str] = {}
    num_to_bib: Dict[int, str] = {}
    idx = 0
    for e in edges:
        bc = (e.get("ref_bibcode") or "").strip()
        if not bc:
            continue
        idx += 1
        title = (e.get("title") or "").strip()
        to_pid = e.get("to_paper_id")
        has_pdf = bool(e.get("has_local_pdf"))
        if has_pdf and title:
            line = f"[库内PDF] {_short_label(title, 200)} | {bc}"
        elif to_pid is not None and title:
            line = f"[库内] {_short_label(title, 200)} | {bc}"
        elif to_pid is not None:
            line = f"[库内] {bc}"
        else:
            line = bc
        ref_map[idx] = line
        num_to_bib[idx] = bc
    return ref_map, num_to_bib, "library_graph"


def _author_year_hits_from_text(text: str) -> List[Tuple[str, str]]:
    """(surname_lower, year) from in-text astro-style citations."""
    if not text.strip():
        return []
    raw = text
    pairs: List[Tuple[str, str]] = []

    def add_surname_year(s: str, y: str) -> None:
        su = s.strip().lower().split()[0] if s.strip() else ""
        if su and len(y) == 4 and y.isdigit():
            pairs.append((su, y))

    for m in re.finditer(
        r"\b([A-Z][a-z]+(?:-[A-Z][a-z]+)?)\s+et\s+al\.?\s*[,(]?\s*((?:19|20)\d{2})\b",
        raw,
    ):
        add_surname_year(m.group(1), m.group(2))
    for m in re.finditer(
        r"\b([A-Z][a-z]+)\s*&\s*[A-Z][a-z]+\s+((?:19|20)\d{2})\b",
        raw,
    ):
        add_surname_year(m.group(1), m.group(2))
    for m in re.finditer(
        r"\(([A-Z][a-z]+(?:\s+et\s+al\.?)?)\s+((?:19|20)\d{2})\)",
        raw,
    ):
        add_surname_year(m.group(1).replace(" et al.", "").replace(" et al", ""), m.group(2))

    seen: Set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []
    for p in pairs:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _ref_scores_for_haystack(ref_map: Dict[int, str], joined: str) -> Dict[int, float]:
    """Score each bibliography index against a single haystack string (author–year + token overlap)."""
    if not joined or not ref_map:
        return {n: 0.0 for n in ref_map}
    hay_lower = re.sub(r"\s+", " ", joined).lower()
    scored: Dict[int, float] = {}

    def bump(n: int, delta: float) -> None:
        scored[n] = scored.get(n, 0.0) + delta

    for sur, yr in _author_year_hits_from_text(joined):
        for n, line in ref_map.items():
            ll = line.lower()
            if yr not in line:
                continue
            if re.search(rf"(?<![a-z]){re.escape(sur)}(?![a-z])", ll):
                bump(n, 25.0)
            elif re.search(rf"(?<![a-z]){re.escape(sur)}(?![a-z])", hay_lower):
                bump(n, 10.0)

    for n, line in ref_map.items():
        score = scored.get(n, 0.0)
        for y in re.findall(r"\b(19\d{2}|20\d{2})\b", line):
            if y in hay_lower:
                score += 3.0
        head = line.split(",", 1)[0].strip()
        for tok in re.findall(r"[A-Za-z][A-Za-z'`\-]{2,}", head):
            tl = tok.lower()
            if len(tl) < 3 or tl in {"the", "and", "for", "von", "van", "der", "los", "las"}:
                continue
            if re.search(rf"(?<![a-z]){re.escape(tl)}(?![a-z])", hay_lower):
                score += 2.0
        scored[n] = score
    return scored


def _refs_matching_haystacks(
    ref_map: Dict[int, str],
    haystacks: Sequence[str],
    *,
    max_refs: int = 8,
) -> List[int]:
    """Match bibliography lines using author–year cues + token overlap (body + question + excerpts)."""
    joined = " ".join(h.strip() for h in haystacks if h and str(h).strip()).strip()
    if not joined or not ref_map:
        return []
    scored = _ref_scores_for_haystack(ref_map, joined)
    ranked = sorted(
        ((n, s) for n, s in scored.items() if s > 0),
        key=lambda x: (-x[1], x[0]),
    )
    out: List[int] = []
    for n, _ in ranked:
        if n not in out:
            out.append(n)
        if len(out) >= max_refs:
            break
    return out


def _narrow_ref_map_for_llm(
    ref_map: Dict[int, str],
    haystack: str,
    *,
    trigger_above: int,
    max_in_prompt: int,
    must_pick_refs: int,
) -> Tuple[Dict[int, str], Dict[int, int]]:
    """If ref list is long, renumber a subset for the LLM only. Return (prompt_map, prompt_num_to_orig)."""
    if len(ref_map) <= trigger_above or not ref_map:
        ident = {n: n for n in sorted(ref_map.keys())}
        return dict(ref_map), ident

    scored = _ref_scores_for_haystack(ref_map, haystack)
    must = set(
        _refs_matching_haystacks(
            ref_map,
            [haystack],
            max_refs=max(must_pick_refs, 12),
        )
    )
    ranked = sorted(ref_map.keys(), key=lambda n: (-scored.get(n, 0.0), n))
    picked: List[int] = []
    seen: Set[int] = set()

    def take(n: int) -> None:
        if n in ref_map and n not in seen:
            seen.add(n)
            picked.append(n)

    for n in sorted(must):
        if len(picked) >= max_in_prompt:
            break
        take(n)
    for n in ranked:
        take(n)
        if len(picked) >= max_in_prompt:
            break
    for n in sorted(ref_map.keys()):
        if len(picked) >= max_in_prompt:
            break
        take(n)

    prompt_map = {i + 1: ref_map[picked[i]] for i in range(len(picked))}
    inv = {i + 1: picked[i] for i in range(len(picked))}
    return prompt_map, inv


def _chain_retrieval_query(question: str, ctx_boost: str, *, cap: int = 6000) -> str:
    q = (question or "").strip()
    b = (ctx_boost or "").strip()
    if not b:
        return q[:cap]
    return (q + "\n\n" + b)[:cap]


def _chain_follow_combine(
    model_follow: List[int],
    citation_follow: List[int],
    method_nums: List[int],
    *,
    tight: bool,
) -> List[int]:
    """Loose: union. Tight: model ∩ (citation ∪ method), or model alone if hints empty."""
    mf, cf, mn = set(model_follow), set(citation_follow), set(method_nums)
    if tight:
        hints = cf | mn
        if hints:
            out = mf & hints
        else:
            out = mf
        return sorted(out)
    return sorted(mf | cf | mn)


def _maybe_auto_semantic_index_paper(conn: Any, pdf_abs: str) -> Optional[str]:
    """If enabled, chunk+embed a newly-ingested PDF when it has no chunks yet."""
    raw = (os.environ.get("RESEARCH_PDF_CHAIN_AUTO_SEMANTIC_INDEX") or "").strip().lower()
    if raw not in ("1", "true", "yes"):
        return None
    try:
        from research_library.library import db as library_db
        from research_library.library import semantic as sem

        library_db.ensure_schema(conn)
        ap = os.path.abspath(pdf_abs)
        pid = library_db.paper_id_for_absolute_pdf(conn, ap)
        if pid is None:
            return "no_paper_id"
        if library_db.paper_chunk_count(conn, pid) > 0:
            return "already_indexed"
        sem.index_paper(conn, pid, force=False)
        conn.commit()
        return "indexed"
    except Exception as e:
        return f"error:{e}"


def _standard_ref_for_reference_line(line: str, conn: Any) -> StandardRef:
    """Bibliography line → ``StandardRef``: ADS 题录解析；若无 DOI/arXiv/bibcode 再用 fragment 抽行内标识。"""
    raw = (line or "").strip()
    cat = parse_catalog_line(raw, conn, use_ads=True)
    if cat.bibcode or cat.arxiv_id or cat.doi:
        return cat
    frag = parse_bibliography_fragment(raw, conn)
    if frag.bibcode or frag.arxiv_id or frag.doi:
        return frag
    return cat


def _ref_visit_key(ref_line: str) -> str:
    aid = extract_arxiv_from_text(ref_line)
    if aid:
        return f"arxiv:{aid.lower()}"
    doi = extract_doi_from_text(ref_line)
    if doi:
        return f"doi:{doi.lower()}"
    h = hashlib.sha256(ref_line.encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"raw:{h}"


def _short_label(ref_line: str, max_len: int = 72) -> str:
    one = re.sub(r"\s+", " ", ref_line.strip())
    if len(one) <= max_len:
        return one
    return one[: max_len - 1] + "…"


def _excerpt_in_body(excerpt: str, body: str, *, min_overlap: float = 0.6) -> bool:
    """Cheap grounding check: substring match OR ≥ ``min_overlap`` token Jaccard.

    Returns True if the excerpt is plausibly drawn from ``body``. We strip
    whitespace and lowercase before comparing; for non-trivial Chinese/English
    excerpts we accept either a direct substring hit (after collapsing
    whitespace) or a token-set overlap above the threshold.
    """
    ex = (excerpt or "").strip()
    if not ex:
        return False
    src = body or ""
    ex_norm = re.sub(r"\s+", " ", ex).lower()
    src_norm = re.sub(r"\s+", " ", src).lower()
    if len(ex_norm) < 24 and ex_norm in src_norm:
        return True
    if ex_norm in src_norm:
        return True
    ex_toks = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", ex_norm))
    if not ex_toks:
        return False
    src_toks = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", src_norm))
    if not src_toks:
        return False
    overlap = len(ex_toks & src_toks) / max(1, len(ex_toks))
    return overlap >= min_overlap


def _verify_excerpts(
    excerpts: List[str], body: str
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """Split LLM-returned excerpts into (verified, dropped) using ``_excerpt_in_body``."""
    if not excerpts:
        return [], []
    verified: List[str] = []
    dropped: List[Dict[str, Any]] = []
    for ex in excerpts:
        s = str(ex).strip()
        if not s:
            continue
        if _excerpt_in_body(s, body):
            verified.append(s)
        else:
            dropped.append({"excerpt": s[:400], "reason": "not_in_chunks"})
    return verified, dropped


def _parse_llm_json_obj(text: str) -> Dict[str, Any]:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1)
    else:
        i = t.find("{")
        j = t.rfind("}")
        if i >= 0 and j > i:
            t = t[i : j + 1]
    return json.loads(t)


def _ref_map_for_prompt(ref_dict: Dict[int, str], limit: int = 80) -> str:
    lines = []
    for n in sorted(ref_dict.keys())[:limit]:
        lines.append(f"[{n}] {_short_label(ref_dict[n], 300)}")
    if len(ref_dict) > limit:
        lines.append(f"... 另有 {len(ref_dict) - limit} 条参考文献未列出")
    return "\n".join(lines) if lines else "(参考文献列表解析为空)"


def _llm_chain_step(
    llm: ChatClient,
    *,
    paper_body: str,
    question: str,
    ref_index_text: str,
    max_tokens: int = 32768,
) -> Dict[str, Any]:
    sys_prompt = (
        "你是学术文献分析助手。只依据给定的「论文正文」与「参考文献索引表」回答。\n"
        "天体物理类论文正文**通常**使用作者–年份（如 Smith et al. 2019）、有时是上标或叙述式引用，"
        "**很少**在正文里出现方括号编号如 [12]；不要以此为门槛。\n"
        "你必须输出**唯一一段**合法 JSON（不要 Markdown），格式：\n"
        '{"excerpts": ["原文摘录1", ...], "follow_ref_numbers": [整数编号, ...], "rationale": "中文简述"}\n'
        "要求：\n"
        "1) excerpts：从正文复制的连续片段（可多条），可含人名、年份、常见观测名等，无需含 [n]。\n"
        "2) follow_ref_numbers：若用户问题需要对照**原始文献**加深理解，请列出要进一步打开的条目。"
        "优先把 excerpts 里出现的**作者姓、年份**与索引表中各行的文字对照，选出对应编号；"
        "若正文只有「某团队」「前述工作」等无法唯一定位，则按与 excerpts / 用户问题**语义最直接**从表中选 **1–8** 条。"
        "索引表每行形如 [k] …，只输出整数 k，且 k 必须在表中真实存在（勿编造）。\n"
        "3) 若用户要求追文献/对比参考文献、或问题含「补充」「原始文献」「出处」等，则 **follow_ref_numbers 一般不应为空**（除非索引表解析为空）。\n"
        "4) rationale：说明摘录、问题与所选编号之间的关系。"
    )
    user_content = (
        f"用户问题：{question}\n\n"
        f"--- 参考文献索引表（[k] 仅为列表序号，正文未必出现 k）---\n{ref_index_text}\n\n"
        f"--- 论文正文（可能截断）---\n{paper_body}\n"
    )
    raw = llm.chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=max_tokens,
        temperature=0.2,
    )
    try:
        obj = _parse_llm_json_obj(raw)
    except (json.JSONDecodeError, ValueError):
        # NEVER feed raw model output back as ``excerpts`` — that would silently
        # break the "only-from-chunks" guarantee. Surface the parse failure
        # instead, so the synthesis step can flag this node as unverified.
        return {
            "excerpts": [],
            "follow_ref_numbers": [],
            "rationale": "模型未返回可解析 JSON；该步骤无可信摘录。",
            "parse_error": True,
            "_raw_preview": raw[:1200],
        }
    nums: List[int] = []
    for x in obj.get("follow_ref_numbers") or []:
        try:
            nums.append(int(x))
        except (TypeError, ValueError):
            continue
    ex = obj.get("excerpts") or []
    if not isinstance(ex, list):
        ex = [str(ex)]
    ex = [str(s).strip() for s in ex if str(s).strip()]
    return {
        "excerpts": ex,
        "follow_ref_numbers": nums,
        "rationale": str(obj.get("rationale") or "").strip(),
    }


def _llm_chain_step_follow_only(
    llm: ChatClient,
    *,
    paper_body: str,
    question: str,
    ref_index_text: str,
    max_tokens: int,
) -> Dict[str, Any]:
    sys_prompt = (
        "你是学术文献分析助手。仅根据「论文正文」与「参考文献索引表」判断应进一步打开哪些文献。\n"
        "天体物理论文常用作者–年份引用，正文中未必出现方括号编号 [k]。\n"
        "输出**唯一一段**合法 JSON（不要 Markdown），格式：\n"
        '{"follow_ref_numbers": [整数编号, ...], "rationale": "中文简述"}\n'
        "要求：索引表每行形如 [k]，只输出表中真实存在的整数 k；优先与问题及正文引用对应，选 1–8 条；**不要**输出 excerpts 字段。\n"
    )
    user_content = (
        f"用户问题：{question}\n\n"
        f"--- 参考文献索引表 ---\n{ref_index_text}\n\n"
        f"--- 论文正文（可能截断）---\n{paper_body}\n"
    )
    raw = llm.chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=max_tokens,
        temperature=0.2,
    )
    try:
        obj = _parse_llm_json_obj(raw)
    except (json.JSONDecodeError, ValueError):
        return {
            "follow_ref_numbers": [],
            "rationale": "跟进步骤未返回可解析 JSON。",
            "_raw": raw[:6000],
        }
    nums: List[int] = []
    for x in obj.get("follow_ref_numbers") or []:
        try:
            nums.append(int(x))
        except (TypeError, ValueError):
            continue
    return {
        "follow_ref_numbers": nums,
        "rationale": str(obj.get("rationale") or "").strip(),
    }


def _llm_chain_step_excerpts(
    llm: ChatClient,
    *,
    paper_body: str,
    question: str,
    ref_index_text: str,
    locked_ref_numbers: List[int],
    max_tokens: int,
) -> Dict[str, Any]:
    nums_s = ", ".join(str(x) for x in sorted(set(locked_ref_numbers))) or "(无)"
    sys_prompt = (
        "你是学术文献分析助手。下列**参考文献编号已锁定**，请勿改动编号；请只从「论文正文」摘录"
        "支撑用户问题与这些文献关联的**连续原文片段**（可多条）。\n"
        "输出**唯一一段**合法 JSON（不要 Markdown）：\n"
        '{"excerpts": ["原文摘录1", ...], "rationale": "中文简述"}\n'
        f"已锁定编号：{nums_s}。\n"
    )
    user_content = (
        f"用户问题：{question}\n\n"
        f"--- 参考文献索引表 ---\n{ref_index_text}\n\n"
        f"--- 论文正文（可能截断）---\n{paper_body}\n"
    )
    raw = llm.chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=max_tokens,
        temperature=0.2,
    )
    try:
        obj = _parse_llm_json_obj(raw)
    except (json.JSONDecodeError, ValueError):
        return {
            "excerpts": [],
            "rationale": "摘录步骤未返回可解析 JSON。",
            "_raw": raw[:6000],
        }
    ex = obj.get("excerpts") or []
    if not isinstance(ex, list):
        ex = [str(ex)]
    ex = [str(s).strip() for s in ex if str(s).strip()]
    return {
        "excerpts": ex,
        "rationale": str(obj.get("rationale") or "").strip(),
    }


def _trace_bundle_chunks(
    trace: List[Dict[str, Any]],
    *,
    max_node_chars: int,
    max_bundle_chars: int,
) -> List[str]:
    blocks: List[str] = []
    for node in trace:
        js = json.dumps(node, ensure_ascii=False, indent=2)
        if len(js) > max_node_chars:
            js = js[:max_node_chars] + "\n...(node truncated for LLM)...\n"
        blocks.append(js)
    bundles: List[str] = []
    buf: List[str] = []
    cur = 0
    sep = "\n\n--- NODE ---\n\n"
    for b in blocks:
        add = len(b) + (len(sep) if buf else 0)
        if buf and cur + add > max_bundle_chars:
            bundles.append(sep.join(buf))
            buf = [b]
            cur = len(b)
        else:
            if buf:
                cur += len(sep)
            buf.append(b)
            cur += len(b)
    if buf:
        bundles.append(sep.join(buf))
    return bundles


def _synthesize_one_bundle(
    llm: ChatClient,
    *,
    question: str,
    bundle: str,
    max_tokens: int,
    system_extra: str = "",
    compact: bool = False,
) -> str:
    sys_base = (
        "你是学术写作助手。下面是从多篇论文中递归摘录的结构化追踪结果（JSON 片段）。"
        "天文学论文常用作者–年份引用，follow_ref_numbers 是参考文献列表的程序索引。\n"
    )
    if system_extra:
        sys_base += system_extra + "\n"
    if compact:
        sys_base += "请用**简洁中文要点**（小标题 + 短列表），便于后续合并；不要写成长篇终稿。\n"
    else:
        sys_base += (
            "请写一份**中文 Markdown 报告**：\n"
            "## 问题\n## 主文献要点\n## 参考文献链路上的相关发现（按深度分段）\n## 综合结论与仍缺口\n"
            "只根据给定材料归纳；若某篇未解析到 PDF，请如实说明。\n"
        )
    messages: List[ChatMessage] = [
        {"role": "system", "content": sys_base},
        {"role": "user", "content": f"用户问题：{question}\n\n--- 追踪数据 ---\n{bundle}"},
    ]
    return llm.chat(messages, max_completion_tokens=max_tokens, temperature=0.3).strip()


def _synthesize_chain_markdown(
    llm: ChatClient,
    *,
    question: str,
    trace: List[Dict[str, Any]],
    max_tokens: int = 65536,
) -> str:
    max_node = int(os.environ.get("RESEARCH_PDF_CHAIN_SYNTH_NODE_MAX_CHARS", "8000") or "8000")
    max_bundle = int(os.environ.get("RESEARCH_PDF_CHAIN_SYNTH_BUNDLE_MAX_CHARS", "40000") or "40000")
    partial_cap = int(os.environ.get("RESEARCH_PDF_CHAIN_SYNTH_PARTIAL_MAX_TOKENS", "6144") or "6144")
    try:
        max_node = max(2000, min(max_node, 80_000))
        max_bundle = max(8000, min(max_bundle, 200_000))
        partial_cap = max(512, min(partial_cap, max_tokens))
    except ValueError:
        max_node, max_bundle = 8000, 40000
        partial_cap = min(6144, max_tokens)

    bundles = _trace_bundle_chunks(
        trace,
        max_node_chars=max_node,
        max_bundle_chars=max_bundle,
    )
    if len(bundles) <= 1:
        b0 = bundles[0] if bundles else "(无追踪数据)"
        return _synthesize_one_bundle(
            llm, question=question, bundle=b0, max_tokens=max_tokens
        )

    partials: List[str] = []
    for i, b in enumerate(bundles):
        part = _synthesize_one_bundle(
            llm,
            question=question,
            bundle=b,
            max_tokens=partial_cap,
            system_extra=f"这是分段 {i + 1}/{len(bundles)} 的追踪数据（非全文）。",
            compact=True,
        )
        partials.append(part)
    merged = "\n\n".join(
        f"### 分段{i + 1}草稿\n\n{p}" for i, p in enumerate(partials)
    )
    return _synthesize_one_bundle(
        llm,
        question=question,
        bundle=merged,
        max_tokens=max_tokens,
        system_extra="下列是多次调用得到的分段草稿，请**合并**为一份连贯的最终中文 Markdown 报告（保留四个大章节结构），去重并写清仍缺口。\n",
    )


def _write_session_state(
    session_dir: Path,
    *,
    trace: List[Dict[str, Any]],
    visited_keys: Set[str],
    usage_totals: Dict[str, int],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort dump of intermediate state — lets users recover trace data
    if the run is killed before synthesis. Never raises."""
    try:
        payload: Dict[str, Any] = {
            "trace": trace,
            "visited_keys": sorted(visited_keys),
            "llm_usage_totals": usage_totals,
        }
        if extra:
            payload.update(extra)
        (session_dir / "state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _acquire_children_concurrent(
    jobs: List[Dict[str, Any]],
    conn_lib: Any,
    *,
    max_workers: int,
) -> List[Dict[str, Any]]:
    """Resolve + acquire PDFs for a list of follow-up refs concurrently.

    Each ``job`` carries ``ref_line``, ``bibcode`` (optional), ``dest``. We do
    the network-bound work (catalog parse + ``acquire_pdf``) in a thread pool
    and return per-job ``(child_path, note, sref, opened_via)``. The DB connection
    is only used for read helpers — these are safe across threads when using
    WAL + busy_timeout (see ``connect()``) for the small selects involved.
    """
    if not jobs:
        return []
    n = max(1, min(max_workers, 8))
    from research_library.library import db as library_db

    def _resolve(job: Dict[str, Any]) -> Dict[str, Any]:
        ref_line = job["ref_line"]
        bc = job.get("bibcode")
        dest = job["dest"]
        opened_via = "acquired"
        try:
            if bc:
                local_pdf = library_db.find_local_pdf_path(conn_lib, bibcode=bc)
                sref = parse_catalog_line(bc, conn_lib, use_ads=True)
                if local_pdf:
                    return {
                        **job,
                        "child_path": local_pdf,
                        "note": "library_local_pdf",
                        "sref": sref,
                        "opened_via": "library_local_pdf",
                    }
                child_path, note = acquire_pdf(sref, conn_lib, dest)
            else:
                sref = _standard_ref_for_reference_line(ref_line, conn_lib)
                child_path, note = acquire_pdf(sref, conn_lib, dest)
            return {
                **job,
                "child_path": child_path,
                "note": note,
                "sref": sref,
                "opened_via": opened_via,
            }
        except Exception as e:
            return {
                **job,
                "child_path": None,
                "note": f"acquire_error:{e}",
                "sref": None,
                "opened_via": opened_via,
            }

    if n == 1 or len(jobs) == 1:
        return [_resolve(j) for j in jobs]
    from concurrent.futures import ThreadPoolExecutor

    out: List[Optional[Dict[str, Any]]] = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = {ex.submit(_resolve, jobs[i]): i for i in range(len(jobs))}
        for fut in futures:
            i = futures[fut]
            try:
                out[i] = fut.result()
            except Exception as e:
                out[i] = {
                    **jobs[i],
                    "child_path": None,
                    "note": f"acquire_thread_error:{e}",
                    "sref": None,
                    "opened_via": "acquired",
                }
    return [o for o in out if o is not None]


def analyze_pdf_reference_chain(
    pdf_path: str,
    question: str,
    *,
    client: Optional[ChatClient] = None,
    provider: Optional[str] = None,
    max_hops: int = 2,
    max_chars_per_pdf: Optional[int] = None,
    max_step_tokens: Optional[int] = None,
    max_synth_tokens: Optional[int] = None,
    cache_dir: Optional[str] = None,
    persist_library: bool = True,
    use_library_references: Optional[bool] = None,
) -> Dict[str, Any]:
    """BFS follow references: LLM picks ref numbers, fetch PDFs, repeat up to max_hops deep."""
    load_env()
    if not (question or "").strip():
        raise ValueError("question is required for reference chain analysis")

    if use_library_references is None:
        raw_lr = (os.environ.get("RESEARCH_PDF_CHAIN_LIBRARY_REFS") or "").strip().lower()
        use_library_references = raw_lr not in ("0", "false", "no", "off")

    cap = _default_max_completion_tokens()
    if max_step_tokens is None:
        raw_st = os.environ.get("RESEARCH_PDF_CHAIN_MAX_STEP_TOKENS", "").strip()
        try:
            max_step_tokens = int(raw_st) if raw_st else cap
        except ValueError:
            max_step_tokens = cap
    if max_synth_tokens is None:
        raw_sy = os.environ.get("RESEARCH_PDF_CHAIN_MAX_SYNTH_TOKENS", "").strip()
        try:
            max_synth_tokens = int(raw_sy) if raw_sy else 65536
        except ValueError:
            max_synth_tokens = 65536

    path = os.path.abspath(os.path.expanduser(pdf_path))
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    lim = max_chars_per_pdf
    if lim is None:
        lim = int(os.environ.get("RESEARCH_PDF_ANALYZE_MAX_CHARS") or "100000")

    llm = client or get_chat_client(provider)

    base_cache = cache_dir or str(get_data_dir() / "pdf_chain_cache")
    Path(base_cache).mkdir(parents=True, exist_ok=True)
    session_dir = Path(base_cache) / hashlib.sha256(path.encode()).hexdigest()[:16]
    session_dir.mkdir(parents=True, exist_ok=True)

    visited_keys: Set[str] = set()
    trace: List[Dict[str, Any]] = []
    queue: List[Tuple[int, str, str, str, Optional[str], str]] = [
        (0, path, "root", "", None, "")
    ]
    seen_jobs: Set[Tuple[int, str]] = set()
    library_ingest_by_pdf: Dict[str, Dict[str, Any]] = {}

    def _chain_env_int(key: str, default: int) -> int:
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def _chain_env_truthy(key: str, *, default: bool = False) -> bool:
        raw = (os.environ.get(key) or "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on")

    narrow_above = _chain_env_int("RESEARCH_PDF_CHAIN_REF_NARROW_ABOVE", 60)
    ref_prompt_max = _chain_env_int("RESEARCH_PDF_CHAIN_REF_PROMPT_MAX", 80)
    narrow_must_pick = _chain_env_int("RESEARCH_PDF_CHAIN_REF_MUST_PICK", 12)
    tight_follow = _chain_env_truthy("RESEARCH_PDF_CHAIN_TIGHT", default=False)
    max_follow_per_hop = _chain_env_int("RESEARCH_PDF_CHAIN_MAX_FOLLOW_PER_HOP", 0)
    retrieval_max_chars = _chain_env_int("RESEARCH_PDF_CHAIN_RETRIEVAL_MAX_CHARS", 16000)
    retrieval_max_chunks = _chain_env_int("RESEARCH_PDF_CHAIN_RETRIEVAL_MAX_CHUNKS", 10)
    step_body_max = _chain_env_int("RESEARCH_PDF_CHAIN_STEP_BODY_MAX_CHARS", 18000)
    step_split = _chain_env_truthy("RESEARCH_PDF_CHAIN_STEP_SPLIT", default=True)
    pass1_body_cap = _chain_env_int("RESEARCH_PDF_CHAIN_STEP_PASS1_BODY_CHARS", 10000)
    pass1_tokens_cap = _chain_env_int("RESEARCH_PDF_CHAIN_STEP_PASS1_MAX_TOKENS", 16384)
    pass2_tokens_cfg = _chain_env_int("RESEARCH_PDF_CHAIN_STEP_PASS2_MAX_TOKENS", 0)
    match_body_cap = _chain_env_int("RESEARCH_PDF_CHAIN_MATCH_BODY_MAX_CHARS", 120000)
    total_token_budget = _chain_env_int("RESEARCH_PDF_CHAIN_TOTAL_TOKEN_BUDGET", 0)
    acquire_workers = max(
        1, _chain_env_int("RESEARCH_PDF_CHAIN_ACQUIRE_WORKERS", 4)
    )

    from research_library.library import db as library_db
    from research_library.library.semantic import retrieve_context_for_paper_question

    conn_lib = library_db.connect()

    usage_rows: List[Dict[str, Any]] = []
    budget_exhausted = False
    while queue:
        if total_token_budget > 0 and not budget_exhausted:
            cur_totals = _sum_llm_usage(usage_rows)
            if int(cur_totals.get("total_tokens") or 0) >= total_token_budget:
                budget_exhausted = True
                trace.append({
                    "depth": -1,
                    "label": "budget_exhausted",
                    "note": "RESEARCH_PDF_CHAIN_TOTAL_TOKEN_BUDGET reached; stopping further hops.",
                    "tokens_used": int(cur_totals.get("total_tokens") or 0),
                    "budget": total_token_budget,
                })
                break
        depth, pth, label, parent, pdf_opened_via, ctx_boost = queue.pop(0)
        key_job = (depth, pth)
        if key_job in seen_jobs:
            continue
        seen_jobs.add(key_job)

        ap_node = os.path.abspath(pth)
        lib_meta = library_ingest_by_pdf.pop(ap_node, None)

        full_text = extract_pdf_text(pth)
        if not full_text.strip():
            row: Dict[str, Any] = {
                "depth": depth,
                "label": label,
                "parent": parent or None,
                "pdf_path": pth,
                "error": "empty_text",
            }
            if lib_meta is not None:
                row["library_ingest"] = lib_meta
            if pdf_opened_via:
                row["pdf_opened_via"] = pdf_opened_via
            trace.append(row)
            continue

        chain_auto_ingest: Optional[Dict[str, Any]] = None
        lib_lookup_path = ap_node
        lib_pid = library_db.paper_id_for_absolute_pdf(conn_lib, lib_lookup_path)

        if (
            use_library_references
            and persist_library
            and lib_pid is None
            and _chain_env_truthy("RESEARCH_PDF_CHAIN_AUTO_INGEST", default=True)
            and (os.environ.get("ADS_API_TOKEN") or "").strip()
        ):
            from research_library.library.pdf_ingest import ingest_pdf_file

            try:
                ing = ingest_pdf_file(
                    conn_lib,
                    ap_node,
                    dry_run=False,
                    require_strong_id=False,
                    copy_to_pdfs=True,
                    symlink_to_pdfs=False,
                    source="pdf_reference_chain_auto_ingest",
                )
                chain_auto_ingest = ing
                if ing.get("ok"):
                    conn_lib.commit()
                    lib_lookup_path = os.path.abspath(
                        str(
                            ing.get("pdf_path_stored")
                            or ing.get("pdf_path_input")
                            or ap_node
                        )
                    )
                    lib_pid = library_db.paper_id_for_absolute_pdf(
                        conn_lib, lib_lookup_path
                    )
                else:
                    conn_lib.rollback()
            except Exception as e:
                chain_auto_ingest = {"ok": False, "error": str(e)}
                conn_lib.rollback()

        ref_section = _find_references_section(full_text)
        ref_map: Dict[int, str] = {}
        ref_num_to_bib: Dict[int, str] = {}
        ref_parse_mode = "empty"

        if use_library_references and lib_pid is not None:
            edges = library_db.list_paper_reference_edges(conn_lib, lib_pid)
            if (
                not edges
                and persist_library
                and (os.environ.get("ADS_API_TOKEN") or "").strip()
            ):
                from research_library.library.citations import sync_references_from_ads

                sync_attempts = max(
                    1, _chain_env_int("RESEARCH_PDF_CHAIN_SYNC_RETRY_ATTEMPTS", 3)
                )
                sync_sleep = max(
                    0.0, float(_chain_env_int("RESEARCH_PDF_CHAIN_SYNC_SLEEP_MS", 500)) / 1000.0
                )
                last_err: Optional[str] = None
                for attempt in range(sync_attempts):
                    try:
                        sync_references_from_ads(
                            conn_lib,
                            paper_ids={lib_pid},
                            resolve_arxiv_bibcodes=True,
                            sleep_s=sync_sleep,
                        )
                        last_err = None
                        break
                    except Exception as e:
                        last_err = str(e)
                        if attempt == sync_attempts - 1:
                            break
                        # exponential backoff for transient ADS issues
                        import time as _time

                        _time.sleep(min(2 ** attempt * 0.5, 8.0))
                if last_err is not None:
                    log_event(
                        "chain.sync_references_failed",
                        paper_id=lib_pid,
                        error=last_err,
                        attempts=sync_attempts,
                    )
                edges = library_db.list_paper_reference_edges(conn_lib, lib_pid)
            if edges:
                ref_map, ref_num_to_bib, ref_parse_mode = _ref_map_from_library_edges(
                    edges
                )

        if not ref_map and not use_library_references:
            ref_num_to_bib = {}
            ref_map, ref_parse_mode = _build_ref_map(ref_section)

        narrow_hay = " ".join(
            p
            for p in (
                question.strip(),
                (ctx_boost or "").strip(),
                full_text[:12000] if full_text else "",
            )
            if p
        )
        prompt_map, prompt_num_to_orig = _narrow_ref_map_for_llm(
            ref_map,
            narrow_hay,
            trigger_above=narrow_above,
            max_in_prompt=ref_prompt_max,
            must_pick_refs=narrow_must_pick,
        )
        ref_prompt = _ref_map_for_prompt(prompt_map, limit=ref_prompt_max)
        ref_prompt_narrowed = len(prompt_map) < len(ref_map)

        body_source = "truncated_fulltext"
        body_raw = ""
        truncated = False
        retrieval_query = _chain_retrieval_query(
            question.strip(), ctx_boost or "", cap=6000
        )
        if (
            lib_pid is not None
            and library_db.paper_chunk_count(conn_lib, lib_pid) > 0
            and (question or "").strip()
        ):
            ctx, ctag = retrieve_context_for_paper_question(
                conn_lib,
                lib_pid,
                retrieval_query.strip(),
                max_chars=min(lim, retrieval_max_chars),
                max_chunks=retrieval_max_chunks,
            )
            if ctag and ctx.strip():
                body_raw = ctx
                body_source = ctag
        if not body_raw:
            body_raw, truncated = truncate_text(full_text, lim)

        body_llm, extra_trunc = truncate_text(body_raw, step_body_max)
        if extra_trunc:
            truncated = True
        body_for_match = body_raw
        if len(body_for_match) > match_body_cap:
            body_for_match, _ = truncate_text(body_for_match, match_body_cap)

        if step_split:
            b_follow, _ = truncate_text(body_llm, min(len(body_llm), pass1_body_cap))
            t_follow = min(max_step_tokens, max(256, pass1_tokens_cap))
            s_follow = _llm_chain_step_follow_only(
                llm,
                paper_body=b_follow,
                question=question.strip(),
                ref_index_text=ref_prompt,
                max_tokens=t_follow,
            )
            uc_f = _usage_row(llm, f"chain_step_depth_{depth}_follow")
            if uc_f:
                usage_rows.append(uc_f)
            nums_prompt = list(s_follow.get("follow_ref_numbers") or [])
            t_ex = max_step_tokens
            if pass2_tokens_cfg > 0:
                t_ex = min(max_step_tokens, pass2_tokens_cfg)
            if nums_prompt:
                s_ex = _llm_chain_step_excerpts(
                    llm,
                    paper_body=body_llm,
                    question=question.strip(),
                    ref_index_text=ref_prompt,
                    locked_ref_numbers=nums_prompt,
                    max_tokens=t_ex,
                )
                uc_e = _usage_row(llm, f"chain_step_depth_{depth}_excerpts")
                if uc_e:
                    usage_rows.append(uc_e)
            else:
                s_ex = {"excerpts": [], "rationale": ""}
            r1 = str(s_follow.get("rationale") or "").strip()
            r2 = str(s_ex.get("rationale") or "").strip()
            rationale_merged = f"{r1}\n{r2}".strip() if r1 or r2 else r1 or r2
            step = {
                "excerpts": s_ex.get("excerpts") or [],
                "follow_ref_numbers": nums_prompt,
                "rationale": rationale_merged,
            }
        else:
            step = _llm_chain_step(
                llm,
                paper_body=body_llm,
                question=question.strip(),
                ref_index_text=ref_prompt,
                max_tokens=max_step_tokens,
            )
            uc = _usage_row(llm, f"chain_step_depth_{depth}")
            if uc:
                usage_rows.append(uc)

        grounding_verify = (
            os.environ.get("RESEARCH_PDF_CHAIN_VERIFY_EXCERPTS") or "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        grounding_drops: List[Dict[str, Any]] = []
        if grounding_verify and step.get("excerpts"):
            verified, dropped = _verify_excerpts(step.get("excerpts") or [], body_raw)
            if dropped:
                grounding_drops = dropped
            # Even if some excerpts fail verification, keep the verified ones; if
            # everything failed we leave the list empty (caller / synth still has
            # the rationale + follow-up refs to work with).
            step = dict(step)
            step["excerpts"] = verified

        prompt_valid = set(prompt_map.keys())
        model_follow_raw = list(step.get("follow_ref_numbers") or [])
        model_follow = sorted(
            {
                prompt_num_to_orig[n]
                for n in model_follow_raw
                if n in prompt_valid and prompt_num_to_orig.get(n) in ref_map
            }
        )
        try:
            cap_match = max(1, int(os.environ.get("RESEARCH_PDF_CHAIN_BODY_MATCH_MAX", "8") or "8"))
        except ValueError:
            cap_match = 8
        citation_follow = _refs_matching_haystacks(
            ref_map,
            [
                body_for_match,
                question.strip(),
                (ctx_boost or "").strip(),
                *(step.get("excerpts") or []),
            ],
            max_refs=cap_match,
        )
        method_nums: List[int] = []
        if (os.environ.get("RESEARCH_PDF_CHAIN_METHOD_HINTS") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            from research_library.analysis.ref_method_hints import method_hint_ref_numbers

            method_nums = sorted(method_hint_ref_numbers(full_text, ref_map))
        follow = _chain_follow_combine(
            model_follow, citation_follow, method_nums, tight=tight_follow
        )
        if max_follow_per_hop > 0:
            follow = follow[:max_follow_per_hop]
        parts: List[str] = []
        if model_follow:
            parts.append("model")
        if citation_follow:
            parts.append("citation_match")
        if method_nums:
            parts.append("method_hints")
        follow_source = "+".join(parts) if parts else "none"

        row = {
            "depth": depth,
            "label": label,
            "parent": parent or None,
            "pdf_path": pth,
            "truncated": truncated,
            "library_paper_id": lib_pid,
            "chain_body_source": body_source,
            "ref_parse_mode": ref_parse_mode,
            "ref_list_size": len(ref_map),
            "ref_prompt_size": len(prompt_map),
            "ref_prompt_narrowed": ref_prompt_narrowed,
            "chain_tight_follow": tight_follow,
            "chain_retrieval_query_chars": len(retrieval_query),
            "chain_ctx_boost_chars": len(ctx_boost or ""),
            "chain_llm_split": step_split,
            "chain_step_body_chars": len(body_llm),
            "chain_body_raw_chars": len(body_raw),
            "chain_retrieval_cap_chars": retrieval_max_chars,
            "chain_retrieval_cap_chunks": retrieval_max_chunks,
            "excerpts": step["excerpts"],
            "follow_ref_numbers": follow,
            "follow_ref_numbers_model_prompt": model_follow_raw,
            "follow_ref_numbers_model": model_follow,
            "follow_ref_numbers_citation_match": citation_follow,
            "follow_ref_numbers_method_hints": method_nums,
            "follow_ref_source": follow_source,
            "rationale": step.get("rationale"),
        }
        if grounding_drops:
            row["grounding_dropped_excerpts"] = grounding_drops
        if step.get("parse_error"):
            row["llm_parse_error"] = True
        lu = getattr(llm, "last_usage", None)
        if lu:
            row["llm_usage"] = dict(lu)
        if lib_meta is not None:
            row["library_ingest"] = lib_meta
        if chain_auto_ingest is not None:
            row["chain_auto_library_ingest"] = chain_auto_ingest
        if pdf_opened_via:
            row["pdf_opened_via"] = pdf_opened_via
        trace.append(row)

        # Periodic best-effort state snapshot so kills don't lose trace data.
        _write_session_state(
            session_dir,
            trace=trace,
            visited_keys=visited_keys,
            usage_totals=_sum_llm_usage(usage_rows),
        )

        if depth >= max_hops or not follow:
            continue

        # Batch the per-follow jobs so we can parallelize the network-bound
        # ``acquire_pdf`` calls. We must still de-dup against ``visited_keys``
        # serially before dispatching.
        pending_jobs: List[Dict[str, Any]] = []
        for n in follow:
            ref_line = ref_map[n]
            bc = ref_num_to_bib.get(n)
            if bc:
                vk = f"bibcode:{bc.strip().lower()}"
            else:
                vk = _ref_visit_key(ref_line)
            if vk in visited_keys:
                continue
            visited_keys.add(vk)
            stem = re.sub(r"[^\w\-.]", "_", f"d{depth + 1}_ref{n}_{vk}")[:120]
            dest = str(session_dir / f"{stem}.pdf")
            pending_jobs.append({
                "n": n,
                "ref_line": ref_line,
                "bibcode": bc,
                "dest": dest,
                "visit_key": vk,
            })

        results = _acquire_children_concurrent(
            pending_jobs, conn_lib, max_workers=acquire_workers
        )

        for res in results:
            n = res["n"]
            ref_line = res["ref_line"]
            child_path = res.get("child_path")
            note = res.get("note") or ""
            sref = res.get("sref")
            opened_via = res.get("opened_via") or "acquired"

            if not child_path or sref is None:
                trace.append(
                    {
                        "depth": depth + 1,
                        "label": f"ref[{n}]",
                        "parent": label,
                        "ref_line": _short_label(ref_line, 400),
                        "unresolved": True,
                        "reason": note,
                        "ref_resolution_note": getattr(sref, "resolution_note", None),
                        "ref_bibcode": getattr(sref, "bibcode", None),
                        "ref_arxiv_id": getattr(sref, "arxiv_id", None),
                        "ref_doi": getattr(sref, "doi", None),
                    }
                )
                continue

            if persist_library:
                ap_child = os.path.abspath(child_path)
                try:
                    meta = ingest_downloaded_reference(
                        conn_lib, sref, child_path, acquire_reason=note
                    )
                    idx = _maybe_auto_semantic_index_paper(conn_lib, ap_child)
                    if idx is not None:
                        meta = dict(meta)
                        meta["auto_semantic_index"] = idx
                    library_ingest_by_pdf[ap_child] = meta
                except Exception as e:
                    library_ingest_by_pdf[ap_child] = {"ok": False, "error": str(e)}

            ex_list = step.get("excerpts") or []
            child_boost = "\n\n".join(
                str(e).strip() for e in ex_list[:12] if str(e).strip()
            )
            if len(child_boost) > 8000:
                child_boost = child_boost[:8000]
            queue.append(
                (
                    depth + 1,
                    child_path,
                    f"ref[{n}] from {label}",
                    label,
                    opened_via,
                    child_boost,
                )
            )

    # Final state snapshot before synthesis (so users can inspect even if synth fails)
    _write_session_state(
        session_dir,
        trace=trace,
        visited_keys=visited_keys,
        usage_totals=_sum_llm_usage(usage_rows),
        extra={"budget_exhausted": budget_exhausted},
    )

    md = _synthesize_chain_markdown(
        llm,
        question=question.strip(),
        trace=trace,
        max_tokens=max_synth_tokens,
    )
    us = _usage_row(llm, "chain_synthesize")
    if us:
        usage_rows.append(us)

    lib_ok = sum(
        1
        for t in trace
        if isinstance(t.get("library_ingest"), dict) and t["library_ingest"].get("ok")
    )

    return {
        "pdf_path": path,
        "question": question.strip(),
        "max_hops": max_hops,
        "markdown_report": md,
        "trace": trace,
        "library_ingested_ok": lib_ok,
        "persist_library": bool(persist_library),
        "use_library_references": bool(use_library_references),
        "llm_usage": usage_rows,
        "llm_usage_totals": _sum_llm_usage(usage_rows),
        "budget_exhausted": budget_exhausted,
        "session_dir": str(session_dir),
    }


def main(argv: Optional[list[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(prog="research-lib pdf-analyze")
    p.add_argument("pdf_path", help="Path to PDF")
    p.add_argument("--question", "-q", default="", help="Optional question for targeted excerpts")
    p.add_argument(
        "--reference-chain",
        action="store_true",
        help="Follow references up to --max-hops with LLM; emit markdown_report",
    )
    p.add_argument("--max-hops", type=int, default=2, help="Max reference depth (default 2: seed + one child hop)")
    p.add_argument(
        "--no-persist-library",
        action="store_true",
        help="Do not upsert downloaded ref PDFs into library.db",
    )
    p.add_argument(
        "--no-library-refs",
        action="store_true",
        help="Do not use paper_references; parse ref list from PDF only",
    )
    p.add_argument("--json", action="store_true", help="Print JSON to stdout")
    p.add_argument("--max-chars", type=int, default=None, help="Max chars sent to LLM (default env)")
    p.add_argument(
        "--max-step-tokens",
        type=int,
        default=None,
        help="Max completion tokens per chain step (JSON); default from RESEARCH_LLM_MAX_COMPLETION_TOKENS / RESEARCH_PDF_CHAIN_MAX_STEP_TOKENS",
    )
    p.add_argument(
        "--max-synth-tokens",
        type=int,
        default=None,
        help="Max completion tokens for final markdown; default 65536 or RESEARCH_PDF_CHAIN_MAX_SYNTH_TOKENS",
    )
    p.add_argument("--provider", default=None, help="RESEARCH_LLM_PROVIDER override (e.g. minimax)")
    args = p.parse_args(argv)

    try:
        if args.reference_chain:
            if not (args.question or "").strip():
                print("Error: --reference-chain requires --question", file=sys.stderr)
                return 2
            step_tok = args.max_step_tokens
            if step_tok is None:
                raw_s = os.environ.get("RESEARCH_PDF_CHAIN_MAX_STEP_TOKENS", "").strip()
                try:
                    step_tok = int(raw_s) if raw_s else _default_max_completion_tokens()
                except ValueError:
                    step_tok = _default_max_completion_tokens()
            synth_tok = args.max_synth_tokens
            if synth_tok is None:
                raw_y = os.environ.get("RESEARCH_PDF_CHAIN_MAX_SYNTH_TOKENS", "").strip()
                try:
                    synth_tok = int(raw_y) if raw_y else 65536
                except ValueError:
                    synth_tok = 65536
            use_lr = False if args.no_library_refs else None
            out = analyze_pdf_reference_chain(
                args.pdf_path,
                args.question,
                provider=args.provider,
                max_hops=args.max_hops,
                max_chars_per_pdf=args.max_chars,
                max_step_tokens=step_tok,
                max_synth_tokens=synth_tok,
                persist_library=not getattr(args, "no_persist_library", False),
                use_library_references=use_lr,
            )
        else:
            out = analyze_pdf(
                args.pdf_path,
                question=args.question or None,
                provider=args.provider,
                max_chars=args.max_chars,
            )
    except LLMError as e:
        print(str(e), file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"File not found: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if args.reference_chain:
            print(out.get("markdown_report") or "")
        else:
            print(out["summary"])
            print()
            if out.get("question_focused"):
                print("— 与问题相关的部分 —")
                print(out["question_focused"])
    totals = out.get("llm_usage_totals") or {}
    if totals and sum(int(totals.get(k) or 0) for k in totals):
        print(
            "[llm_usage_totals]",
            json.dumps(totals, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
