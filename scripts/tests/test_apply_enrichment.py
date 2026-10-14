"""Testes da junção do enriquecimento web (subagentes) ao dataset curado."""

from __future__ import annotations

import pandas as pd
import pytest

from scripts.pipeline.apply_enrichment import apply_enrichment, flatten_enrichment


def _campo(valor, confianca="alta", unidade=None, fonte="https://ex.pt/a", citacao="trecho literal"):
    return {"valor": valor, "unidade": unidade, "fonte_url": fonte, "citacao": citacao,
            "data_acesso": "2026-10-06", "tipo_fonte": "operador", "confianca": confianca}


def _resultado(campos: dict, facility_id="site-a", lote="A_teste"):
    return {"lote": lote, "sites": [{"facility_id": facility_id, "campos": campos}]}


def _sites(**overrides):
    base = {"facility_id": "site-a", "status": "desconhecido", "mw_operacional": float("nan"),
            "pue_declarado": float("nan")}
    base.update(overrides)
    return pd.DataFrame([base])


class TestFlattenEnrichment:
    def test_gera_uma_linha_por_campo_com_proveniencia(self):
        long = flatten_enrichment([_resultado({"status": _campo("operacional"), "mw_ti_instalado": _campo(26, unidade="MW")})])
        assert set(long["campo"]) == {"status", "mw_ti_instalado"}
        row = long[long["campo"] == "mw_ti_instalado"].iloc[0]
        assert row["facility_id"] == "site-a"
        assert row["lote"] == "A_teste"
        assert row["fonte_url"] == "https://ex.pt/a"
        assert row["citacao"] == "trecho literal"

    def test_descarta_campo_sem_fonte_ou_sem_citacao(self):
        long = flatten_enrichment([_resultado({
            "status": _campo("operacional", fonte=""),
            "pue": _campo(1.2, citacao=None),
            "area_m2": _campo(1000),
        })])
        assert list(long["campo"]) == ["area_m2"]

    def test_ignora_campos_que_nao_sao_objetos(self):
        long = flatten_enrichment([_resultado({"status": "operacional", "area_m2": _campo(10)})])
        assert list(long["campo"]) == ["area_m2"]


class TestApplyEnrichment:
    def test_preenche_lacuna_e_registra_a_fonte(self):
        long = flatten_enrichment([_resultado({"mw_ti_instalado": _campo(26, unidade="MW")})])
        out = apply_enrichment(_sites(), long)
        assert out.loc[0, "mw_operacional"] == pytest.approx(26.0)
        assert out.loc[0, "mw_operacional_fonte"].startswith("web:operador:alta")

    def test_nunca_sobrescreve_valor_dos_diretorios(self):
        long = flatten_enrichment([_resultado({"mw_ti_instalado": _campo(30, unidade="MW")})])
        out = apply_enrichment(_sites(mw_operacional=5.1), long)
        assert out.loc[0, "mw_operacional"] == pytest.approx(5.1)
        assert pd.isna(out.loc[0, "mw_operacional_fonte"])

    def test_ignora_confianca_baixa(self):
        long = flatten_enrichment([_resultado({"status": _campo("operacional", confianca="baixa")})])
        out = apply_enrichment(_sites(), long)
        assert out.loc[0, "status"] == "desconhecido"

    def test_status_so_substitui_desconhecido_e_normaliza_texto_livre(self):
        long = flatten_enrichment([_resultado({"status": _campo("operacional, em expansão (5 M€ em 2026)")})])
        assert apply_enrichment(_sites(), long).loc[0, "status"] == "operacional"
        assert apply_enrichment(_sites(status="planejado"), long).loc[0, "status"] == "planejado"

    def test_converte_numero_com_virgula_ou_prefixo(self):
        long = flatten_enrichment([_resultado({"pue": _campo("<1,3"), "mw_ti_instalado": _campo("1.085 MW")})])
        out = apply_enrichment(_sites(), long)
        assert out.loc[0, "pue_declarado"] == pytest.approx(1.3)
        assert out.loc[0, "mw_operacional"] == pytest.approx(1.085)

    def test_mw_planejado_anunciado_fica_em_coluna_propria(self):
        long = flatten_enrichment([_resultado({"mw_ti_planejado": _campo(1200, unidade="MW")})])
        out = apply_enrichment(_sites(mw_planejado=1060.0), long)
        assert out.loc[0, "mw_planejado"] == pytest.approx(1060.0)
        assert out.loc[0, "mw_planejado_anunciado"] == pytest.approx(1200.0)

    def test_controlador_final_vira_texto(self):
        long = flatten_enrichment([_resultado({"controlador_final": _campo("Equinix, Inc. (EUA)")})])
        assert apply_enrichment(_sites(), long).loc[0, "controlador_final"] == "Equinix, Inc. (EUA)"

    def test_site_sem_enriquecimento_fica_intacto(self):
        long = flatten_enrichment([_resultado({"status": _campo("operacional")}, facility_id="outro")])
        out = apply_enrichment(_sites(), long)
        assert out.loc[0, "status"] == "desconhecido"
        assert bool(out.loc[0, "enriquecido_web"]) is False
