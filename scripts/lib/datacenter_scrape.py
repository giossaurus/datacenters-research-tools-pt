# Copyright (c) 2025-2026 Giovanni Della Dea
"""Shared HTML / JSON extraction for datacentermap.com and datacenters.com."""

from __future__ import annotations

import codecs
import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

DATACENTERMAP_BASE = "https://www.datacentermap.com"
DATACENTERS_COM_BASE = "https://www.datacenters.com"


def _datacenters_com_split_address(full_address: str) -> tuple[str, str]:
    """Heurística simples a partir de fullAddress (lista do mapa / RSC)."""
    if not (full_address or "").strip():
        return "", ""
    parts = [p.strip() for p in full_address.split(",") if p.strip()]
    if len(parts) < 2:
        return full_address.strip(), ""
    tail = parts[-2]
    if " - " in tail:
        left, right = tail.split(" - ", 1)
        return left.strip(), right.strip()
    return tail, (parts[-3] if len(parts) >= 3 else "")


def _datacenters_com_decode_flight_payload(html_content: str) -> str:
    """Junta e descodifica o stream RSC `self.__next_f.push([1,"..."])`."""
    parts = re.findall(
        r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)',
        html_content,
    )
    if not parts:
        return ""
    try:
        return codecs.decode("".join(parts), "unicode_escape")
    except (UnicodeDecodeError, UnicodeError):
        return ""


def datacenters_com_extract_locations_from_rsc(html_content: str) -> tuple[list[dict], int]:
    """Extrai preloadedSearchLocations do payload Flight (Next.js)."""
    full_u = _datacenters_com_decode_flight_payload(html_content)
    if not full_u:
        return [], 0
    key = '"preloadedSearchLocations":['
    i = full_u.find(key)
    if i < 0:
        return [], 0
    start = i + len(key) - 1
    depth = 0
    for j in range(start, len(full_u)):
        if full_u[j] == "[":
            depth += 1
        elif full_u[j] == "]":
            depth -= 1
            if depth == 0:
                try:
                    arr = json.loads(full_u[start : j + 1])
                except json.JSONDecodeError:
                    return [], 0
                locs: list[dict] = []
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    prov = item.get("provider") or {}
                    fa = (item.get("fullAddress") or "").strip()
                    city, region = _datacenters_com_split_address(fa)
                    locs.append({
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "city": city,
                        "region": region,
                        "latitude": item.get("latitude"),
                        "longitude": item.get("longitude"),
                        "detailUrl": item.get("url"),
                        "providerName": prov.get("name", ""),
                    })
                m = re.search(r'"totalPreloadedSearchLocations":(\d+)', full_u)
                total = int(m.group(1)) if m else len(locs)
                return locs, total
    return [], 0


def datacentermap_html_looks_like_bot_wall(html_content: str) -> bool:
    """Página de desafio (ex.: Vercel) sem dados úteis do mapa."""
    lower = html_content[:32768].lower()
    return any(
        x in lower
        for x in (
            "vercel security checkpoint",
            "security checkpoint",
            "/challenge",
            "cf-challenge",
            "enable javascript",
            "we're verifying your browser",
            "verifying your browser",
            "just a moment",
        )
    )


_NEXT_DATA_SCRIPT_RE = re.compile(
    r'<script[^>]*\bid\s*=\s*["\']__NEXT_DATA__["\'][^>]*>(?P<body>.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def parse_datacentermap_next_data_json(
    html_content: str,
    city_name: str = "",
) -> dict | None:
    """
    Extrai o objeto JSON do script __NEXT_DATA__ (BeautifulSoup + fallback regex).
    Devolve None se parecer bot-wall, não houver payload ou JSON inválido.
    """
    if datacentermap_html_looks_like_bot_wall(html_content):
        return None

    soup = BeautifulSoup(html_content, "html.parser")
    script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script_tag:
        for s in soup.find_all("script"):
            if (s.get("id") or "").strip().lower() == "__next_data__":
                script_tag = s
                break

    raw_json = ""
    if script_tag:
        raw_json = (script_tag.string or script_tag.get_text() or "").strip()
    if not raw_json:
        m = _NEXT_DATA_SCRIPT_RE.search(html_content)
        if m:
            raw_json = m.group("body").strip()

    if not raw_json:
        return None

    try:
        return json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(f"    JSON parse error for {city_name}: {e}")
        return None


def _geojson_feature_list(candidate: list) -> bool:
    if not candidate or not isinstance(candidate[0], dict):
        return False
    sample = candidate[0]
    return "properties" in sample and "geometry" in sample


