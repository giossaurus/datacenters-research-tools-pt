# Copyright (c) 2025-2026 Giovanni Della Dea
"""
fetch_datacentermap_profiles.py
Fetches the profile page of each facility listed in datacentermap_<country>_raw.csv and extracts
the structured `dc` object from __NEXT_DATA__ (capacity, status/stage, campus hierarchy, owners,
tenants).

The listing pages only carry name/company/address; capacity (MW), operational year, PUE and the
campus → building hierarchy live in the profile pages.

Input:  data/processed/datacentermap_<country>_raw.csv  (falls back to datacentermap_raw.csv)
Output: data/processed/datacentermap_profiles_<country>.csv
"""

import json
import logging
import re
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

with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

COUNTRY_SLUG = config["country"]["slug"]
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
proxy_cycle = make_proxy_cycle(config.get("proxies", []) or [])

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Scalar fields of the `dc` object kept as-is.
DC_FIELDS = [
    "id", "link", "name", "listingtype", "capacitytype", "status", "stage", "parent",
    "address", "address_details", "postal", "city", "state", "district", "market",
    "latitude", "longitude", "description",
]


def _input_csv() -> Path:
    country_csv = PROCESSED_DIR / f"datacentermap_{COUNTRY_SLUG}_raw.csv"
    return country_csv if country_csv.is_file() else PROCESSED_DIR / "datacentermap_raw.csv"


def _names(items: object) -> str:
    """Lista de dicts (owners/tenants/companies) → nomes separados por '; '."""
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return ""
    out = []
    for it in items:
        if isinstance(it, dict):
            inner = it.get("companies") if isinstance(it.get("companies"), dict) else it
            out.append(str(inner.get("name") or inner.get("link") or ""))
    return "; ".join(x for x in out if x)


def _flatten_meta(dc: dict) -> dict:
    """meta_power / meta_capacity / meta_building / meta_stats → colunas planas com prefixo."""
    flat: dict = {}
    for key, value in dc.items():
        if not key.startswith("meta_") or not isinstance(value, dict):
            continue
        prefix = key[len("meta_"):]
        for k, v in value.items():
            if not isinstance(v, (dict, list)):
                flat[f"{prefix}_{k}"] = v
    return flat


def parse_profile(html: str) -> dict | None:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
    dc = props.get("dc")
    if not isinstance(dc, dict):
        return None
    row = {f: dc.get(f) for f in DC_FIELDS}
    row.update(_flatten_meta(dc))
    parent = props.get("parent") or {}
    children = props.get("dcchildren") or []
    row.update({
        "company": _names(dc.get("companies")),
        "parent_link": parent.get("link") if isinstance(parent, dict) else None,
        "parent_name": parent.get("name") if isinstance(parent, dict) else None,
        "parent_listingtype": parent.get("listingtype") if isinstance(parent, dict) else None,
        "n_children": len(children) if isinstance(children, list) else 0,
        "children_links": "; ".join(c.get("link", "") for c in children if isinstance(c, dict)),
        "owners": _names(props.get("owners")),
        "tenants": _names(props.get("tenants")),
    })
    return row


def main() -> None:
    src = _input_csv()
    listing = pd.read_csv(src)
    urls = listing["url"].dropna().drop_duplicates().tolist()
    logger.info("Perfis a coletar: %d (de %s)", len(urls), src.name)

    session = build_session()
    rows: list[dict] = []
    failed: list[str] = []
    for i, url in enumerate(urls, start=1):
        logger.info("[%d/%d] %s", i, len(urls), url)
        try:
            response = fetch_with_backoff(session, url, proxy_cycle=proxy_cycle)
        except (requests.HTTPError, requests.exceptions.ConnectionError) as exc:
            logger.warning("  falhou: %s", exc)
            failed.append(url)
            continue
        if is_blocked(response):
            handle_interruption(url)
            failed.append(url)
            continue
        row = parse_profile(response.text)
        if row is None:
            logger.warning("  sem __NEXT_DATA__/dc")
            failed.append(url)
        else:
            row["url"] = url
            rows.append(row)
        random_delay(min_s=2.0, max_s=4.0)

    out = PROCESSED_DIR / f"datacentermap_profiles_{COUNTRY_SLUG}.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    logger.info("Perfis gravados: %d → %s", len(rows), out)
    if failed:
        logger.warning("Falharam (%d): %s", len(failed), ", ".join(failed))


if __name__ == "__main__":
    main()
