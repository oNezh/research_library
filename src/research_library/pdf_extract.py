#!/usr/bin/env python3
"""Extract tables and figures from a PDF.

Usage:
    pdf_extract.py <pdf> --list                     # List all tables & images per page
    pdf_extract.py <pdf> --tables [--pages N,M-P]    # Extract tables as markdown
    pdf_extract.py <pdf> --images [--pages N,M-P]    # Extract images to files
    pdf_extract.py <pdf> --all [--pages N,M-P]       # Extract both

Tables are reconstructed from text layout using pdfplumber-style heuristics
(via PyMuPDF). Images are extracted as PNG/JPEG files.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import json
from pathlib import Path

# PyMuPDF
import fitz


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def parse_pages_arg(pages_str: str) -> set[int] | None:
    """Parse '1,3-5,7' → {1,3,4,5,7}"""
    if not pages_str:
        return None
    result = set()
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            result.update(range(int(start.strip()), int(end.strip()) + 1))
        else:
            result.add(int(part))
    return result


def extract_images_from_page(page: fitz.Page, page_num: int) -> list[dict]:
    """Extract all images from a page. Returns list of {num, name, path}."""
    images = []
    image_list = page.get_images(full=True)
    for img_idx, img in enumerate(image_list):
        xref = img[0]
        try:
            base_image = page.parent.extract_image(xref)
            ext = base_image.get("ext", "png")
            img_data = base_image["image"]
            img_bytes = img_data
            width = base_image.get("width", 0)
            height = base_image.get("height", 0)
        except Exception:
            continue

        # Save to current dir or memory
        name = f"page{page_num}_img{img_idx+1}.{ext}"
        images.append({
            "num": img_idx + 1,
            "xref": xref,
            "name": name,
            "ext": ext,
            "width": width,
            "height": height,
            "bytes": len(img_bytes),
        })
    return images


def extract_tables_from_page(page: fitz.Page) -> list[dict]:
    """Extract tables from a page using PyMuPDF's table detection.
    
    Returns list of {num, rows, cols, text} where text is the table
    rendered as tab-separated values.
    """
    tables_data = []
    
    # Use PyMuPDF's built-in table finder
    try:
        tabs = page.find_tables()
    except Exception:
        return []
    
    for tab_idx, tab in enumerate(tabs):
        cells = tab.extract()
        if not cells or len(cells) < 2:
            continue
        
        # Skip very small tables
        n_rows = len(cells)
        n_cols = max(len(row) for row in cells) if cells else 0
        if n_rows < 2 or n_cols < 2:
            continue
        
        # Convert to TSV text
        lines = []
        for row in cells:
            # Clean each cell
            cleaned = []
            for cell in row:
                if cell is None:
                    cell = ""
                # Remove newlines and collapse whitespace
                cell_text = " ".join(str(cell).split())
                cleaned.append(cell_text)
            lines.append("\t".join(cleaned))
        
        tables_data.append({
            "num": tab_idx + 1,
            "rows": n_rows,
            "cols": n_cols,
            "text": "\n".join(lines),
        })
    
    return tables_data


def list_all(pdf_path: str, pages: set[int] | None):
    """List tables and images on each page."""
    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    print(f"PDF: {pdf_path} ({total_pages} pages)", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    
    results = {"pages": {}}
    
    for page_num in range(1, total_pages + 1):
        if pages and page_num not in pages:
            continue
        page = doc[page_num - 1]
        
        images = extract_images_from_page(page, page_num)
        tables = extract_tables_from_page(page)
        
        if images or tables:
            print(f"\n📄 Page {page_num}", file=sys.stderr)
            
            if tables:
                for t in tables:
                    print(f"  📊 Table {t['num']}: {t['rows']} rows × {t['cols']} cols", file=sys.stderr)
                    print(f"     Preview: {t['text'].split(chr(10))[0][:80]}", file=sys.stderr)
            
            if images:
                for img in images:
                    print(f"  🖼️  Image {img['num']}: {img['width']}×{img['height']} px, {img['ext']}, {img['bytes']} bytes", file=sys.stderr)
            
            results["pages"][page_num] = {
                "tables": [{"num": t["num"], "rows": t["rows"], "cols": t["cols"]} for t in tables],
                "images": [{"num": i["num"], "width": i["width"], "height": i["height"], "ext": i["ext"], "bytes": i["bytes"]} for i in images],
            }
    
    doc.close()
    return results


def extract_tables(pdf_path: str, pages: set[int] | None, fmt: str = "md"):
    """Extract tables as markdown or TSV."""
    doc = fitz.open(pdf_path)
    results = []
    
    for page_num in range(1, len(doc) + 1):
        if pages and page_num not in pages:
            continue
        page = doc[page_num - 1]
        tables = extract_tables_from_page(page)
        
        if not tables:
            continue
        
        for t in tables:
            result = {
                "page": page_num,
                "table_num": t["num"],
                "rows": t["rows"],
                "cols": t["cols"],
            }
            if fmt == "md":
                # Convert TSV to markdown table
                lines = t["text"].split("\n")
                if not lines:
                    continue
                # Header
                header = lines[0].split("\t")
                md_lines = [
                    "| " + " | ".join(header) + " |",
                    "| " + " | ".join(["---"] * len(header)) + " |",
                ]
                for row in lines[1:]:
                    cells = row.split("\t")
                    md_lines.append("| " + " | ".join(cells) + " |")
                result["markdown"] = "\n".join(md_lines)
                result["text"] = t["text"]
            else:
                result["tsv"] = t["text"]
            
            results.append(result)
    
    doc.close()
    return results


def extract_images(pdf_path: str, pages: set[int] | None, output_dir: str | None):
    """Extract images from specified pages."""
    doc = fitz.open(pdf_path)
    
    if output_dir:
        out_path = Path(output_dir)
    else:
        pdf_name = Path(pdf_path).stem
        out_path = Path(pdf_name + "_images")
    
    out_path.mkdir(parents=True, exist_ok=True)
    
    results = []
    for page_num in range(1, len(doc) + 1):
        if pages and page_num not in pages:
            continue
        page = doc[page_num - 1]
        images = extract_images_from_page(page, page_num)
        
        for img in images:
            xref = img["xref"]
            try:
                base_image = page.parent.extract_image(xref)
                ext = base_image.get("ext", "png")
                img_bytes = base_image["image"]
                out_name = f"page{page_num}_img{img['num']}.{ext}"
                out_file = out_path / out_name
                with open(out_file, "wb") as f:
                    f.write(img_bytes)
                results.append({
                    "page": page_num,
                    "image_num": img["num"],
                    "name": out_name,
                    "path": str(out_file),
                    "width": img["width"],
                    "height": img["height"],
                    "ext": ext,
                })
                print(f"Saved: {out_file}", file=sys.stderr)
            except Exception as e:
                print(f"  Failed to extract image {img['num']} on page {page_num}: {e}", file=sys.stderr)
    
    doc.close()
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Extract tables and figures from a PDF.")
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--list", action="store_true", help="List all tables and images per page")
    parser.add_argument("--tables", action="store_true", help="Extract tables")
    parser.add_argument("--images", action="store_true", help="Extract images")
    parser.add_argument("--all", action="store_true", help="Extract both tables and images")
    parser.add_argument("--pages", type=str, help="Pages to process, e.g. '1,3-5,7' (default: all)")
    parser.add_argument("--format", choices=["md", "tsv"], default="md", help="Table output format (default: md)")
    parser.add_argument("--output-dir", type=str, help="Output directory for images (default: {pdf_stem}_images/)")
    parser.add_argument("--json", action="store_true", help="Output results as JSON (for programmatic use)")
    
    args = parser.parse_args(argv)
    
    if not os.path.isfile(args.pdf):
        print(f"Error: file not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)
    
    pages = parse_pages_arg(args.pages) if args.pages else None
    
    # Default: --list
    if args.list or not (args.tables or args.images or args.all):
        result = list_all(args.pdf, pages)
        if args.json:
            print(json.dumps(result, indent=2))
        return
    
    if args.all:
        args.tables = True
        args.images = True
    
    if args.tables:
        tables = extract_tables(args.pdf, pages, args.format)
        if args.json:
            print(json.dumps(tables, indent=2, ensure_ascii=False))
        else:
            for t in tables:
                print(f"\n{'='*60}", file=sys.stderr)
                print(f"📊 Page {t['page']} — Table {t['table_num']} ({t['rows']}×{t['cols']})", file=sys.stderr)
                print(f"{'='*60}", file=sys.stderr)
                if args.format == "md":
                    print(t.get("markdown", ""))
                else:
                    print(t.get("tsv", ""))
    
    if args.images:
        imgs = extract_images(args.pdf, pages, args.output_dir)
        if args.json:
            print(json.dumps(imgs, indent=2))
        else:
            for img in imgs:
                print(f"  🖼️  Page {img['page']}, Image {img['image_num']}: {img['name']} → {img['path']}")


if __name__ == "__main__":
    main()
