# Copyright (c) 2025-2026 Giovanni Della Dea
"""
apply_enrichment.py
Joins the web enrichment (per-batch JSON with field-level provenance) to the curated facilities dataset.

Rules:
- a field without `fonte_url` or `citacao` is discarded (provenance is mandatory);
- only `confianca` alta/media is applied; everything stays in the long table for validation;
- values from the directories are never overwritten; the web only fills gaps;
- every filled cell gets a `<coluna>_fonte` = "web:<tipo_fonte>:<confianca>:<url>".

Inputs:  data/curation/enrichment_<country>/*_resultado.json, data/processed/facilities_curated_<country>.csv
Outputs: data/processed/enrichment_long_<country>.csv, data/processed/facilities_enriched_<country>.csv
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
import yaml

_PROJECT_FALLBACK = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_PROJECT_FALLBACK))

from scripts.lib.workspace_root import resolve_project_root

PROJECT_ROOT = resolve_project_root(_PROJECT_FALLBACK)
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CURATION_DIR = PROJECT_ROOT / "data" / "curation"

APPLY_CONFIDENCE = {"alta", "media"}
STATUS_VALUES = ("operacional", "em_construcao", "planejado", "encerrado")

# campo do enriquecimento → (coluna do dataset, tipo)
FIELD_MAP: dict[str, tuple[str, str]] = {
    "status": ("status", "status"),
    "mw_ti_instalado": ("mw_operacional", "num"),
    "mw_ti_planejado": ("mw_planejado_anunciado", "num"),
    "pue": ("pue_declarado", "num"),
    "controlador_final": ("controlador_final", "text"),
    "controlador_final_pais": ("controlador_pais", "text"),
    "ano_operacao": ("ano_operacao", "num"),
    "area_m2": ("area_m2", "num"),
}

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def flatten_enrichment(results: list[dict]) -> pd.DataFrame:
    """JSON por lote → tabela longa (uma linha por site × campo), só com campos que têm fonte e citação."""
    rows = []
    for res in results:
        for site in res.get("sites", []):
            for campo, v in (site.get("campos") or {}).items():
                if not isinstance(v, dict) or not v.get("fonte_url") or not v.get("citacao"):
                    continue
                rows.append({"lote": res.get("lote"), "facility_id": site["facility_id"], "campo": campo,
                             "valor": v.get("valor"), "unidade": v.get("unidade"), "fonte_url": v["fonte_url"],
                             "citacao": v["citacao"], "data_acesso": v.get("data_acesso"),
                             "tipo_fonte": v.get("tipo_fonte"), "confianca": v.get("confianca")})
    cols = ["lote", "facility_id", "campo", "valor", "unidade", "fonte_url", "citacao", "data_acesso",
            "tipo_fonte", "confianca"]
    return pd.DataFrame(rows, columns=cols)


def _to_number(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    m = _NUM_RE.search(str(value or ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _to_status(value) -> str | None:
    text = str(value or "").strip().lower()
    return next((s for s in STATUS_VALUES if text.startswith(s)), None)


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA


def apply_enrichment(sites: pd.DataFrame, enrichment: pd.DataFrame) -> pd.DataFrame:
    out = sites.copy()
    for col, _ in FIELD_MAP.values():
        if col not in out.columns:
            out[col] = pd.NA
        out[f"{col}_fonte"] = pd.Series(pd.NA, index=out.index, dtype="object")
        if out[col].dtype != "object":
            out[col] = out[col].astype("object")
    out["enriquecido_web"] = False

    usable = enrichment[enrichment["confianca"].isin(APPLY_CONFIDENCE) & enrichment["campo"].isin(FIELD_MAP)]
    for idx, site in out.iterrows():
        for r in usable[usable["facility_id"] == site["facility_id"]].itertuples():
            col, kind = FIELD_MAP[r.campo]
            current = out.at[idx, col]
            value: float | str | None
            if kind == "status":
                if current != "desconhecido":
                    continue
                value = _to_status(r.valor)
            elif not _is_missing(current):
                continue
            else:
                value = _to_number(r.valor) if kind == "num" else str(r.valor)
            if value is None:
                continue
            out.at[idx, col] = value
            out.at[idx, f"{col}_fonte"] = f"web:{r.tipo_fonte}:{r.confianca}:{r.fonte_url}"
            out.at[idx, "enriquecido_web"] = True
    return out


def _country_slug() -> str:
    with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
        return str((yaml.safe_load(f).get("country") or {})["slug"])


def main() -> None:
    slug = _country_slug()
    files = sorted((CURATION_DIR / f"enrichment_{slug}").glob("*_resultado.json"))
    results = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    long = flatten_enrichment(results)
    sites = pd.read_csv(PROCESSED_DIR / f"facilities_curated_{slug}.csv")
    enriched = apply_enrichment(sites, long)
    long.to_csv(PROCESSED_DIR / f"enrichment_long_{slug}.csv", index=False, encoding="utf-8-sig")
    enriched.to_csv(PROCESSED_DIR / f"facilities_enriched_{slug}.csv", index=False, encoding="utf-8-sig")
    print(f"[enrichment] lotes: {[p.stem for p in files]}")
    print(f"[enrichment] campos com fonte: {len(long)} | sites enriquecidos: {int(enriched['enriquecido_web'].sum())}/{len(enriched)}")


if __name__ == "__main__":
    main()
