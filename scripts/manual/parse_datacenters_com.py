# Copyright (c) 2025-2026 Giovanni Della Dea
"""parse_datacenters_com.py: HTML manual → datacenters_raw.csv"""
from pathlib import Path
import sys
import pandas as pd

_PROJECT_FALLBACK = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_PROJECT_FALLBACK))

from scripts.lib.workspace_root import resolve_project_root

PROJECT_ROOT = resolve_project_root(_PROJECT_FALLBACK)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.datacenter_scrape import (
    DATACENTERS_COM_BASE,
    datacenters_com_extract_locations,
    datacenters_com_locations_to_records,
    datacenters_com_records_from_html_cards,
)

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "datacenters"
OUTPUT_PATH = PROJECT_ROOT / "data" / "processed" / "datacenters_raw.csv"


def process_file(html_file):
    print(f"  Processing {html_file.name}...")
    with open(html_file, "r", encoding="utf-8") as f:
        html = f.read()
    locations, _ = datacenters_com_extract_locations(html)
    if locations:
        records = datacenters_com_locations_to_records(
            locations, base_url=DATACENTERS_COM_BASE, source_file=html_file.name
        )
        print(f"    → JSON: {len(records)} datacenters")
        return records
    cards = datacenters_com_records_from_html_cards(html)
    for card in cards:
        card["source_file"] = html_file.name
    print(f"    → HTML cards: {len(cards)} datacenters")
    return cards


def main():
    html_files = sorted(RAW_DIR.glob("datacenters-*"))
    if not html_files:
        print(f"No HTML files found in {RAW_DIR}")
        return
    print(f"Found {len(html_files)} file(s) to process:")
    all_records = []
    for html_file in html_files:
        all_records.extend(process_file(html_file))
    df = pd.DataFrame(all_records)
    if df.empty:
        print("No data extracted.")
        return
    initial = len(df)
    dedup_cols = [c for c in ["name", "provider", "city"] if c in df.columns]
    df = df.drop_duplicates(subset=dedup_cols, keep="first")
    print(f"\nDeduplication: {initial} → {len(df)} records")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_PATH, index=False, encoding="utf-8-sig")
    print(f"Saved to: {OUTPUT_PATH}")
    print(df.head())


if __name__ == "__main__":
    main()
