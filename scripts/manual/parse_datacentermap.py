# Copyright (c) 2025-2026 Giovanni Della Dea
"""
parse_datacentermap.py: HTML manual → datacentermap_raw.csv/json
"""
from pathlib import Path
import json
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
    DATACENTERMAP_BASE,
    datacentermap_records_from_html_cards,
    datacentermap_records_from_next_data,
)

RAW_DIR = PROJECT_ROOT / "data" / "raw" / "datacentermap"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"


def process_city_file(file_path):
    city_name = file_path.name
    print(f"  Processing {city_name}...")
    with open(file_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    datacenters = datacentermap_records_from_next_data(
        html_content, city_name, base_url=DATACENTERMAP_BASE
    )
    if datacenters:
        print(f"    → __NEXT_DATA__: {len(datacenters)} datacenters")
        return datacenters
    datacenters = datacentermap_records_from_html_cards(
        html_content, city_name, base_url=DATACENTERMAP_BASE
    )
    print(f"    → HTML cards: {len(datacenters)} datacenters")
    return datacenters


def main():
    files = [f for f in RAW_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        print(f"No HTML files found in {RAW_DIR}")
        return
    print(f"Found {len(files)} city file(s) to process:")
    all_datacenters = []
    for file_path in sorted(files):
        all_datacenters.extend(process_city_file(file_path))
    print(f"\nTotal datacenters found: {len(all_datacenters)}")
    if not all_datacenters:
        print("No data extracted.")
        return
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    json_path = PROCESSED_DIR / "datacentermap_raw.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_datacenters, f, indent=2, ensure_ascii=False)
    print(f"JSON saved to: {json_path}")
    df = pd.DataFrame(all_datacenters)
    csv_path = PROCESSED_DIR / "datacentermap_raw.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"CSV saved to:  {csv_path}")
    print(f"\nSummary:")
    print(f"  Total datacenters: {len(df)}")
    print(f"  Cities processed:  {df['source_city'].nunique()}")
    print(f"  Unique companies:  {df['company'].nunique()}")


if __name__ == "__main__":
    main()
