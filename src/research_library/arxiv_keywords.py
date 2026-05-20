#!/usr/bin/env python3
"""
arxiv_keyword_monitor.py
快速扫描 arxiv 新文，匹配用户关键字列表
用法:
    python3 arxiv_keywords.py [astro-ph.GA|astro-ph.CO|astro-ph.SR|all]
    python3 arxiv_keywords.py --clear-cache   # 清空缓存
    python3 arxiv_keywords.py --stats         # 显示缓存统计
    python3 arxiv_keywords.py all --days=365 --max-results=500 --max-pages=0   # 回填（每分区多页，0=自动直到日期/API 尽头，最多 250 页/区）
    python3 arxiv_keywords.py astro-ph.GA --days=120 --max-pages=20             # 单区分段拉取
"""

import sys
import json
import os
import random
import time
import urllib.request
import urllib.error
import http.client
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# arXiv API: leave a few seconds between requests (export.arxiv.org/help/api).
_ARXIV_INTER_REQUEST_SEC = (3.0, 7.0)
# When max_pages_per_category=0, cap pagination to avoid runaway.
_MAX_PAGES_SAFETY = 250

# ========== 用户配置 ==========
# 用户关键字（中英文）
KEYWORDS = {
    "globular cluster": "球状星团",
    "dwarf galaxy": "矮星系",
    "dwarf galaxies": "矮星系",
    "stellar stream": "星流",
    "near-field cosmology": "近场宇宙学",
    "galactic archaeology": "星系考古学",
    "open cluster": "疏散星团",
    "stellar population": "星族",
    "球状星团": "球状星团",
    "矮星系": "矮星系",
    "星流": "星流",
    "近场宇宙学": "近场宇宙学",
    "星系考古学": "星系考古学",
    "疏散星团": "疏散星团",
    "星族": "星族",
}

CATEGORIES = {
    "astro-ph.GA": "星系的星系",
    "astro-ph.CO": "宇宙学与河外天体",
    "astro-ph.SR": "太阳与恒星",
    "astro-ph.IM": "仪器与方法",
    "all": "全部",
}

# ========== 缓存配置 ==========

def _cache_file() -> Path:
    from research_library.config import get_index_dir

    return get_index_dir() / "arxiv_cache.json"


CACHE_MAX_ENTRIES = 20000  # 缓存条目上限（防止无限膨胀）

# ========== 缓存读写 ==========

def load_cache():
    """加载本地缓存，返回 {arxiv_id: entry_dict}"""
    cache_file = _cache_file()
    if not cache_file.exists():
        return {}
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", {})
    except (json.JSONDecodeError, IOError):
        return {}


def save_cache(entries_dict):
    """保存 entries 到缓存文件，保留 last_updated"""
    # 按时间排序，保留最新的 CACHE_MAX_ENTRIES 条
    sorted_entries = sorted(
        entries_dict.items(),
        key=lambda x: x[1].get("published", ""),
        reverse=True
    )
    # 保留最新条目
    trimmed = dict(sorted_entries[:CACHE_MAX_ENTRIES])
    data = {
        "entries": trimmed,
        "last_updated": datetime.now().isoformat(),
    }
    cache_file = _cache_file()
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cache_stats():
    """返回缓存统计信息"""
    entries = load_cache()
    if not entries:
        return "缓存为空"
    dates = [e["published"] for e in entries.values() if e.get("published")]
    cats = {}
    for e in entries.values():
        for c in e.get("categories", []):
            cats[c] = cats.get(c, 0) + 1
    kws = {}
    for e in entries.values():
        for k in e.get("matched_kw", []):
            kws[k] = kws.get(k, 0) + 1
    lines = [
        f"缓存条目总数: {len(entries)}",
        f"最后更新: {cache_file.stat().st_mtime}",
        f"日期范围: {min(dates)} ~ {max(dates)}",
        f"分类分布: {cats}",
        f"关键词分布: {kws}",
    ]
    return "\n".join(lines)


def clear_cache():
    cache_file = _cache_file()
    if cache_file.exists():
        cache_file.unlink()
    print("缓存已清空")

# ========== 核心逻辑 ==========

def fetch_arxiv(
    category: str,
    max_results: int = 500,
    start: int = 0,
    timeout: int = 180,
    retries: int = 4,
) -> str:
    url = (
        f"https://export.arxiv.org/api/query"
        f"?search_query=cat:{category}&sortBy=submittedDate&sortOrder=descending"
        f"&start={start}&max_results={max_results}"
    )
    last_err: BaseException | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": "python3/arxiv_keyword_monitor"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except http.client.IncompleteRead as e:
            last_err = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt + 1 < retries:
            delay = 2.0 + random.uniform(1.0, 4.0)
            print(
                f"[WARN] arXiv fetch retry {attempt + 1}/{retries - 1} in {delay:.1f}s: {last_err}",
                file=sys.stderr,
            )
            time.sleep(delay)
    assert last_err is not None
    raise last_err


def parse_arxiv_xml(xml_text):
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = []
    for entry in root.findall("atom:entry", ns):
        title = entry.find("atom:title", ns)
        summary = entry.find("atom:summary", ns)
        published = entry.find("atom:published", ns)
        link = entry.find("atom:id", ns)
        authors = entry.findall("atom:author/atom:name", ns)
        entries.append({
            "title": title.text.strip().replace("\n", " ") if title is not None else "",
            "summary": summary.text.strip().replace("\n", " ") if summary is not None else "",
            "published": published.text[:10] if published is not None else "",
            "id": link.text.strip().split("/")[-1] if link is not None else "",
            "authors": [a.text for a in authors][:3],
        })
    return entries