def _find_dcs_list_in_value(obj, depth: int = 0):
    """Procura listas tipo GeoJSON Feature sob pageProps (estrutura pode mudar)."""
    if depth > 14:
        return None
    if isinstance(obj, dict):
        if "dcs" in obj and isinstance(obj["dcs"], list) and _geojson_feature_list(obj["dcs"]):
            return obj["dcs"]
        for v in obj.values():
            found = _find_dcs_list_in_value(v, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list) and obj and _geojson_feature_list(obj):
        return obj
    elif isinstance(obj, list):
        for item in obj[:30]:
            found = _find_dcs_list_in_value(item, depth + 1)
            if found is not None:
                return found
    return None


def datacentermap_records_from_page_props(
    page_props: dict,
    city_name: str,
    *,
    base_url: str = DATACENTERMAP_BASE,
) -> list[dict]:
    """Converte pageProps do Next.js (mapdata.dcs ou equivalente) em linhas tabulares."""
    md = page_props.get("mapdata") or {}
    if not isinstance(md, dict):
        md = {}
    dcs_data = md.get("dcs", [])

    if not dcs_data:
        found = _find_dcs_list_in_value(page_props)
        if found:
            dcs_data = found

    out: list[dict] = []
    for dc in dcs_data:
        props = dc.get("properties", {})
        name = (props.get("name") or "").strip()
        # "Unmapped Data Centers" is a synthetic aggregate marker, not a real datacenter.
        if name.lower().startswith("unmapped"):
            continue
        coords = dc.get("geometry", {}).get("coordinates", [])
        out.append({
            "name":        name,
            "company":     props.get("companyname", ""),
            "address":     props.get("address", ""),
            "postal_code": props.get("postal", ""),
            "city":        props.get("city", city_name),
            "state":       props.get("state", ""),
            "country":     props.get("country", ""),
            "url":         urljoin(base_url, props.get("url", "")),
            "description": props.get("oneliner", ""),
            "type":        props.get("type", ""),
            "longitude":   coords[0] if len(coords) > 0 else None,
            "latitude":    coords[1] if len(coords) > 1 else None,
            "source_city": city_name,
        })
    return out


def datacentermap_records_from_next_data(
    html_content: str,
    city_name: str,
    *,
    base_url: str = DATACENTERMAP_BASE,
) -> list[dict]:
    json_data = parse_datacentermap_next_data_json(html_content, city_name=city_name)
    if not json_data:
        return []
    page_props = json_data.get("props", {}).get("pageProps", {})
    if not isinstance(page_props, dict):
        return []
    return datacentermap_records_from_page_props(
        page_props, city_name, base_url=base_url
    )
def datacentermap_records_from_html_cards(
    html_content: str,
    city_name: str,
    *,
    base_url: str = DATACENTERMAP_BASE,
) -> list[dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    cards = soup.find_all("a", class_="ui card")
    datacenters: list[dict] = []
    for card in cards:
        header = card.find("div", class_="header")
        name = header.get_text(strip=True) if header else "Unknown"
        description = card.find("div", class_="description")
        if not description:
            continue
        lines = [l.strip() for l in description.get_text("\n", strip=True).split("\n") if l.strip()]
        company = lines[0] if lines else ""
        address = lines[1] if len(lines) > 1 else ""
        url = card.get("href", "")
        datacenters.append({
            "name": name, "company": company, "address": address,
            "url": urljoin(base_url, url) if url else "",
            "source_city": city_name,
        })
    return datacenters


def datacenters_com_extract_locations(html_content: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html_content, "html.parser")
    for script in soup.find_all("script"):
        if not script.string:
            continue
        if "country" not in script.string or "locations" not in script.string:
            continue
        try:
            data = json.loads(script.string)
            if "country" in data and "locations" in data["country"]:
                c = data["country"]
                return c["locations"], int(c.get("locationCount") or 0)
        except json.JSONDecodeError:
            m = re.search(r'"locations"\s*:\s*(\[.*?\])\s*[,}]', script.string, re.DOTALL)
            if not m:
                m = re.search(r'"locations"\s*:\s*(\[.*?\])', script.string, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1)), 0
                except json.JSONDecodeError:
                    continue
    locs, total = datacenters_com_extract_locations_from_rsc(html_content)
    if locs:
        return locs, total
    return [], 0


def _datacenters_com_card_fields_from_inner(div) -> dict | None:
    provider_elem = div.find("div", class_="text-xs text-gray-500")
    provider = provider_elem.get_text().strip() if provider_elem else ""
    name_elem = div.find("div", class_="text font-medium hover:text-purple")
    if not name_elem:
        name_elem = div.find("div", class_="font-medium")
    name = name_elem.get_text().strip() if name_elem else ""
    address_elems = div.find_all("div", class_="text-xs text-gray-500")
    address = ""
    if len(address_elems) > 1:
        address = address_elems[1].get_text().strip()
    elif len(address_elems) == 1 and address_elems[0].get_text().strip() != provider:
        address = address_elems[0].get_text().strip()
    if not name and not provider:
        return None
    return {"provider": provider, "name": name, "address": address}


