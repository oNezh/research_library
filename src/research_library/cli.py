"""CLI entry for research-library."""

from __future__ import annotations

import argparse
import sys


def arxiv_keywords_run() -> None:
    """Delegate to arxiv_keywords main flow."""
    from research_library import arxiv_keywords as ak

    if "--clear-cache" in sys.argv:
        ak.clear_cache()
        return
    if "--stats" in sys.argv:
        print(ak.cache_stats())
        return

    cat = "all"
    days = 365
    persist_db = True
    for a in sys.argv[1:]:
        if a in ak.CATEGORIES:
            cat = a
        elif a.startswith("--days="):
            days = int(a.split("=", 1)[1])
        elif a == "--no-persist-db":
            persist_db = False
    ak.run(category=cat, days_back=days, persist_db=persist_db)


def main() -> None:
    from research_library.config import load_env

    load_env()
    parser = argparse.ArgumentParser(prog="research-lib")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("lookup", help="ADS/arXiv search, bibtex, download, pdf2ads").add_argument(
        "lookup_args", nargs=argparse.REMAINDER, help="e.g. ref --text '...' --json"
    )

    p_ads = sub.add_parser("ads-data-products", help="ADS data products + catalog scan")
    p_ads.add_argument("ads_args", nargs=argparse.REMAINDER)

    p_ax = sub.add_parser("arxiv-keywords", help="arXiv keyword monitor")
    p_ax.add_argument("arxiv_args", nargs=argparse.REMAINDER)

    p_pdf = sub.add_parser("pdf-extract", help="Extract tables/images from PDF (needs pymupdf)")
    p_pdf.add_argument("pdf_args", nargs=argparse.REMAINDER)

    p_pana = sub.add_parser(
        "pdf-analyze",
        help="LLM summary + question-focused excerpts (MiniMax; RESEARCH_LLM_PROVIDER)",
    )
    p_pana.add_argument("pdf_analyze_args", nargs=argparse.REMAINDER)

    p_refs = sub.add_parser("refs-classify", help="Reference list + citation contexts JSON")
    p_refs.add_argument("refs_args", nargs=argparse.REMAINDER)

    p_lib = sub.add_parser("library", help="Local SQLite literature index (FTS5)")
    p_lib.add_argument("library_args", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    if args.command == "lookup":
        from research_library.lookup import main as lookup_main

        ra = getattr(args, "lookup_args", None) or []
        sys.exit(lookup_main(ra))

    if args.command == "ads-data-products":
        from research_library.ads_data_products import main as ads_main

        ads_main(getattr(args, "ads_args", None) or None)
        sys.exit(0)

    if args.command == "arxiv-keywords":
        old = sys.argv
        try:
            sys.argv = ["arxiv_keywords.py"] + (getattr(args, "arxiv_args", None) or [])
            arxiv_keywords_run()
        finally:
            sys.argv = old
        sys.exit(0)

    if args.command == "pdf-extract":
        from research_library.pdf_extract import main as pdf_main

        pdf_main(getattr(args, "pdf_args", None) or None)
        sys.exit(0)

    if args.command == "pdf-analyze":
        from research_library.analysis.pdf import main as pan_main

        sys.exit(pan_main(getattr(args, "pdf_analyze_args", None) or []))

    if args.command == "refs-classify":
        from research_library.refs_classify import main as rc_main

        sys.exit(rc_main(getattr(args, "refs_args", None) or None))

    if args.command == "library":
        from research_library.library.cli import main as library_main

        sys.exit(library_main(getattr(args, "library_args", None) or []))

    sys.exit(1)


if __name__ == "__main__":
    main()
