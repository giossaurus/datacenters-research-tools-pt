# Copyright (c) 2025-2026 Giovanni Della Dea
"""
extract_operator_documents.py
Deterministic (regex-only) extraction of infrastructure / sustainability signals from the PDFs
collected by fetch_operator_documents.py. Deterministic: every hit carries document, page and the text
snippet so it can be validated by hand.

Signals: power (MW/GW), PUE, WUE, renewable share, water/cooling, certifications, named facilities.

Input:  data/processed/operator_documents_manifest_<country>.csv (+ PDFs under data/documents/)
Output: data/processed/operator_documents_signals_<country>.csv   (one row per hit)
        data/processed/operator_documents_summary_<country>.csv   (one row per document, relevance)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import yaml
from pypdf import PdfReader

_PROJECT_FALLBACK = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_PROJECT_FALLBACK))

from scripts.lib.workspace_root import resolve_project_root

PROJECT_ROOT = resolve_project_root(_PROJECT_FALLBACK)
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

_NUM = r"(\d{1,4}(?:[.,]\d{1,3})?)"
SIGNALS: dict[str, re.Pattern[str]] = {
    "potencia_mw": re.compile(_NUM + r"\s?(MW|GW)\b"),
    "pue": re.compile(r"\bPUE\b[^0-9]{0,40}(1[.,]\d{1,2})", re.I),
    "wue": re.compile(r"\bWUE\b[^0-9]{0,40}(\d[.,]\d{1,3})", re.I),
    "renovavel_pct": re.compile(r"(\d{1,3})\s?%[^.]{0,60}\b(renew|renov|green|verde)", re.I),
    "agua_refrigeracao": re.compile(r"\b(seawater|sea water|água do mar|agua do mar|water[- ]cool|refrigera\w+|"
                                    r"cooling|arrefecimento|consumo de água|water consumption|WUE)\b", re.I),
    "certificacao": re.compile(r"\b(ISO\s?\d{4,5}|LEED\s?\w*|OCP Ready|Tier\s?(?:I{1,3}V?|[1-4])|BREEAM)\b"),
    "instalacao": re.compile(r"\b(SIN0[1-6]|Deucalion|MareNostrum|Oblivion|Navigator|Sines|Covilh[ãa]|"
                             r"Guimar[ãa]es|Riba de Ave)\b"),
}
# Termos que indicam documento sobre infraestrutura (score de relevância por documento).
RELEVANCE = re.compile(r"\b(data ?cent(er|re)s?|centro de dados|supercomput\w*|HPC|MW|PUE|colocation|"
                       r"capacity|capacidade|energy|energia)\b", re.I)


def _country_slug() -> str:
    with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
        return str((yaml.safe_load(f).get("country") or {})["slug"])


def _snippet(text: str, m: re.Match, width: int = 110) -> str:
    return re.sub(r"\s+", " ", text[max(0, m.start() - width): m.end() + width]).strip()


def extract(pdf_path: Path) -> tuple[list[dict], int, int]:
    """Devolve (hits, n_paginas, score_relevancia)."""
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:  # noqa: BLE001 - PDFs de terceiros podem vir corrompidos
        return [], 0, 0
    hits, score = [], 0
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            continue
        score += len(RELEVANCE.findall(text))
        for signal, rx in SIGNALS.items():
            for m in rx.finditer(text):
                hits.append({"pagina": page_no, "sinal": signal, "valor": m.group(0).strip()[:60],
                             "trecho": _snippet(text, m)})
    return hits, len(reader.pages), score


def main() -> None:
    slug = _country_slug()
    manifest = pd.read_csv(PROCESSED_DIR / f"operator_documents_manifest_{slug}.csv")
    manifest = manifest[manifest["path"].notna()].drop_duplicates(subset="sha256")
    signal_rows, summary_rows = [], []
    for doc in manifest.itertuples():
        hits, pages, score = extract(PROJECT_ROOT / doc.path)
        name = Path(doc.path).name
        for h in hits:
            signal_rows.append({"operator_id": doc.operator_id, "documento": name, "url": doc.url, **h})
        counts = pd.Series([h["sinal"] for h in hits]).value_counts().to_dict() if hits else {}
        summary_rows.append({"operator_id": doc.operator_id, "documento": name, "url": doc.url, "paginas": pages,
                             "relevancia": score, "n_sinais": len(hits), **{f"n_{k}": v for k, v in counts.items()}})
    pd.DataFrame(signal_rows).to_csv(PROCESSED_DIR / f"operator_documents_signals_{slug}.csv",
                                     index=False, encoding="utf-8-sig")
    summary = pd.DataFrame(summary_rows).sort_values("relevancia", ascending=False)
    summary.to_csv(PROCESSED_DIR / f"operator_documents_summary_{slug}.csv", index=False, encoding="utf-8-sig")
    print(f"[extract-docs] {len(summary)} documentos, {len(signal_rows)} sinais")


if __name__ == "__main__":
    main()