def match_keywords(text):
    text_lower = text.lower()
    matched = []
    for kw in KEYWORDS:
        if kw.lower() in text_lower:
            matched.append(KEYWORDS[kw])
    return matched


def run(
    category: str = "all",
    days_back: int = 365,
    cache_enabled: bool = True,
    persist_db: bool = True,
    max_results: int = 500,
    max_pages_per_category: int = 1,
):
    cache = load_cache() if cache_enabled else {}
    cached_ids = set(cache.keys())

    cats = [c for c in CATEGORIES if c != "all"] if category == "all" else [category]
    cutoff = datetime.now() - timedelta(days=days_back)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    page_cap = _MAX_PAGES_SAFETY if max_pages_per_category == 0 else max(1, max_pages_per_category)

    results = []
    new_entries = {}   # 本次新出现的条目（用于更新缓存）
    any_request = False

    for cat in cats:
        start = 0
        for page_idx in range(page_cap):
            if any_request:
                delay = random.uniform(*_ARXIV_INTER_REQUEST_SEC)
                print(f"[INFO] Sleeping {delay:.1f}s before next arXiv request...", file=sys.stderr)
                time.sleep(delay)
            any_request = True
            print(
                f"[INFO] Fetching {cat} start={start} (page {page_idx + 1} of up to {page_cap})...",
                file=sys.stderr,
            )
            try:
                xml = fetch_arxiv(cat, max_results=max_results, start=start)
            except Exception as e:
                print(f"[ERROR] Failed to fetch {cat}: {e}", file=sys.stderr)
                break
            entries = parse_arxiv_xml(xml)
            if not entries:
                break
            for entry in entries:
                if entry["published"] < cutoff_str:
                    continue
                text = entry["title"] + " " + entry["summary"]
                matched = match_keywords(text)
                if matched:
                    arxiv_id = entry["id"]
                    is_new = arxiv_id not in cached_ids
                    results.append((entry, matched, cat, is_new))
                    if arxiv_id not in new_entries:
                        new_entries[arxiv_id] = {
                            "title": entry["title"],
                            "summary": entry["summary"],
                            "published": entry["published"],
                            "id": arxiv_id,
                            "authors": entry["authors"],
                            "categories": [cat],
                            "matched_kw": matched,
                            "cached_at": datetime.now().isoformat(),
                        }
                    else:
                        new_entries[arxiv_id]["categories"].append(cat)
                        new_entries[arxiv_id]["matched_kw"] = list(
                            set(new_entries[arxiv_id]["matched_kw"] + matched)
                        )
            oldest = entries[-1]["published"]
            if oldest < cutoff_str or len(entries) < max_results:
                break
            start += max_results

    results.sort(key=lambda x: x[0]["published"], reverse=True)

    if not results:
        print("NO_REPLY")
        return

    # 更新缓存
    if cache_enabled and new_entries:
        # 合并：保留旧条目中未出现的新条目
        merged = dict(cache)
        for k, v in new_entries.items():
            merged[k] = v   # 新条目覆盖旧条目（更新内容）
        save_cache(merged)
        new_count = len(new_entries)
        print(f"[INFO] 缓存已更新，新增 {new_count} 条", file=sys.stderr)

    if persist_db and new_entries:
        try:
            from research_library.library import db

            conn = db.connect()
            db.ensure_schema(conn)
            n_db = 0
            for rec in new_entries.values():
                rec = dict(rec)
                rec.setdefault("source", "arxiv_keyword_scan")
                try:
                    db.upsert_from_arxiv_cache_entry(conn, rec)
                    n_db += 1
                except Exception as ex:
                    print(f"[library.db] upsert skip {rec.get('id')}: {ex}", file=sys.stderr)
            print(f"[INFO] 本地库已写入 {n_db} 条 (library.db)", file=sys.stderr)
        except Exception as ex:
            print(f"[library.db] batch failed: {ex}", file=sys.stderr)

    # 输出
    total = len(results)
    new_num = sum(1 for _, _, _, is_new in results if is_new)
    print(f"\n🎯 共匹配 {total} 篇（近 {days_back} 天）", file=sys.stderr)
    if new_num > 0:
        print(f"🆕 其中 {new_num} 篇为新增（未在缓存中）", file=sys.stderr)
    print(file=sys.stderr)

    for entry, matched_kws, cat, is_new in results:
        cat_label = CATEGORIES.get(cat, cat)
        arxiv_id = entry["id"]
        title = entry["title"]
        authors = ", ".join(entry["authors"])
        new_marker = " 🆕" if is_new else ""
        print(f"📄 [{cat_label}] {arxiv_id}{new_marker}")
        print(f"   {title}")
        print(f"   作者: {authors}")
        print(f"   关键词: {', '.join(matched_kws)}")
        print(f"   https://arxiv.org/abs/{arxiv_id}")
        print()


if __name__ == "__main__":
    if "--clear-cache" in sys.argv:
        clear_cache()
        sys.exit(0)
    if "--stats" in sys.argv:
        print(cache_stats())
        sys.exit(0)

    cat = "all"
    days = 365
    persist_db = True
    max_r = 500
    max_pages = 1
    for a in sys.argv[1:]:
        if a in CATEGORIES:
            cat = a
        elif a.startswith("--days="):
            days = int(a.split("=", 1)[1])
        elif a.startswith("--max-results="):
            max_r = int(a.split("=", 1)[1])
        elif a.startswith("--max-pages="):
            max_pages = int(a.split("=", 1)[1])
        elif a == "--no-persist-db":
            persist_db = False
    run(
        category=cat,
        days_back=days,
        persist_db=persist_db,
        max_results=max_r,
        max_pages_per_category=max_pages,
    )
