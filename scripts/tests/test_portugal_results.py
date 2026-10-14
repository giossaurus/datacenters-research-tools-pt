"""Testes das agregações dos Results de Portugal (Fig. 1, Fig. 2 e Tabela 1)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from scripts.pipeline.portugal_results import (
    NUTS2_2024_PT,
    concentration_summary,
    controller_origin,
    hhi,
    operator_table,
    region_summary,
)

NAN = float("nan")


def _sites() -> pd.DataFrame:
    rows = [
        # facility_id, operador, nuts2, status, mw_operacional
        ("a1", "Alfa", "Grande Lisboa", "operacional", 10.0),
        ("a2", "Alfa", "Norte", "operacional", NAN),
        ("b1", "Beta", "Grande Lisboa", "operacional", 30.0),
        ("c1", "Gama", "Alentejo", "em_construcao", 0.0),
        ("d1", "Delta", "Norte", "desconhecido", NAN),
    ]
    df = pd.DataFrame(rows, columns=["facility_id", "operador", "nuts2", "status", "mw_operacional"])
    df["mw_planejado_anunciado"] = [NAN, NAN, 50.0, 180.0, NAN]
    return df


ORIGIN = {
    "Alfa": {"pais": "PT", "categoria": "publico_pt"},
    "Beta": {"pais": "US", "categoria": "estrangeiro"},
    "Gama": {"pais": "ES", "categoria": "estrangeiro"},
    "Delta": {"pais": "PT", "categoria": "privado_pt"},
}


class TestHHI:
    def test_monopolio_vale_10000(self):
        assert hhi([5]) == pytest.approx(10000)

    def test_partes_iguais(self):
        assert hhi([1, 1, 1, 1]) == pytest.approx(2500)

    def test_ignora_nan_e_zero(self):
        assert hhi([1, 1, NAN, 0]) == pytest.approx(5000)

    def test_sem_dados_devolve_nan(self):
        assert math.isnan(hhi([NAN, 0]))


class TestControllerOrigin:
    def test_mapeia_pais_e_categoria_por_operador(self):
        out = controller_origin(_sites(), ORIGIN)
        assert list(out["controlador_iso"]) == ["PT", "PT", "US", "ES", "PT"]
        assert list(out["controlador_categoria"]) == [
            "publico_pt", "publico_pt", "estrangeiro", "estrangeiro", "privado_pt"]

    def test_operador_sem_mapeamento_falha_alto(self):
        with pytest.raises(KeyError, match="Delta"):
            controller_origin(_sites(), {k: v for k, v in ORIGIN.items() if k != "Delta"})


class TestRegionSummary:
    def test_inclui_todas_as_regioes_nuts2_mesmo_sem_sites(self):
        out = region_summary(_sites())
        assert list(out["nuts2"]) == list(NUTS2_2024_PT)
        assert out.set_index("nuts2").loc["Algarve", "n_sites"] == 0

    def test_conta_por_estagio_e_soma_mw_declarado(self):
        out = region_summary(_sites()).set_index("nuts2")
        assert out.loc["Grande Lisboa", "n_sites"] == 2
        assert out.loc["Norte", "n_operacional"] == 1
        assert out.loc["Alentejo", "n_em_construcao"] == 1
        assert out.loc["Grande Lisboa", "mw_operacional"] == pytest.approx(40.0)
        assert out.loc["Norte", "n_com_mw"] == 0

    def test_partes_em_percentual(self):
        out = region_summary(_sites()).set_index("nuts2")
        assert out.loc["Grande Lisboa", "pct_sites"] == pytest.approx(40.0)
        assert out.loc["Grande Lisboa", "pct_mw"] == pytest.approx(100.0)
        assert out["pct_sites"].sum() == pytest.approx(100.0)

    def test_exclusao_para_sensibilidade(self):
        out = region_summary(_sites(), exclude=["d1"]).set_index("nuts2")
        assert out.loc["Norte", "n_sites"] == 1
        assert out["n_sites"].sum() == 4

    def test_regiao_fora_da_lista_falha_alto(self):
        bad = _sites()
        bad.loc[0, "nuts2"] = "Lisboa"
        with pytest.raises(ValueError, match="Lisboa"):
            region_summary(bad)


class TestOperatorTable:
    def test_uma_linha_por_operador_ordenada_por_sites(self):
        out = operator_table(_sites(), ORIGIN)
        assert out.iloc[0]["operador"] == "Alfa"
        assert len(out) == 4
        alfa = out.iloc[0]
        assert alfa["n_sites"] == 2
        assert alfa["n_operacional"] == 2
        assert alfa["mw_operacional"] == pytest.approx(10.0)
        assert alfa["regioes"] == "Norte; Grande Lisboa"  # ordem oficial NUTS (norte → sul)
        assert alfa["controlador_iso"] == "PT"

    def test_soma_mw_anunciado_separado_do_operacional(self):
        out = operator_table(_sites(), ORIGIN).set_index("operador")
        assert out.loc["Gama", "mw_planejado_anunciado"] == pytest.approx(180.0)
        assert out.loc["Beta", "mw_planejado_anunciado"] == pytest.approx(50.0)
        assert math.isnan(out.loc["Alfa", "mw_planejado_anunciado"])

    def test_mw_ausente_fica_nan_e_nao_zero(self):
        out = operator_table(_sites(), ORIGIN).set_index("operador")
        assert math.isnan(out.loc["Delta", "mw_operacional"])
        assert out.loc["Gama", "mw_operacional"] == pytest.approx(0.0)


class TestConcentrationSummary:
    def test_hhi_por_sites_e_por_mw(self):
        s = concentration_summary(_sites(), ORIGIN)
        # sites: Alfa 2, Beta 1, Gama 1, Delta 1 → 40² + 20²·3 = 2800
        assert s["hhi_sites"] == pytest.approx(2800)
        # MW declarado: Alfa 10, Beta 30 → 25² + 75² = 6250
        assert s["hhi_mw"] == pytest.approx(6250)
        assert s["n_operadores"] == 4

    def test_parte_estrangeira(self):
        s = concentration_summary(_sites(), ORIGIN)
        assert s["pct_sites_estrangeiro"] == pytest.approx(40.0)
        assert s["pct_mw_estrangeiro"] == pytest.approx(75.0)
        assert s["n_sites_com_mw"] == 3
