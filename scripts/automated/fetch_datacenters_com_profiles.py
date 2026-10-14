# Copyright (c) 2025-2026 Giovanni Della Dea
"""
fetch_datacenters_com_profiles.py
Fetches the datacenters.com detail page of each facility in datacenters_<country>_raw.csv and extracts,
by regex over the visible text, the declared power (MW), operational status and certifications.
The matching text snippet is kept as evidence for manual validation.

Output: data/processed/datacenters_com_profiles_<country>.csv
"""

import logging
import re
import sys
from pathlib import Path

import pandas as pd
import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from scraper_utils import build_session, fetch_with_backoff, is_blocked, random_delay

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

_MW_RE = re.compile(r"(\d{1,4}(?:[.,]\d+)?)\s*MW\b")
_STATUS_RE = re.compile(r"\b(Fully operational|Operational|Under construction|Planned)\b", re.I)
_ISO_RE = re.compile(r"\bISO\s?(\d{4,5}(?:-\d)?)\b")


def visible_text(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def parse_detail(text: str) -> dict:
    mws = [(float(m.group(1).replace(",", ".")), m) for m in _MW_RE.finditer(text)]
    first = mws[0][1] if mws else None
    status = _STATUS_RE.search(text)
    return {
        "mw_valores_texto": "; ".join(f"{v:g}" for v, _ in mws),
        "mw_primeiro": mws[0][0] if mws else None,
        "mw_trecho": text[max(0, first.start() - 120): first.end() + 60] if first else None,
        "status_texto": status.group(1) if status else None,
        "certificacoes": "; ".join(sorted({f"ISO {c}" for c in _ISO_RE.findall(text)})),
    }


def main() -> None:
    src = PROCESSED_DIR / f"datacenters_{COUNTRY_SLUG}_raw.csv"
    listing = pd.read_csv(src)
    session = build_session()
    rows = []
    for i, r in enumerate(listing.itertuples(), start=1):
        logger.info("[%d/%d] %s", i, len(listing), r.details_url)
        try:
            resp = fetch_with_backoff(session, r.details_url)
        except (requests.HTTPError, requests.exceptions.ConnectionError) as exc:
            logger.warning("  falhou: %s", exc)
            continue
        if is_blocked(resp):
            logger.warning("  bloqueado")
            continue
        rows.append({"id": r.id, "name": r.name, "provider": r.provider, "details_url": r.details_url,
                     **parse_detail(visible_text(resp.text))})
        random_delay(min_s=2.0, max_s=4.0)
    out = PROCESSED_DIR / f"datacenters_com_profiles_{COUNTRY_SLUG}.csv"
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    logger.info("Perfis gravados: %d → %s", len(rows), out)


if __name__ == "__main__":
    main()
