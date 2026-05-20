#!/usr/bin/env python3
"""Get data products, catalogs, and related materials for an ADS paper.

Usage:
    ads_data_products.py <bibcode>               # List all data products
    ads_data_products.py <bibcode> --download    # Download available data
    ads_data_products.py <bibcode> --catalogs    # List catalogs mentioned in paper
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

try:
    from pdfminer.high_level import extract_text
except ImportError:
    extract_text = None

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

ADS_API_URL = "https://api.adsabs.harvard.edu/v1"
GITHUB_RAW = "https://raw.githubusercontent.com/maartenvandekant/papers/main"

# Known catalog / service patterns found in astronomy papers
CATALOG_PATTERNS = {
    "2MASS":           (r'\b2MASS\b', "NASA/IPAC Infrared Science Archive"),
    "Gaia":            (r'\bGaia\b', "ESA Gaia mission"),
    "Gaia EDR3":      (r'Gaia\s+EDR3', "ESA Gaia Early Data Release 3"),
    "Gaia DR2":        (r'Gaia\s+DR2', "ESA Gaia Data Release 2"),
    "Tycho-2":        (r'\bTycho[- ]2\b', "Hipparcos Tycho-2 catalog"),
    "Hipparcos":      (r'\bHipparcos\b', "ESA Hipparcos mission"),
    "WISE":           (r'\bWISE\b', "NASA WISE survey"),
    "IRAS":           (r'\bIRAS\b', "NASA IRAS survey"),
    "Planck":         (r'\bPlanck\b', "ESA Planck mission"),
    "VizieR":         (r'VizieR', "CDS catalog service"),
    "SIMBAD":         (r'\bSIMBAD\b', "CDS astronomical database"),
    "NED":            (r'\bNED\b', "NASA Extragalactic Database"),
    "SDSS":           (r'\bSDSS\b', "Sloan Digital Sky Survey"),
    "APASS":          (r'\bAPASS\b', "AstroPy Photoalliance"),
    "IRSA":           (r'\bIRSA\b', "NASA/IPAC IRSA"),
    "JCMT":           (r'\bJCMT\b', "James Clerk Maxwell Telescope"),
    "ALMA":           (r'\bALMA\b', "Atacama Large Millimeter Array"),
    "VLT":            (r'\bVLT\b', "Very Large Telescope"),
    "Spitzer":        (r'\bSpitzer\b', "Spitzer Space Telescope"),
    "Herschel":       (r'\bHerschel\b', "Herschel Space Observatory"),
    "CORAVEL":        (r'\bCORAVEL\b', "Radial velocity survey"),
    "Bica":           (r'\bBica\b', "Catalog of star clusters"),
    "Kharchenko":     (r'\bKharchenko\b', "MWSC stellar catalog"),
    "Mend":           (r'\bMend\b', "Catalog of Be stars"),
    "DAML":           (r'\bDAML\b', "Database of Massive Stars"),
    "WR":             (r'\bWR\s+catalog', "Wolf-Rayet catalog"),
}

# Catalog download URLs (VizieR / IRSA / CDS)
CATALOG_URLS = {
    "2MASS":    "https://irsa.ipac.caltech.edu/cgi-bin/OasisPaperDataSet/nph-acirdb?project=2MASS&searchString=",
    "Gaia DR2": "https://cdn.gea.ari.uni-heidelberg.de/rd/download/GaiaDR2.dat",
    "Gaia EDR3": "https://cdn.gea.ari.uni-heidelberg.de/rd/download/GaiaEDR3.dat",
    "Tycho-2":  "ftp://cdsarc.u-strasbg.fr/pub/cats/I/259/tyc2.dat.gz",
    "Hipparcos": "ftp://cdsarc.u-strasbg.fr/pub/cats/I/239/hip_main.dat.gz",
    "IRAS":     "https://irsa.ipac.caltech.edu/cgi-bin/OasisPaperDataSet/nph-acirdb?project=IRAS&searchString=",
    "WISE":     "https://irsa.ipac.caltech.edu/cgi-bin/OasisPaperDataSet/nph-acirdb?project=WISE&searchString=",
    "Planck":   "https://www.cosmos.esa.int/web/planck/pla",
    "VizieR":   "https://vizier.cds.unistra.fr/",
    "SIMBAD":   "https://simbad.cds.unistra.fr/simbad/sim-fref",
    "IRSA":     "https://irsa.ipac.caltech.edu/",
}


# ──────────────────────────────────────────────────────────────────────────────
# ADS API helpers
# ──────────────────────────────────────────────────────────────────────────────

def load_token() -> str:
    from research_library.config import load_ads_token

    return load_ads_token()


def ads_query(bibcode: str, fields: str, token: str) -> dict:
    """Query ADS API for a single bibcode."""
    params = urllib.parse.urlencode({
        "q": f"bibcode:{bibcode}",
        "rows": 1,
        "fl": fields,
    })
    url = f"{ADS_API_URL}/search/query?{params}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def parse_data_field(data_str: str) -> list[dict]:
    """Parse 'IRSA:2,SIMBAD:52' into structured list."""
    result = []
    if not data_str:
        return result
    for part in data_str.split(","):
        part = part.strip()
        if ":" in part:
            provider, count = part.rsplit(":", 1)
            try:
                count = int(count.strip())
            except ValueError:
                count = None
            result.append({"provider": provider.strip(), "count": count})
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Catalog extraction from PDF
# ──────────────────────────────────────────────────────────────────────────────

def extract_catalogs_from_pdf(pdf_path: str) -> list[dict]:
    """Find catalog/database mentions in the PDF text."""
    if not extract_text:
        return []
    
    try:
        text = extract_text(pdf_path)
    except Exception:
        return []
    
    found = []
    for cat_name, (pattern, description) in CATALOG_PATTERNS.items():
        matches = list(re.finditer(pattern, text, re.IGNORECASE))
        if matches:
            # Get context for each match
            contexts = []
            for m in matches[:3]:  # limit to 3
                start = max(0, m.start() - 40)
                end = min(len(text), m.end() + 60)
                ctx = text[start:end].replace("\n", " ").strip()
                contexts.append(f"...{ctx}...")
            
            # Build VizieR / IRSA URL if applicable
            url = ""
            if cat_name in CATALOG_URLS:
                url = CATALOG_URLS[cat_name]
            
            # Known VizieR catalog codes for common catalogs
            vizier_codes = {
                "2MASS": "II/246", "Gaia": "I/355", "Gaia EDR3": "I/355",
                "Gaia DR2": "I/345", "Tycho-2": "I/259", "Hipparcos": "I/239",
                "WISE": "II/311", "IRAS": "II/125", "Planck": "IX/56",
                "APASS": "II/336", "SDSS": "V/154",
            }
            vizier_url = f"https://vizier.u-strasbg.fr/vizier/cat4/{vizier_codes.get(cat_name, '')}" if cat_name in vizier_codes else ""
            
            found.append({
                "name": cat_name,
                "description": description,
                "occurrences": len(matches),
                "contexts": contexts,
                "catalog_url": url,
                "vizier_url": vizier_url if cat_name in vizier_codes else "",
            })
    
    return found


# ──────────────────────────────────────────────────────────────────────────────
# Download helpers
# ──────────────────────────────────────────────────────────────────────────────

def download_catalog(cat_name: str, dest_dir: str) -> str | None:
    """Download a catalog to dest_dir. Returns path or None."""
    import urllib.error
    
    if cat_name not in CATALOG_URLS:
        return None
    
    url = CATALOG_URLS[cat_name]
    if not url:
        return None
    
    os.makedirs(dest_dir, exist_ok=True)
    
    # Determine filename
    filename = url.split("/")[-1].split("?")[0]
    if not filename or "." not in filename:
        filename = f"{cat_name.lower().replace(' ', '_')}.dat"
    dest_path = os.path.join(dest_dir, filename)
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return dest_path
    except Exception as e:
        print(f"  Download failed: {e}", file=sys.stderr)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    from research_library.config import load_env

    load_env()
    parser = argparse.ArgumentParser(description="Get ADS paper data products, catalogs, and related materials.")
    parser.add_argument("bibcode", help="ADS bibcode (e.g. 2024A&A...689A.225N)")
    parser.add_argument("--pdf", help="Also scan this PDF for catalog references")
    parser.add_argument("--download", action="store_true", help="Download catalog data")
    parser.add_argument("--dest", default="./catalogs", help="Download destination directory")
    parser.add_argument("--json", action="store_true", help="JSON output")
    
    args = parser.parse_args(argv)
    bibcode = args.bibcode.strip()
    token = load_token()
    
    if not token:
        print("Error: ADS_API_TOKEN not found. Set it in .env or environment.", file=sys.stderr)
        sys.exit(1)
    
    result = {"bibcode": bibcode, "data_products": [], "catalogs": []}
    
    # 1. ADS data products
    print(f"Fetching ADS data products for {bibcode}...", file=sys.stderr)
    try:
        data = ads_query(bibcode, "bibcode,title,author,data,database", token)
        docs = data.get("response", {}).get("docs", [])
        if docs:
            doc = docs[0]
            data_field = doc.get("data", [])
            if isinstance(data_field, list):
                result["ads_data_providers"] = data_field
                for item in parse_data_field(",".join(data_field)):
                    result["data_products"].append({
                        "type": "data_provider",
                        "provider": item["provider"],
                        "count": item["count"],
                        "info": f"https://ui.adsabs.harvard.edu/abs/{bibcode}/access"
                    })
            result["title"] = doc.get("title", [""])[0] if doc.get("title") else ""
            result["authors"] = doc.get("author", [])[:3]
    except Exception as e:
        print(f"ADS query failed: {e}", file=sys.stderr)
    
    # 2. Catalog mentions in PDF
    if args.pdf and os.path.isfile(args.pdf):
        print(f"Scanning PDF for catalog references...", file=sys.stderr)
        cats = extract_catalogs_from_pdf(args.pdf)
        result["catalogs"] = cats
        print(f"  Found {len(cats)} catalog references", file=sys.stderr)
    
    # 3. Download (if requested)
    if args.download and args.pdf and os.path.isfile(args.pdf):
        print(f"Downloading catalogs to {args.dest}...", file=sys.stderr)
        downloaded = []
        for cat in result.get("catalogs", []):
            if cat["name"] in CATALOG_URLS:
                path = download_catalog(cat["name"], args.dest)
                if path:
                    downloaded.append({"catalog": cat["name"], "path": path})
        result["downloaded"] = downloaded
    
    # Output
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"\n{'='*60}")
        print(f"Paper: {result.get('title', bibcode)}")
        authors = result.get('authors', [])
        if authors:
            print(f"Authors: {', '.join(authors[:3])}{' et al.' if len(authors) > 3 else ''}")
        print(f"Bibcode: {bibcode}")
        
        if result.get("ads_data_providers"):
            print(f"\n📦 ADS Data Products ({len(result['data_products'])})")
            for dp in result["data_products"]:
                print(f"  • {dp['provider']} — {dp['count']} entries | {dp['info']}")
        
        if result.get("catalogs"):
            print(f"\n📚 Catalogs / Data Services Found ({len(result['catalogs'])})")
            for cat in result["catalogs"]:
                print(f"  • {cat['name']} — {cat['description']} ({cat['occurrences']} mentions)")
                if cat.get("vizier_url"):
                    print(f"    VizieR: {cat['vizier_url']}")
                if cat.get("catalog_url"):
                    print(f"    Data: {cat['catalog_url']}")
                if cat.get("contexts"):
                    print(f"    Context: {cat['contexts'][0][:120]}")
        
        if result.get("downloaded"):
            print(f"\n💾 Downloaded Files")
            for dl in result["downloaded"]:
                print(f"  • {dl['catalog']} → {dl['path']}")


if __name__ == "__main__":
    main()
