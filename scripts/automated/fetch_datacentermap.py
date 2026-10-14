# Copyright (c) 2025-2026 Giovanni Della Dea
"""
fetch_datacentermap.py
Fetches and parses datacentermap.com city pages for the country defined in config.yaml.

Cities that fail (rate limit, soft block, CAPTCHA) are reported at the end so they can be
downloaded manually and processed with scripts/manual/parse_datacentermap.py.

Output: data/processed/datacentermap_raw.csv
        data/processed/datacentermap_raw.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import requests
import yaml

# Ensure scraper_utils is importable when running from project root
sys.path.insert(0, str(Path(__file__).parent))
from scraper_utils import (
    build_session,
    fetch_with_backoff,
    handle_interruption,
    is_blocked,
    make_proxy_cycle,
    random_delay,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_PROJECT_FALLBACK = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_PROJECT_FALLBACK))

from scripts.lib.workspace_root import resolve_project_root

PROJECT_ROOT = resolve_project_root(_PROJECT_FALLBACK)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.datacenter_scrape import (
    datacentermap_records_from_html_cards,
    datacentermap_records_from_next_data,
    discover_city_slugs,
)


with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

BASE_URL = "https://www.datacentermap.com"
COUNTRY_SLUG = config["country"]["slug"]
CITIES = config["cities"]
_AUTO_DISCOVER_DEFAULT = config.get("scraping", {}).get("auto_discover", False)
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

# Optional proxy list from config (key: proxies, value: list of proxy strings).
# Leave the key absent or set to [] to run without proxies.
_proxy_list = config.get("proxies", []) or []
proxy_cycle = make_proxy_cycle(_proxy_list)

if _proxy_list:
    logger.info("Proxy rotation enabled: %d proxies configured.", len(_proxy_list))
else:
    logger.info("No proxies configured: running direct.")

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_city_page(session: requests.Session, city: dict) -> str | None:
    """
    Fetch the datacentermap.com page for one city slug.

    Returns the HTML string on success, or None when the page is blocked /
    unreachable (caller adds the city to the manual list).
    """
    url = f"{BASE_URL}/{COUNTRY_SLUG}/{city['slug']}/"

    try:
        response = fetch_with_backoff(
            session,
            url,
            proxy_cycle=proxy_cycle,
            extra_headers={
                "Referer": f"{BASE_URL}/{COUNTRY_SLUG}/",
                "Sec-Fetch-Site": "same-origin",
            },
        )
    except requests.HTTPError as exc:
        logger.warning("HTTP error for %s: %s", city["name"], exc)
        return None
    except requests.exceptions.ConnectionError as exc:
        logger.warning("Connection error for %s: %s", city["name"], exc)
        return None

    if is_blocked(response):
        handle_interruption(url)
        return None

    return response.text


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def extract_next_data(html_content: str, city_name: str) -> list[dict]:
    """__NEXT_DATA__ primeiro; fallback para cards HTML."""
    rows = datacentermap_records_from_next_data(html_content, city_name, base_url=BASE_URL)
    if rows:
        return rows
    return datacentermap_records_from_html_cards(html_content, city_name, base_url=BASE_URL)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Coleta dados de datacenters do datacentermap.com."
    )
    parser.add_argument(
        "--auto-discover",
        action="store_true",
        default=_AUTO_DISCOVER_DEFAULT,
        help=(
            "Descobre automaticamente todos os slugs de cidades na página índice do país "
            "e inclui no scrape aquelas não presentes em config.yaml. "
            "Padrão lido de config.yaml (scraping.auto_discover)."
        ),
    )
    return parser.parse_args()


def _audit_discovered_slugs(
    discovered: list[str],
    config_cities: list[dict],
    *,
    auto_discover: bool,
) -> list[dict]:
    """
    Compara os slugs descobertos na página do site com os configurados em config.yaml.

    Emite warnings para slugs presentes no site mas ausentes no config.
    Se auto_discover=True, adiciona esses slugs à lista de cidades a coletar.

    Retorna a lista final de cidades (possivelmente expandida).
    """
    config_slugs = {c["slug"] for c in config_cities}
    missing = [s for s in discovered if s not in config_slugs]

    if not missing:
        logger.info("Auditoria de slugs: todas as %d cidades do site estão no config.yaml.", len(discovered))
        return list(config_cities)

    logger.warning(
        "AUDITORIA: %d slug(s) encontrado(s) no site mas AUSENTE(S) em config.yaml:",
        len(missing),
    )
    for slug in missing:
        url = f"{BASE_URL}/{COUNTRY_SLUG}/{slug}/"
        logger.warning("  AUSENTE  slug='%s'  url=%s", slug, url)

    if auto_discover:
        logger.info(
            "auto-discover ativo: adicionando %d cidade(s) descoberta(s) à coleta.",
            len(missing),
        )
        extra = [{"name": slug.replace("-", " ").title(), "slug": slug} for slug in missing]
        return list(config_cities) + extra

    logger.warning(
        "Para incluir essas cidades, adicione-as ao config.yaml ou use --auto-discover."
    )
    return list(config_cities)


def _persist_new_cities(config_path: Path, updated_cities: list[dict], n_new: int) -> None:
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["cities"] = updated_cities
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    logger.info("config.yaml atualizado com %d cidade(s) nova(s).", n_new)


def main() -> None:
    args = _parse_args()

    logger.info("Fetching datacentermap.com data for %s…", config["country"]["name"])
    logger.info("Cities configured: %d", len(CITIES))

    session = build_session()
    all_datacenters: list[dict] = []
    failed_cities: list[dict] = []

    # Fetch country index page: warm-up de cookies/CDN + auditoria de slugs.
    index_url = f"{BASE_URL}/{COUNTRY_SLUG}/"
    index_html: str | None = None
    try:
        index_resp = fetch_with_backoff(
            session,
            index_url,
            proxy_cycle=proxy_cycle,
            extra_headers={"Sec-Fetch-Site": "none"},
        )
        index_html = index_resp.text
        logger.info("Loaded country index: %s", index_url)
    except requests.HTTPError as exc:
        logger.warning("Country index not loaded (%s), continuing with cities: %s", index_url, exc)
    random_delay(min_s=1.0, max_s=2.5)

    # Auditoria: descobrir slugs no site e comparar com config.yaml.
    cities_to_fetch = list(CITIES)
    if index_html:
        discovered = discover_city_slugs(index_html, COUNTRY_SLUG)
        if discovered:
            logger.info("Slugs descobertos na página índice do país: %d", len(discovered))
            cities_to_fetch = _audit_discovered_slugs(
                discovered,
                CITIES,
                auto_discover=args.auto_discover,
            )
            n_new = len(cities_to_fetch) - len(CITIES)
            if n_new > 0:
                _persist_new_cities(PROJECT_ROOT / "config.yaml", cities_to_fetch, n_new)
        else:
            logger.warning(
                "Não foi possível extrair slugs da página índice "
                "(bot-wall ou estrutura desconhecida). Usando apenas config.yaml."
            )

    for i, city in enumerate(cities_to_fetch, 1):
        city_name = city["name"]
        logger.info("[%d/%d] %s (%s)…", i, len(cities_to_fetch), city_name, city["slug"])

        html = fetch_city_page(session, city)

        if html is None:
            logger.info("  → No HTML retrieved: adding to manual list.")
            failed_cities.append(city)
        else:
            datacenters = extract_next_data(html, city_name)
            if datacenters:
                logger.info("  → %d datacenters found.", len(datacenters))
                all_datacenters.extend(datacenters)
            else:
                from scripts.lib.datacenter_scrape import parse_datacentermap_next_data_json
                nd = parse_datacentermap_next_data_json(html, city_name=city_name)
                if nd is not None:
                    logger.info("  → Cidade sem datacenters listados na fonte (mapdata.dcs vazio).")
                else:
                    logger.warning("  → Falha ao extrair __NEXT_DATA__; adicionar à lista manual.")
                    failed_cities.append(city)

        if i < len(cities_to_fetch):
            random_delay(min_s=2.0, max_s=5.0)

    # ------------------------------------------------------------------
    logger.info("Total datacenters collected: %d", len(all_datacenters))

    if failed_cities:
        logger.warning(
            "\nThe following cities need manual download (%d):", len(failed_cities)
        )
        for city in failed_cities:
            url = f"{BASE_URL}/{COUNTRY_SLUG}/{city['slug']}/"
            logger.warning("  - %s: %s", city["name"], url)
        logger.warning(
            "  → Save each page manually and run: "
            "python scripts/manual/parse_datacentermap.py"
        )

    if all_datacenters:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

        json_path = PROCESSED_DIR / "datacentermap_raw.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(all_datacenters, f, indent=2, ensure_ascii=False)
        logger.info("JSON saved to: %s", json_path)

        # SQLite de coletas (aditivo): upsert + export do CSV (mesmo contrato).
        from scripts.lib import collections_db as cdb
        csv_path = PROCESSED_DIR / "datacentermap_raw.csv"
        stats = cdb.ingest(PROJECT_ROOT, "datacentermap", all_datacenters, csv_out=csv_path)
        logger.info("Coleta no .db: %s", stats)
        logger.info("CSV exportado (acumulado): %s", csv_path)

        df = pd.DataFrame(all_datacenters)
        logger.info("Summary (run atual):")
        logger.info("  Datacenters neste run: %d", len(df))
        logger.info("  Cities collected:  %d / %d", df["source_city"].nunique(), len(cities_to_fetch))
        logger.info("  Acumulado no banco: %d (novos %d, atualizados %d)", stats["total"], stats["new"], stats["updated"])
    else:
        logger.warning("No data collected. Use the manual scripts instead.")
        sys.exit(1)


if __name__ == "__main__":
    main()
