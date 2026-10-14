# Copyright (c) 2025-2026 Giovanni Della Dea
"""
curate_facilities.py
Builds ONE analytical dataset of facilities (sites) from datacentermap.com + datacenters.com,
applying the curation rules R1–R5 declared in config/curation_<country>.yaml.

Every source record gets a row in the curation table saying which rule placed it where
(site, building of a campus, cross-source duplicate, colocation tenant), so the dataset is auditable.

Inputs  (data/processed/):
  datacentermap_profiles_<c>.csv   (fetch_datacentermap_profiles.py)
  datacentermap_<c>_raw.csv        (listing; covers facilities whose profile failed)
  datacenters_<c>_raw.csv          (datacenters.com)
Outputs (data/processed/):
  curation_table_<c>.csv           one row per source record
  facilities_curated_<c>.csv       one row per site (unit of analysis)
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yaml

_PROJECT_FALLBACK = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_PROJECT_FALLBACK))

from scripts.lib.workspace_root import resolve_project_root

PROJECT_ROOT = resolve_project_root(_PROJECT_FALLBACK)
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# GeoAPI (json.geoapi.pt) limita o plano gratuito a 5 req/dia → Nominatim/OSM (limites municipais de PT vêm da CAOP).
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "datacenters-research-tools-pt (https://github.com/giossaurus/datacenters-research-tools-pt)"
STAGE_LABEL = {0: "planejado", 1: "em_construcao", 2: "operacional"}


def _country_slug() -> str:
    with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
        return str((yaml.safe_load(f).get("country") or {})["slug"])


def _norm(value: object) -> str:
    s = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", s.lower()).strip()


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * np.arcsin(np.sqrt(a))


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _dcmap_records(slug: str, canon) -> pd.DataFrame:
    prof = pd.read_csv(PROCESSED_DIR / f"datacentermap_profiles_{slug}.csv")
    listing = pd.read_csv(PROCESSED_DIR / f"datacentermap_{slug}_raw.csv")
    prof["key"] = "dcmap:" + prof["link"]
    # Facilities whose profile failed: keep the listing row (no stage / MW).
    missing = listing[~listing["url"].isin(prof["url"])].copy()
    missing["link"] = missing["url"].str.rstrip("/").str.split("/").str[-1]
    missing["key"] = "dcmap:" + missing["link"]
    missing["listingtype"] = "Facility"
    rec = pd.concat([prof, missing], ignore_index=True)
    rec["fonte"] = "datacentermap"
    rec["operador"] = rec["company"].map(canon)
    return rec


def _dccom_records(slug: str, canon) -> pd.DataFrame:
    d = pd.read_csv(PROCESSED_DIR / f"datacenters_{slug}_raw.csv")
    d["key"] = "dccom:" + d["name"].astype(str) + "|" + d["provider"].astype(str)
    d["fonte"] = "datacenters.com"
    d["company"] = d["provider"]
    d["operador"] = d["provider"].map(canon)
    return d


def _mw_by_stage(r: pd.Series) -> dict[str, float | str | None]:
    """R3: MW do registro distribuído pelo estágio declarado (campo usado fica em mw_campo)."""
    stage = _num(r.get("stage"))
    total = _num(r.get("power_totalmw"))
    built = _num(r.get("capacity_mw_builtout"))
    pipe = _num(r.get("capacity_mw_pipeline"))
    ucon = _num(r.get("capacity_mw_underconstruction"))
    out: dict[str, float | str | None] = {"mw_operacional": None, "mw_em_construcao": None,
                                          "mw_planejado": None, "mw_campo": None}
    val: float | None
    field: str | None
    if stage == 2:
        val, field = (total, "power_totalmw") if total is not None else (built, "capacity_mw_builtout")
        out["mw_operacional"] = val
    elif stage == 1:
        val, field = next(((v, f) for v, f in ((ucon, "capacity_mw_underconstruction"), (total, "power_totalmw"),
                                               (built, "capacity_mw_builtout")) if v is not None), (None, None))
        out["mw_em_construcao"] = val
    elif stage == 0:
        val, field = next(((v, f) for v, f in ((pipe, "capacity_mw_pipeline"), (total, "power_totalmw"),
                                               (built, "capacity_mw_builtout")) if v is not None), (None, None))
        out["mw_planejado"] = val
    else:
        val, field = None, None
    out["mw_campo"] = field if val is not None else None
    return out


# ---------------------------------------------------------------------------
# Curation
# ---------------------------------------------------------------------------

def curate(dcmap: pd.DataFrame, dccom: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    overrides = cfg.get("overrides") or {}
    same_km = float(cfg.get("same_operator_km", 10.0))
    rows: list[dict] = []

    # --- datacentermap: hierarquia declarada pela fonte (R1) e clientes declarados (R2)
    by_link = dcmap.set_index("link")
    for _, r in dcmap.iterrows():
        base = {"key": r["key"], "fonte": r["fonte"], "name": r["name"], "company": r["company"],
                "operador": r["operador"], "city": r.get("city"), "latitude": r["latitude"],
                "longitude": r["longitude"], "stage": r.get("stage"), **_mw_by_stage(r)}
        parent = r.get("parent_link") if isinstance(r.get("parent_link"), str) else None
        ov = overrides.get(r["key"])
        if ov and "stage" in ov and "papel" not in ov:
            # R3 manual: só corrige o estágio declarado pela fonte; o registro segue as regras normais.
            rows.append({**base, "stage": ov["stage"], "papel": "site", "facility_id": r["link"],
                         "regra": ov["motivo"].split(" ")[0], "justificativa": ov["motivo"]})
        elif ov:
            rows.append({**base, "papel": ov["papel"], "facility_id": ov.get("host") or ov.get("facility"),
                         "regra": ov["motivo"].split(" ")[0], "justificativa": ov["motivo"]})
        elif parent and parent in by_link.index and by_link.loc[parent, "operador"] != r["operador"]:
            rows.append({**base, "papel": "cliente_colocation", "facility_id": parent, "regra": "R2",
                         "justificativa": f"fonte declara parent = {parent} (outro operador)"})
        elif parent:
            rows.append({**base, "papel": "predio", "facility_id": parent, "regra": "R1",
                         "justificativa": f"prédio do campus {parent} (parent declarado na fonte)"})
        else:
            rows.append({**base, "papel": "site", "facility_id": r["link"], "regra": "R1",
                         "justificativa": "instalação de topo na fonte"})

    sites = pd.DataFrame([x for x in rows if x["papel"] == "site"])

    # --- datacenters.com: duplicata do mesmo operador (R4), cliente no mesmo endereço (R2) ou site novo
    for _, r in dccom.iterrows():
        base = {"key": r["key"], "fonte": r["fonte"], "name": r["name"], "company": r["company"],
                "operador": r["operador"], "city": r.get("city"), "latitude": r["latitude"],
                "longitude": r["longitude"], "stage": None, "mw_operacional": None,
                "mw_em_construcao": None, "mw_planejado": None, "mw_campo": None}
        ov = overrides.get(r["key"])
        if ov:
            rows.append({**base, "papel": ov["papel"], "facility_id": ov.get("host") or ov.get("facility"),
                         "regra": ov["motivo"].split(" ")[0], "justificativa": ov["motivo"]})
            continue
        dist = _haversine_km(r["latitude"], r["longitude"], sites["latitude"].values, sites["longitude"].values)
        same = (sites["operador"] == r["operador"]).values & (dist <= same_km)
        if same.any():
            i = int(np.argmin(np.where(same, dist, np.inf)))
            rows.append({**base, "papel": "duplicata", "facility_id": sites.iloc[i]["facility_id"], "regra": "R4",
                         "justificativa": f"mesmo operador a {dist[i]:.2f} km de {sites.iloc[i]['facility_id']}"})
            continue
        rows.append({**base, "papel": "site", "facility_id": None, "regra": "R1",
                     "justificativa": "só no datacenters.com"})

    table = pd.DataFrame(rows)

    # R2 automático: site novo do datacenters.com no mesmo endereço de um site de OUTRO operador.
    coinc_km = float(cfg.get("coincident_km", 0.25))
    all_sites = table[table["papel"] == "site"]
    for idx in table.index[(table["papel"] == "site") & table["facility_id"].isna()]:
        r = table.loc[idx]
        others = all_sites[(all_sites["operador"] != r["operador"]) & all_sites["facility_id"].notna()]
        dist = _haversine_km(r["latitude"], r["longitude"], others["latitude"].values, others["longitude"].values)
        # nan*: sites sem coordenadas não podem anular a comparação.
        if np.isfinite(dist).any() and np.nanmin(dist) <= coinc_km:
            host = others.iloc[int(np.nanargmin(dist))]
            table.loc[idx, ["papel", "facility_id", "regra", "justificativa"]] = [
                "cliente_colocation", host["facility_id"], "R2",
                f"mesmo endereço de {host['facility_id']} ({host['operador']}), {np.nanmin(dist):.3f} km"]

    # Sites novos do datacenters.com que coincidem entre si (ex.: vários revendedores no mesmo prédio sem
    # site no datacentermap) ficam como sites distintos, marcados para revisão.
    new = table.index[(table["papel"] == "site") & table["facility_id"].isna()]
    for idx in new:
        table.loc[idx, "facility_id"] = "dccom-" + re.sub(r"[^a-z0-9]+", "-", _norm(table.loc[idx, "key"][6:]))[:60]
    return table


def _reverse_geocode(lat: float, lon: float) -> dict:
    """Concelho/distrito por coordenadas (Nominatim, zoom 10 = município). Política: 1 req/s, User-Agent."""
    params = {"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1, "accept-language": "pt"}
    for attempt in range(3):
        try:
            resp = requests.get(NOMINATIM_REVERSE, params=params, timeout=30, headers={"User-Agent": USER_AGENT})
            if resp.ok:
                addr = resp.json().get("address") or {}
                concelho = addr.get("municipality") or addr.get("city") or addr.get("town") or addr.get("village")
                return {"concelho": concelho, "distrito": addr.get("county")} if concelho else {}
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return {}


def _geocode(sites: pd.DataFrame, slug: str, fallback: dict) -> pd.DataFrame:
    """R5: concelho/distrito pelas coordenadas (Nominatim), com cache local (só respostas válidas).
    Sem coordenadas → `concelho_por_cidade` do YAML."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"reverse_geocode_{slug}.json"
    cache = json.loads(cache_path.read_text()) if cache_path.is_file() else {}
    concelhos: list[str | None] = []
    distritos: list[str | None] = []
    for _, r in sites.iterrows():
        if pd.isna(r["latitude"]) or pd.isna(r["longitude"]):
            concelhos.append(fallback.get(str(r.get("city"))))
            distritos.append(None)
            continue
        k = f"{r['latitude']:.6f},{r['longitude']:.6f}"
        if not cache.get(k, {}).get("concelho"):
            cache[k] = _reverse_geocode(r["latitude"], r["longitude"])
            time.sleep(1.1)
        concelhos.append(cache[k].get("concelho"))
        distritos.append(cache[k].get("distrito"))
    cache_path.write_text(json.dumps({k: v for k, v in cache.items() if v}, ensure_ascii=False, indent=1))
    return sites.assign(concelho=concelhos, distrito=distritos)


