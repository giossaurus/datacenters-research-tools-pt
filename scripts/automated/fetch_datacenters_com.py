# Copyright (c) 2025-2026 Giovanni Della Dea
"""
fetch_datacenters_com.py
Fetches the datacenters.com country listing pages and extracts datacenter records.

Paginates with ?page=N until locationCount is reached or a page adds no new rows.

Output: data/processed/datacenters_raw.csv
"""

import logging
import sys
from pathlib import Path

import pandas as pd
import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from scraper_utils import (
    build_session,
    fetch_with_backoff,
    handle_interruption,
    is_blocked,
    make_proxy_cycle,
    random_delay,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

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

with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

BASE_URL = DATACENTERS_COM_BASE
COUNTRY_SLUG = config["country"]["datacenters_com_slug"]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

_proxy_list = config.get("proxies", []) or []
proxy_cycle = make_proxy_cycle(_proxy_list)

if _proxy_list:
    logger.info("Proxy rotation enabled: %d proxies configured.", len(_proxy_list))
else:
    logger.info("No proxies configured: running direct.")


def fetch_page(session: requests.Session, page: int | None = None) -> str | None:
    url = f"{BASE_URL}/locations/{COUNTRY_SLUG}"
    if page and page > 1:
        url += f"?page={page}"

    logger.info("Fetching %s…", url)

    try:
        response = fetch_with_backoff(
            session,
            url,
            proxy_cycle=proxy_cycle,
            extra_headers={
                "Referer": f"{BASE_URL}/locations/{COUNTRY_SLUG}",
                "Sec-Fetch-Site": "same-origin",
            },
        )
    except requests.HTTPError as exc:
        logger.warning("HTTP error: %s", exc)
        return None
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Connection error: %s", exc)
        return None

    if is_blocked(response):
        handle_interruption(url)
        return None

    return response.text


def _location_dedup_key(loc: dict) -> tuple:
    lid = loc.get("id")
    if lid is not None:
        return ("id", lid)
    return (
        "row",
        loc.get("name"),
        loc.get("city"),
        loc.get("region"),
        loc.get("detailUrl"),
    )


def collect_all_locations_json(session: requests.Session) -> tuple[list[dict], int]:
    page = 1
    seen_keys: set = set()
    merged: list[dict] = []
    reported_total = 0
    max_pages = 500

    while page <= max_pages:
        html = fetch_page(session, page)
        if html is None:
            if page == 1:
                return [], 0
            break

        locations, page_total = datacenters_com_extract_locations(html)
        if page == 1:
            reported_total = page_total

        if not locations:
            break

        new_on_page = 0
        for loc in locations:
            key = _location_dedup_key(loc)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(loc)
            new_on_page += 1

        logger.info(
            "    Page %d: +%d new (total %d%s)",
            page,
            new_on_page,
            len(merged),
            f" / {reported_total}" if reported_total else "",
        )

        if new_on_page == 0:
            break
        if reported_total and len(merged) >= reported_total:
            break

        page += 1
        random_delay(min_s=1.0, max_s=2.5)

    return merged, reported_total


def main() -> None:
    logger.info("Fetching datacenters.com data for %s…", config["country"]["name"])

    session = build_session()

    locations, reported_total = collect_all_locations_json(session)

    if locations:
        logger.info("HTML/RSC extraction: %d unique locations.", len(locations))
        if reported_total and len(locations) < reported_total:
            logger.warning(
                "Site reports %d total but only %d unique rows were merged.",
                reported_total,
                len(locations),
            )
        records = datacenters_com_locations_to_records(locations, base_url=BASE_URL)
    else:
        logger.warning("No locations in HTML: card fallback (page 1).")
        html = fetch_page(session, 1)
        if html is None:
            logger.error(
                "No HTML. Save manually: python scripts/manual/parse_datacenters_com.py"
            )
            sys.exit(1)
        random_delay(min_s=1.0, max_s=2.5)
        records = datacenters_com_records_from_html_cards(html)

    # País explícito em cada linha: o pipeline descarta registros de outro país (CSVs separados por país).
    records = [{**r, "country": config["country"]["name"]} for r in records]
    df = pd.DataFrame(records)

    if df.empty:
        logger.error("No data collected.")
        sys.exit(1)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    output_path = PROCESSED_DIR / "datacenters_raw.csv"

    # SQLite de coletas (aditivo): upsert + export do CSV (mesmo contrato).
    from scripts.lib import collections_db as cdb
    stats = cdb.ingest(PROJECT_ROOT, "datacenters_com", records, csv_out=output_path)
    logger.info("Coleta no .db: %s", stats)
    logger.info("CSV exportado (acumulado): %s", output_path)
    logger.info("Summary (run atual): %d rows | acumulado %d", len(df), stats["total"])
    if "provider" in df.columns:
        logger.info("  Unique providers: %d", df["provider"].nunique())


if __name__ == "__main__":
    main()
