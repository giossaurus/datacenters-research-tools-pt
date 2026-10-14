# Copyright (c) 2025-2026 Giovanni Della Dea
"""
portugal_results.py
Aggregations, figures and table for the Portugal national mapping (IHE2026 paper, Results).

- Fig. 1: NUTS II 2024 choropleth (number of sites) with the sites on top (stage, declared IT MW).
- Fig. 2: share of sites vs share of declared operational IT MW, per NUTS II region.
- Table 1: operators, controller country and type, sites and declared MW; concentration (HHI) summary.

The unit of analysis is the site (curation rule R1). MW is only what operators or directories declare:
missing MW stays missing, it is never read as zero.

Inputs:  data/processed/facilities_enriched_portugal.csv, config/controller_origin_portugal.yaml,
         config/curation_portugal.yaml (sensibilidade_evidencia_fraca), GISCO NUTS 2024 boundaries (cached)
Outputs: data/processed/{region_summary,operator_table,concentration}_portugal.*,
         data/processed/fig{1,2}_*_portugal.{png,svg}, data/processed/table1_operators_portugal.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_PROJECT_FALLBACK = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_PROJECT_FALLBACK))

from scripts.lib.workspace_root import resolve_project_root

PROJECT_ROOT = resolve_project_root(_PROJECT_FALLBACK)
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
CONFIG_DIR = PROJECT_ROOT / "config"
GISCO_CACHE = PROJECT_ROOT / "data" / "cache" / "gisco"
GISCO_URL = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_03M_2024_4326_LEVL_2.geojson"

# NUTS II 2024 (Regulamento Delegado (UE) 2023/674), rótulo do dataset → código NUTS
NUTS2_2024_PT: dict[str, str] = {
    "Norte": "PT11",
    "Centro": "PT19",
    "Oeste e Vale do Tejo": "PT1D",
    "Grande Lisboa": "PT1A",
    "Península de Setúbal": "PT1B",
    "Alentejo": "PT1C",
    "Algarve": "PT15",
    "Região Autónoma dos Açores": "PT20",
    "Região Autónoma da Madeira": "PT30",
}

COUNTRY_EN = {"PT": "Portugal", "US": "United States", "GB": "United Kingdom", "ES": "Spain", "LU": "Luxembourg",
              "FR": "France", "IN": "India"}
CATEGORY_EN = {"publico_pt": "Public (PT)", "privado_pt": "Private (PT)", "estrangeiro": "Foreign"}


def hhi(values: Iterable[float]) -> float:
    """Herfindahl–Hirschman index on 0–10 000, from raw magnitudes (NaN and zero are ignored)."""
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if arr.size == 0:
        return float("nan")
    shares = 100 * arr / arr.sum()
    return float((shares**2).sum())


def controller_origin(sites: pd.DataFrame, origin: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    """Adds controlador_iso / controlador_categoria by operator; an unmapped operator is an error."""
    missing = sorted(set(sites["operador"]) - set(origin))
    if missing:
        raise KeyError(f"operadores sem controlador mapeado: {missing}")
    out = sites.copy()
    out["controlador_iso"] = out["operador"].map(lambda op: origin[op]["pais"])
    out["controlador_categoria"] = out["operador"].map(lambda op: origin[op]["categoria"])
    return out


def _check_regions(sites: pd.DataFrame) -> None:
    unknown = sorted(set(sites["nuts2"].dropna()) - set(NUTS2_2024_PT))
    if unknown:
        raise ValueError(f"regiões fora da NUTS II 2024: {unknown}")


def region_summary(sites: pd.DataFrame, exclude: Iterable[str] = ()) -> pd.DataFrame:
    """One row per NUTS II 2024 region (all nine, in fixed order), with counts by stage and declared MW."""
    _check_regions(sites)
    s = sites[~sites["facility_id"].isin(list(exclude))]
    g = s.groupby("nuts2")
    out = pd.DataFrame({
        "n_sites": g.size(),
        "n_operacional": g["status"].apply(lambda x: int((x == "operacional").sum())),
        "n_em_construcao": g["status"].apply(lambda x: int((x == "em_construcao").sum())),
        "n_desconhecido": g["status"].apply(lambda x: int((x == "desconhecido").sum())),
        "n_com_mw": g["mw_operacional"].apply(lambda x: int(x.notna().sum())),
        "mw_operacional": g["mw_operacional"].sum(min_count=1),
    }).reindex(list(NUTS2_2024_PT))
    count_cols = ["n_sites", "n_operacional", "n_em_construcao", "n_desconhecido", "n_com_mw"]
    out[count_cols] = out[count_cols].fillna(0).astype(int)
    out["mw_operacional"] = out["mw_operacional"].fillna(0.0)
    out["pct_sites"] = 100 * out["n_sites"] / out["n_sites"].sum()
    total_mw = out["mw_operacional"].sum()
    out["pct_mw"] = 100 * out["mw_operacional"] / total_mw if total_mw else np.nan
    out.insert(0, "nuts_id", [NUTS2_2024_PT[r] for r in out.index])
    return out.rename_axis("nuts2").reset_index()


def operator_table(sites: pd.DataFrame, origin: Mapping[str, Mapping[str, Any]]) -> pd.DataFrame:
    """One row per operator: sites (total/operational), declared operational MW, regions, controller."""
    s = controller_origin(sites, origin)
    g = s.groupby("operador")
    out = pd.DataFrame({
        "n_sites": g.size(),
        "n_operacional": g["status"].apply(lambda x: int((x == "operacional").sum())),
        "mw_operacional": g["mw_operacional"].sum(min_count=1),
        "mw_planejado_anunciado": g["mw_planejado_anunciado"].sum(min_count=1),
        "regioes": g["nuts2"].apply(lambda x: "; ".join(r for r in NUTS2_2024_PT if r in set(x))),
        "controlador_iso": g["controlador_iso"].first(),
        "controlador_categoria": g["controlador_categoria"].first(),
    })
    out = out.rename_axis("operador").reset_index()
    out = out.sort_values(["n_sites", "mw_operacional", "operador"], ascending=[False, False, True],
                          na_position="last")
    return out.reset_index(drop=True)


def concentration_summary(sites: pd.DataFrame, origin: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """HHI by sites and by declared MW (operators), and the foreign-controlled share of each."""
    s = controller_origin(sites, origin)
    by_op = s.groupby("operador")
    foreign = s["controlador_categoria"] == "estrangeiro"
    mw = s["mw_operacional"]
    total_mw = mw.sum(skipna=True)
    top4 = by_op.size().sort_values(ascending=False).head(4).sum()
    return {
        "n_sites": int(len(s)),
        "n_operadores": int(by_op.ngroups),
        "hhi_sites": hhi(by_op.size()),
        "hhi_sites_operacional": hhi(s[s["status"] == "operacional"].groupby("operador").size()),
        "hhi_mw": hhi(by_op["mw_operacional"].sum(min_count=1)),
        "cr4_sites_pct": float(100 * top4 / len(s)),
        "n_sites_com_mw": int(mw.notna().sum()),
        "mw_operacional_total": float(total_mw),
        "pct_sites_estrangeiro": float(100 * foreign.mean()),
        "pct_mw_estrangeiro": float(100 * mw[foreign].sum() / total_mw) if total_mw else float("nan"),
        "sites_por_categoria": {k: int(v) for k, v in s["controlador_categoria"].value_counts().items()},
        "sites_por_pais": {k: int(v) for k, v in s["controlador_iso"].value_counts().items()},
    }


# ── saídas ────────────────────────────────────────────────────────────────────

def _nuts_geometries():
    import geopandas as gpd
    import requests

    path = GISCO_CACHE / Path(GISCO_URL).name
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(GISCO_URL, timeout=60)
        resp.raise_for_status()
        path.write_bytes(resp.content)
    nuts = gpd.read_file(path)
    return nuts[nuts["CNTR_CODE"] == "PT"]


def _style():
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9, "axes.spines.top": False,
                         "axes.spines.right": False, "svg.fonttype": "none"})
    return plt


_STAGE_MARKERS = {"operacional": ("o", "#c0392b", "Operational"),
                  "em_construcao": ("^", "#2471a3", "Under construction"),
                  "desconhecido": ("x", "#555555", "Status unknown")}
_LISBON_BOX = (-9.55, -8.75, 38.45, 39.05)  # lon_min, lon_max, lat_min, lat_max (inset)


def _marker_size(mw: pd.Series) -> np.ndarray:
    """Area ∝ declared operational IT MW; sites without a declared value get the minimum size."""
    v = mw.fillna(0).clip(lower=0).to_numpy(dtype=float)
    return np.asarray(10 + 9 * np.sqrt(v), dtype=float)


def _plot_sites(ax, pts: pd.DataFrame) -> None:
    for status, (m, colour, _label) in _STAGE_MARKERS.items():
        sub = pts[pts["status"] == status]
        style = {"color": colour} if m == "x" else {"facecolors": "none", "edgecolors": colour}
        ax.scatter(sub["longitude"], sub["latitude"], s=_marker_size(sub["mw_operacional"]), marker=m,
                   linewidths=0.9, zorder=3, **style)


def _label_regions(ax, polys, skip: Iterable[str] = ()) -> None:
    for _, r in polys.iterrows():
        if r["NUTS_ID"] in set(skip):
            continue
        p = r.geometry.representative_point()
        ax.annotate(f"{r['NAME_LATN'].replace(' (PT)', '')}\n{r['n_sites']}", (p.x, p.y), ha="center",
                    va="center", fontsize=6.5, color="#222")


def figure_map(sites: pd.DataFrame, regions: pd.DataFrame, out_stem: Path) -> None:
    """Fig. 1: mainland NUTS II choropleth (sites per region) + sites; inset zooms on the Lisbon area."""
    plt = _style()
    from matplotlib.lines import Line2D
    from matplotlib.patches import Rectangle

    nuts = _nuts_geometries().merge(regions[["nuts_id", "n_sites"]], left_on="NUTS_ID", right_on="nuts_id")
    mainland = nuts[~nuts["NUTS_ID"].isin(["PT20", "PT30"])]
    vmax = regions["n_sites"].max() * 1.6
    pts = sites.dropna(subset=["latitude", "longitude"])

    fig, ax = plt.subplots(figsize=(5.4, 6.6))
    mainland.plot(ax=ax, column="n_sites", cmap="Greys", vmin=0, vmax=vmax, edgecolor="#555", linewidth=0.5)
    _label_regions(ax, mainland, skip=("PT1A", "PT1B"))
    _plot_sites(ax, pts)
    x0, x1, y0, y1 = _LISBON_BOX
    ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor="#333", linewidth=0.6, zorder=4))
    ax.set_xlim(-12.6, -6.1)
    ax.set_axis_off()

    ins = ax.inset_axes((0.0, 0.08, 0.42, 0.36))
    mainland.plot(ax=ins, column="n_sites", cmap="Greys", vmin=0, vmax=vmax, edgecolor="#555", linewidth=0.5)
    _plot_sites(ins, pts)
    ins.set_xlim(x0, x1)
    ins.set_ylim(y0, y1)
    for code, (lon, lat) in {"PT1A": (-9.5, 39.0), "PT1B": (-9.5, 38.5)}.items():
        r = mainland[mainland["NUTS_ID"] == code].iloc[0]
        ins.text(lon, lat, f"{r['NAME_LATN']} ({r['n_sites']})", fontsize=6, va="center", color="#222",
                 bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.8, "pad": 1})
    ins.set_xticks([])
    ins.set_yticks([])
    for spine in ins.spines.values():
        spine.set_visible(True)
        spine.set_linewidth(0.6)

    stage = [Line2D([], [], marker=m, linestyle="", markerfacecolor="none" if m != "x" else c,
                    markeredgecolor=c, label=lab) for m, c, lab in _STAGE_MARKERS.values()]
    size = [ax.scatter([], [], s=float(_marker_size(pd.Series([mw]))[0]), marker="o", facecolors="none",
                       edgecolors="#c0392b", label=f"{mw:g} MW") for mw in (1, 5, 25)]
    ax.legend(handles=stage + size, loc="upper left", bbox_to_anchor=(0.0, 0.98), fontsize=6.5, frameon=False,
              title="Declared operational IT MW\n(no value = smallest marker)", title_fontsize=6.5)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(out_stem.with_suffix(f".{ext}"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def figure_shares(regions: pd.DataFrame, out_stem: Path) -> None:
    """Fig. 2: share of sites vs share of declared operational IT MW, regions with at least one site."""
    plt = _style()
    r = regions[regions["n_sites"] > 0].iloc[::-1]
    y = np.arange(len(r))
    fig, ax = plt.subplots(figsize=(5.2, 3.0))
    ax.barh(y + 0.2, r["pct_sites"], height=0.38, color="#7f8c8d", label=f"Share of sites (n = {regions['n_sites'].sum()})")
    ax.barh(y - 0.2, r["pct_mw"], height=0.38, color="#c0392b",
            label=f"Share of declared operational IT MW ({regions['mw_operacional'].sum():.0f} MW, "
                  f"{regions['n_com_mw'].sum()} sites)")
    for yi, ps, pm in zip(y, r["pct_sites"], r["pct_mw"], strict=True):
        ax.text(ps + 1, yi + 0.2, f"{ps:.0f}%", va="center", fontsize=7)
        ax.text(pm + 1, yi - 0.2, f"{pm:.0f}%", va="center", fontsize=7)
    ax.set_yticks(y, [f"{n} ({k})" for n, k in zip(r["nuts2"], r["n_sites"], strict=True)])
    ax.set_xlabel("%")
    ax.set_xlim(0, max(r["pct_sites"].max(), r["pct_mw"].max()) * 1.15)
    ax.legend(loc="upper center", bbox_to_anchor=(0.4, -0.18), fontsize=7, frameon=False, ncol=1)
    fig.tight_layout()
    for ext in ("png", "svg"):
        fig.savefig(out_stem.with_suffix(f".{ext}"), dpi=300, bbox_inches="tight")
    plt.close(fig)


def table_markdown(ops: pd.DataFrame, origin: Mapping[str, Mapping[str, Any]]) -> str:
    lines = ["| Operator | Controller country | Type | Sites (operational / total) | Declared operational IT MW | Announced IT MW (full build-out) | NUTS II |",
             "|---|---|---|---|---|---|---|"]
    for _, r in ops.iterrows():
        mw = "—" if pd.isna(r["mw_operacional"]) else f"{r['mw_operacional']:.1f}"
        ann = "—" if pd.isna(r["mw_planejado_anunciado"]) else f"{r['mw_planejado_anunciado']:,.0f}"
        lines.append(f"| {r['operador']} | {COUNTRY_EN.get(r['controlador_iso'], r['controlador_iso'])} | "
                     f"{CATEGORY_EN[r['controlador_categoria']]} | {r['n_operacional']} / {r['n_sites']} | {mw} | {ann} | "
                     f"{r['regioes']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--processed-dir", type=Path, default=PROCESSED_DIR)
    ap.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    ap.add_argument("--no-figures", action="store_true")
    args = ap.parse_args(argv)

    sites = pd.read_csv(args.processed_dir / "facilities_enriched_portugal.csv")
    origin = yaml.safe_load((args.config_dir / "controller_origin_portugal.yaml").read_text(encoding="utf-8"))
    curation = yaml.safe_load((args.config_dir / "curation_portugal.yaml").read_text(encoding="utf-8"))
    weak = list((curation.get("sensibilidade_evidencia_fraca") or {}).keys())

    regions = region_summary(sites)
    regions_sens = region_summary(sites, exclude=weak)
    ops = operator_table(sites, origin)
    conc = concentration_summary(sites, origin)
    conc["sensibilidade_sem_evidencia_fraca"] = {
        "excluidos": weak,
        "pct_sites_grande_lisboa_norte": float(
            regions_sens.set_index("nuts2").loc[["Grande Lisboa", "Norte"], "pct_sites"].sum()),
        "concentracao": concentration_summary(sites[~sites["facility_id"].isin(weak)], origin),
    }
    conc["pct_sites_grande_lisboa_norte"] = float(regions.set_index("nuts2").loc[["Grande Lisboa", "Norte"],
                                                                                 "pct_sites"].sum())
    out = args.processed_dir
    regions.to_csv(out / "region_summary_portugal.csv", index=False)
    regions_sens.to_csv(out / "region_summary_sensibilidade_portugal.csv", index=False)
    ops.to_csv(out / "operator_table_portugal.csv", index=False)
    (out / "concentration_portugal.json").write_text(json.dumps(conc, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "table1_operators_portugal.md").write_text(table_markdown(ops, origin), encoding="utf-8")
    if not args.no_figures:
        figure_map(sites, regions, out / "fig1_map_nuts2_portugal")
        figure_shares(regions, out / "fig2_sites_vs_mw_portugal")
    print(regions.to_string(index=False))
    print(json.dumps({k: v for k, v in conc.items() if k != "sensibilidade_sem_evidencia_fraca"}, indent=2,
                     ensure_ascii=False))
    print("sensibilidade:", json.dumps(conc["sensibilidade_sem_evidencia_fraca"]["pct_sites_grande_lisboa_norte"]))


if __name__ == "__main__":
    main()