def _apply_dccom_details(table: pd.DataFrame, slug: str) -> pd.DataFrame:
    """Sites só do datacenters.com: status e MW do texto da página de detalhe (mw_campo = dccom_texto)."""
    path = PROCESSED_DIR / f"datacenters_com_profiles_{slug}.csv"
    if not path.is_file():
        return table
    det = pd.read_csv(path)
    det["key"] = "dccom:" + det["name"].astype(str) + "|" + det["provider"].astype(str)
    det = det.set_index("key")
    table["certificacoes"] = pd.Series(pd.NA, index=table.index, dtype="object")
    table["mw_campo"] = table["mw_campo"].astype("object")
    only = (table["fonte"] == "datacenters.com") & (table["papel"] == "site")
    for idx in table.index[only]:
        k = table.loc[idx, "key"]
        if k not in det.index:
            continue
        status = str(det.loc[k, "status_texto"] or "").lower()
        if "operational" in status:
            table.loc[idx, "stage"] = 2
            mw = _num(det.loc[k, "mw_primeiro"])
            if mw is not None:
                table.loc[idx, ["mw_operacional", "mw_campo"]] = [mw, "dccom_texto"]
        table.loc[idx, "certificacoes"] = det.loc[k, "certificacoes"]
    return table


def build_sites(table: pd.DataFrame, dcmap: pd.DataFrame, slug: str, cfg: dict) -> pd.DataFrame:
    nuts2 = cfg.get("nuts2") or {}
    table = _apply_dccom_details(table, slug)
    counted = table[table["papel"].isin(["site", "predio", "duplicata"])]
    site_rows = table[table["papel"] == "site"].set_index("facility_id")
    tenants = table[table["papel"] == "cliente_colocation"].groupby("facility_id")["operador"].apply(
        lambda s: "; ".join(sorted(set(s))))
    pue = dcmap.set_index("link")["power_pue"] if "power_pue" in dcmap else pd.Series(dtype=float)
    # Coordenada repetida em registros de operadores diferentes no datacenters.com = centróide da cidade.
    dc = table[table["fonte"] == "datacenters.com"]
    coord_key = dc["latitude"].round(5).astype(str) + "," + dc["longitude"].round(5).astype(str)
    shared = coord_key.groupby(coord_key).transform(lambda s: dc.loc[s.index, "operador"].nunique() > 1)
    approx_keys = set(dc.loc[shared[shared].index, "key"])

    out = []
    for fid, grp in counted.groupby("facility_id"):
        head = site_rows.loc[fid] if fid in site_rows.index else grp.iloc[0]
        bld = grp[grp["papel"] == "predio"]
        mw_src = bld if bld[["mw_operacional", "mw_em_construcao", "mw_planejado"]].notna().any().any() else grp[grp["papel"] == "site"]
        stages = pd.to_numeric(grp["stage"], errors="coerce").dropna()
        status = STAGE_LABEL.get(int(stages.max()), "desconhecido") if len(stages) else "desconhecido"
        out.append({
            "facility_id": fid,
            "nome": head["name"],
            "operador": head["operador"],
            "latitude": head["latitude"],
            "longitude": head["longitude"],
            "status": status,
            "n_predios": len(bld),
            "mw_operacional": mw_src["mw_operacional"].sum(min_count=1),
            "mw_em_construcao": mw_src["mw_em_construcao"].sum(min_count=1),
            "mw_planejado": mw_src["mw_planejado"].sum(min_count=1),
            "mw_campo": "; ".join(sorted(set(mw_src["mw_campo"].dropna()))),
            "pue_declarado": _num(pue.get(fid)) if fid in pue.index else None,
            "certificacoes": "; ".join(sorted(set(grp["certificacoes"].dropna()))) if "certificacoes" in grp else "",
            "fontes": "; ".join(sorted(set(grp["fonte"]))),
            "clientes_colocation": tenants.get(fid, ""),
            "localizacao_aproximada": head["key"] in approx_keys,
            "city": head["city"],
        })
    sites = _geocode(pd.DataFrame(out), slug, cfg.get("concelho_por_cidade") or {})
    sites["concelho"] = sites["concelho"].replace(cfg.get("concelho_correcoes") or {})
    for fid, fix in (cfg.get("localizacao_correcoes") or {}).items():
        mask = sites["facility_id"] == fid
        sites.loc[mask, "concelho"] = fix["concelho"]
        sites.loc[mask, "localizacao_nota"] = fix["motivo"]
    missing = sorted(set(sites["concelho"].dropna()) - set(nuts2))
    if missing:
        raise SystemExit(f"[curate] concelho(s) sem NUTS II em curation_{slug}.yaml: {missing}")
    sites["nuts2"] = sites["concelho"].map(nuts2)
    return sites.sort_values(["nuts2", "operador", "nome"]).reset_index(drop=True)


def main() -> None:
    slug = _country_slug()
    cfg = yaml.safe_load((PROJECT_ROOT / "config" / f"curation_{slug}.yaml").read_text(encoding="utf-8"))
    aliases = cfg.get("aliases") or {}

    def canon(name: object) -> str:
        s = str(name or "").strip()
        return str(aliases.get(s, s))

    dcmap = _dcmap_records(slug, canon)
    dccom = _dccom_records(slug, canon)
    table = curate(dcmap, dccom, cfg)
    sites = build_sites(table, dcmap, slug, cfg)

    table.to_csv(PROCESSED_DIR / f"curation_table_{slug}.csv", index=False, encoding="utf-8-sig")
    sites.to_csv(PROCESSED_DIR / f"facilities_curated_{slug}.csv", index=False, encoding="utf-8-sig")
    print(f"[curate] registros: {len(table)} → papéis {table['papel'].value_counts().to_dict()}")
    print(f"[curate] sites: {len(sites)} → status {sites['status'].value_counts().to_dict()}")
    print(f"[curate] saved curation_table_{slug}.csv e facilities_curated_{slug}.csv")


if __name__ == "__main__":
    main()