def datacenters_com_records_from_html_cards(html_content: str) -> list[dict]:
    soup = BeautifulSoup(html_content, "html.parser")
    records: list[dict] = []
    for div in soup.find_all("div", class_="flex flex-col gap-1"):
        row = _datacenters_com_card_fields_from_inner(div)
        if row:
            records.append(row)
    for a in soup.find_all("a", class_=lambda c: c and "hover:border-teal" in c):
        inner = a.find(
            "div",
            class_=lambda c: bool(c) and "flex" in c.split() and "flex-col" in c.split() and "gap-2" in c.split(),
        )
        if not inner:
            continue
        row = _datacenters_com_card_fields_from_inner(inner)
        if row:
            records.append(row)
    return records


_COUNTRY_PAGE_SKIP_SLUGS = frozenset({
    "quote",
    "index",
    "auth",
    "consulting",
    "africa",
    "europe",
    "asia",
    "americas",
    "north-america",
    "south-america",
    "oceania",
    "middle-east",
})


def _datacentermap_country_index_city_slugs_from_next_data(
    json_data: dict,
    country_slug: str,
) -> list[str]:
    """
    Tenta extrair slugs de cidades/mercados do __NEXT_DATA__ da página índice do país.

    A estrutura exata pode variar com deploys do site; esta função tenta múltiplas
    chaves conhecidas e cai para lista vazia se não encontrar nada reconhecível.
    """
    slugs: list[str] = []
    page_props = json_data.get("props", {}).get("pageProps", {})

    def _collect_from_obj(obj, depth: int = 0) -> None:
        if depth > 10 or not obj:
            return
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    slug = item.get("slug") or item.get("citySlug") or item.get("city_slug")
                    if slug and isinstance(slug, str):
                        slugs.append(slug)
                    _collect_from_obj(item, depth + 1)
        elif isinstance(obj, dict):
            for key in ("markets", "cities", "locations", "areas"):
                if key in obj:
                    _collect_from_obj(obj[key], depth + 1)

    _collect_from_obj(page_props)

    # Normalise: keep only slugs that look like city slugs (letters/hyphens, no dots/slashes)
    _slug_re = re.compile(r'^[a-z0-9][a-z0-9\-]{1,}[a-z0-9]$')
    return [s.lower().strip() for s in slugs if _slug_re.match(s.lower().strip())]


def _datacentermap_country_index_city_slugs_from_html(
    html_content: str,
    country_slug: str,
) -> list[str]:
    """
    Extrai slugs de cidades a partir de links href=/{country_slug}/{slug}/ na página índice.
    """
    pattern = re.compile(
        rf'href=["\']/{re.escape(country_slug)}/([a-z0-9][a-z0-9\-]+)[/"\']',
        re.IGNORECASE,
    )
    found = set()
    for match in pattern.finditer(html_content):
        slug = match.group(1).lower()
        if slug not in _COUNTRY_PAGE_SKIP_SLUGS:
            found.add(slug)
    return sorted(found)


def discover_city_slugs(
    html_content: str,
    country_slug: str,
) -> list[str]:
    """
    Descobre os slugs de cidades/mercados listados na página índice de um país
    no datacentermap.com.

    Tenta primeiro o __NEXT_DATA__ (estruturado); recai para extração de links HTML.

    Parameters
    ----------
    html_content:
        HTML bruto da página ``/{country_slug}/``.
    country_slug:
        Slug do país conforme usado no site (ex.: ``"brazil"``).

    Returns
    -------
    list[str]
        Lista de slugs únicos, ordenados, sem slugs de páginas de utilidade
        (ex.: ``"quote"``, ``"auth"``).
    """
    if datacentermap_html_looks_like_bot_wall(html_content):
        return []

    json_data = parse_datacentermap_next_data_json(html_content)
    slugs: list[str] = []

    if json_data:
        slugs = _datacentermap_country_index_city_slugs_from_next_data(json_data, country_slug)

    if not slugs:
        slugs = _datacentermap_country_index_city_slugs_from_html(html_content, country_slug)

    # Deduplicate and sort, excluding utility slugs.
    return sorted({s for s in slugs if s not in _COUNTRY_PAGE_SKIP_SLUGS})


def datacenters_com_locations_to_records(
    locations: list[dict],
    *,
    base_url: str = DATACENTERS_COM_BASE,
    source_file: str | None = None,
) -> list[dict]:
    rows = []
    for loc in locations:
        row = {
            "id": loc.get("id"),
            "name": loc.get("name"),
            "city": loc.get("city"),
            "region": loc.get("region"),
            "latitude": loc.get("latitude"),
            "longitude": loc.get("longitude"),
            "details_url": urljoin(base_url, loc.get("detailUrl", "") or ""),
        }
        prov = loc.get("providerName") or loc.get("provider")
        if prov:
            row["provider"] = prov
        if source_file is not None:
            row["source_file"] = source_file
        rows.append(row)
    return rows
