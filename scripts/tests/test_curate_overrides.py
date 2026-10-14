"""Testes das substituições (overrides) declaradas em config/curation_<país>.yaml."""

from __future__ import annotations

import pandas as pd

from scripts.pipeline.curate_facilities import curate


def _dcmap(stage: int = 0) -> pd.DataFrame:
    return pd.DataFrame([{
        "key": "dcmap:site-x", "fonte": "datacentermap", "link": "site-x", "name": "Site X", "company": "Op",
        "operador": "Op", "city": "Guimarães", "latitude": 41.45, "longitude": -8.29, "stage": stage,
        "parent_link": None,
    }])


def _dccom_vazio() -> pd.DataFrame:
    return pd.DataFrame(columns=["key", "fonte", "name", "company", "operador", "city", "latitude", "longitude"])


def test_override_de_stage_muda_so_o_estagio_e_mantem_o_site():
    cfg = {"overrides": {"dcmap:site-x": {"stage": 1, "motivo": "R3 — imprensa 2026: em construção"}}}
    row = curate(_dcmap(stage=0), _dccom_vazio(), cfg).iloc[0]
    assert row["papel"] == "site"
    assert row["facility_id"] == "site-x"
    assert row["stage"] == 1
    assert row["regra"] == "R3"
    assert "imprensa 2026" in row["justificativa"]


def test_override_de_papel_continua_funcionando():
    cfg = {"overrides": {"dcmap:site-x": {"papel": "excluido", "motivo": "Escopo — teste"}}}
    row = curate(_dcmap(), _dccom_vazio(), cfg).iloc[0]
    assert row["papel"] == "excluido"


def test_sem_override_usa_o_stage_da_fonte():
    row = curate(_dcmap(stage=2), _dccom_vazio(), {}).iloc[0]
    assert row["stage"] == 2
    assert row["papel"] == "site"
