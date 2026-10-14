"""The datacenters.com collector reads only public pages; /api/ is disallowed by the site's robots.txt."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_FILES = [
    ROOT / "scripts" / "automated" / "fetch_datacenters_com.py",
    ROOT / "scripts" / "automated" / "fetch_datacenters_com_profiles.py",
    ROOT / "scripts" / "lib" / "datacenter_scrape.py",
]


def test_no_api_endpoint_in_collector_code():
    offenders = [str(p.relative_to(ROOT)) for p in COLLECTOR_FILES if "/api/" in p.read_text(encoding="utf-8")]
    assert offenders == []


def test_listing_starts_from_the_public_country_page():
    source = (ROOT / "scripts" / "automated" / "fetch_datacenters_com.py").read_text(encoding="utf-8")
    assert "map_api" not in source
    assert "collect_all_locations_json(session)" in source
