# Copyright (c) 2025-2026 Giovanni Della Dea
"""SQLite da camada de COLETA: aditivo, com proveniência.

Centraliza os dados brutos coletados (scrapers) e o cache dos conectores num único
arquivo ``<workspace>/data/collections.db``, em vez de CSV/JSON soltos. A pipeline
continua igual: ``export_csv`` regenera o mesmo ``*_raw.csv`` que o transform lê.

Modelo "estado atual + proveniência":
- ``upsert_scraped`` deduplica por chave natural e acumula entre runs
  (``first_seen``/``last_seen``/``run_count``).
- ``mark_absent`` marca o que sumiu da fonte (``present_in_last_run=0``) sem apagar.
- ``export_csv`` reconstrói o CSV a partir do ``raw_json`` (idêntico ao original).

sqlite3 nativo + WAL, sem ORM.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Colunas "indexadas" (consultáveis/UI). O conteúdo completo fica em raw_json.
INDEXED_COLUMNS = [
    "name", "company", "city", "state", "country", "url",
    "latitude", "longitude", "address", "postal_code", "description", "source_city",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_scraped (
    source              TEXT NOT NULL,
    natural_key         TEXT NOT NULL,
    name TEXT, company TEXT, city TEXT, state TEXT, country TEXT, url TEXT,
    latitude TEXT, longitude TEXT, address TEXT, postal_code TEXT,
    description TEXT, source_city TEXT,
    raw_json            TEXT NOT NULL,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    run_count           INTEGER NOT NULL DEFAULT 1,
    last_run_id         TEXT,
    present_in_last_run INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, natural_key)
);
CREATE INDEX IF NOT EXISTS idx_raw_source  ON raw_scraped(source);
CREATE INDEX IF NOT EXISTS idx_raw_present ON raw_scraped(source, present_in_last_run);

CREATE TABLE IF NOT EXISTS connector_cache (
    connector_name TEXT NOT NULL,
    cache_key      TEXT NOT NULL,
    data_json      TEXT NOT NULL,
    retrieved_at   TEXT NOT NULL,
    ttl_hours      INTEGER NOT NULL DEFAULT 24,
    PRIMARY KEY (connector_name, cache_key)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id          TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    records_total   INTEGER,
    records_new     INTEGER,
    records_updated INTEGER,
    records_absent  INTEGER,
    status          TEXT NOT NULL DEFAULT 'running',
    error           TEXT
);
"""


def db_path(workspace: str | Path) -> Path:
    return Path(workspace) / "data" / "collections.db"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(workspace: str | Path) -> sqlite3.Connection:
    """Abre (e inicializa) o banco de coletas. Idempotente."""
    path = db_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_SCHEMA)
    return con


# ── chave natural por fonte ──────────────────────────────────────────────────
def _g(row: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def natural_key(source: str, row: dict[str, Any]) -> str:
    """Identidade estável da linha por fonte (para dedup/upsert).

    A URL só é boa chave quando é específica (tem path): a URL base é
    compartilhada por linhas-agregado (ex.: "Unmapped Data Centers" por cidade),
    que precisam ser distinguidas por ``source_city``.
    """
    if source == "datacenters_com":
        return _g(row, "id") or "|".join([_g(row, "name"), _g(row, "city"), _g(row, "region", "state")])
    # datacentermap, scrapegraph e default
    url = _g(row, "url")
    if url and urlparse(url).path.strip("/"):
        return url
    return "|".join([_g(row, "name"), _g(row, "city"), _g(row, "source_city")])


# ── runs ─────────────────────────────────────────────────────────────────────
def start_run(con: sqlite3.Connection, source: str) -> str:
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    con.execute(
        "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?,?,?, 'running')",
        (run_id, source, now_iso()),
    )
    con.commit()
    return run_id


def finish_run(
    con: sqlite3.Connection,
    run_id: str,
    *,
    total: int,
    new: int,
    updated: int,
    absent: int,
    status: str = "success",
    error: str | None = None,
) -> None:
    con.execute(
        """UPDATE collection_runs SET finished_at=?, records_total=?, records_new=?,
           records_updated=?, records_absent=?, status=?, error=? WHERE run_id=?""",
        (now_iso(), total, new, updated, absent, status, error, run_id),
    )
    con.commit()


# ── upsert / absent / export ─────────────────────────────────────────────────
def upsert_scraped(
    con: sqlite3.Connection, source: str, rows: list[dict[str, Any]], run_id: str
) -> tuple[int, int]:
    """Insere/atualiza linhas por chave natural. Retorna (novos, atualizados)."""
    new = updated = 0
    ts = now_iso()
    set_cols = ", ".join(f"{c}=?" for c in INDEXED_COLUMNS)
    insert_cols = ["source", "natural_key", *INDEXED_COLUMNS, "raw_json",
                   "first_seen", "last_seen", "run_count", "last_run_id", "present_in_last_run"]
    placeholders = ",".join("?" * len(insert_cols))
    for row in rows:
        nk = natural_key(source, row)
        if not nk:
            continue
        indexed_vals = [(_g(row, c) or None) for c in INDEXED_COLUMNS]
        raw_json = json.dumps(row, ensure_ascii=False, default=str)
        exists = con.execute(
            "SELECT 1 FROM raw_scraped WHERE source=? AND natural_key=?", (source, nk)
        ).fetchone()
        if exists:
            con.execute(
                f"""UPDATE raw_scraped SET {set_cols}, raw_json=?, last_seen=?,
                    run_count=run_count+1, last_run_id=?, present_in_last_run=1
                    WHERE source=? AND natural_key=?""",
                (*indexed_vals, raw_json, ts, run_id, source, nk),
            )
            updated += 1
        else:
            con.execute(
                f"INSERT INTO raw_scraped({','.join(insert_cols)}) VALUES ({placeholders})",
                (source, nk, *indexed_vals, raw_json, ts, ts, 1, run_id, 1),
            )
            new += 1
    con.commit()
    return new, updated


def count_present(con: sqlite3.Connection, source: str) -> int:
    r = con.execute(
        "SELECT COUNT(*) AS n FROM raw_scraped WHERE source=? AND present_in_last_run=1", (source,)
    ).fetchone()
    return int(r["n"]) if r else 0


def mark_absent(con: sqlite3.Connection, source: str, run_id: str) -> int:
    """Marca como ausentes as linhas não vistas neste run (não apaga)."""
    cur = con.execute(
        "UPDATE raw_scraped SET present_in_last_run=0 WHERE source=? AND last_run_id IS NOT ?",
        (source, run_id),
    )
    con.commit()
    return cur.rowcount


def export_csv(
    con: sqlite3.Connection, source: str, out_path: str | Path, *, present_only: bool = True
) -> int:
    """Regenera o ``*_raw.csv`` a partir do raw_json (contrato idêntico ao original)."""
    import pandas as pd

    q = "SELECT raw_json FROM raw_scraped WHERE source=?"
    if present_only:
        q += " AND present_in_last_run=1"
    rows = [json.loads(r["raw_json"]) for r in con.execute(q, (source,)).fetchall()]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False, encoding="utf-8-sig")
    return len(df)


# ── camada GeoJSON (ex.: CNARH outorgas) ─────────────────────────────────────
def feature_key(feature: dict[str, Any]) -> str:
    """Chave natural de uma feição = hash da feição inteira (geometria+props).

    Necessário porque identificadores como CODIGO_CNARH NÃO são únicos (várias
    outorgas por código). O hash preserva feições distintas e deduplica apenas
    re-coletas byte-idênticas.
    """
    canon = json.dumps(feature, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()  # noqa: S324 - id, não cripto


def upsert_features(
    con: sqlite3.Connection,
    source: str,
    features: list[dict[str, Any]],
    run_id: str,
    *,
    prop_map: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Upsert de feições GeoJSON. ``prop_map`` mapeia colunas indexadas → propriedade."""
    prop_map = prop_map or {}
    existing = {
        r["natural_key"]
        for r in con.execute("SELECT natural_key FROM raw_scraped WHERE source=?", (source,))
    }
    new = updated = 0
    ts = now_iso()
    set_cols = ", ".join(f"{c}=?" for c in INDEXED_COLUMNS)
    insert_cols = ["source", "natural_key", *INDEXED_COLUMNS, "raw_json",
                   "first_seen", "last_seen", "run_count", "last_run_id", "present_in_last_run"]
    ph = ",".join("?" * len(insert_cols))
    for feat in features:
        nk = feature_key(feat)
        props = feat.get("properties") or {}
        indexed_vals = []
        for col in INDEXED_COLUMNS:
            prop = prop_map.get(col)
            v = props.get(prop) if prop else None
            indexed_vals.append(str(v).strip() if v not in (None, "") else None)
        raw_json = json.dumps(feat, ensure_ascii=False, default=str)
        if nk in existing:
            con.execute(
                f"""UPDATE raw_scraped SET {set_cols}, raw_json=?, last_seen=?,
                    run_count=run_count+1, last_run_id=?, present_in_last_run=1
                    WHERE source=? AND natural_key=?""",
                (*indexed_vals, raw_json, ts, run_id, source, nk),
            )
            updated += 1
        else:
            con.execute(
                f"INSERT INTO raw_scraped({','.join(insert_cols)}) VALUES ({ph})",
                (source, nk, *indexed_vals, raw_json, ts, ts, 1, run_id, 1),
            )
            existing.add(nk)
            new += 1
    con.commit()
    return new, updated


def export_geojson(
    con: sqlite3.Connection, source: str, out_path: str | Path, *, present_only: bool = True
) -> int:
    """Reconstrói o FeatureCollection a partir do raw_json (contrato idêntico)."""
    q = "SELECT raw_json FROM raw_scraped WHERE source=?"
    if present_only:
        q += " AND present_in_last_run=1"
    feats = [json.loads(r["raw_json"]) for r in con.execute(q, (source,)).fetchall()]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(feats)


def ingest_geojson(
    workspace: str | Path,
    source: str,
    features: list[dict[str, Any]],
    geojson_out: str | Path | None = None,
    *,
    prop_map: dict[str, str] | None = None,
    drop_absent: bool = False,
) -> dict[str, Any]:
    """Fluxo completo para uma coleta GeoJSON (acumula; exporta FeatureCollection)."""
    con = connect(workspace)
    try:
        run_id = start_run(con, source)
        try:
            new, updated = upsert_features(con, source, features, run_id, prop_map=prop_map)
            absent = mark_absent(con, source, run_id) if drop_absent else 0
            exported = export_geojson(con, source, geojson_out, present_only=drop_absent) if geojson_out else 0
            present = count_present(con, source)
            total = con.execute(
                "SELECT COUNT(*) AS n FROM raw_scraped WHERE source=?", (source,)
            ).fetchone()["n"]
            finish_run(con, run_id, total=total, new=new, updated=updated, absent=absent)
            return {"run_id": run_id, "new": new, "updated": updated, "absent": absent,
                    "exported": exported, "present": present, "total": total}
        except Exception as e:  # noqa: BLE001
            finish_run(con, run_id, total=0, new=0, updated=0, absent=0, status="failed", error=str(e))
            raise
    finally:
        con.close()


def ingest(
    workspace: str | Path,
    source: str,
    rows: list[dict[str, Any]],
    csv_out: str | Path | None = None,
    *,
    drop_absent: bool = False,
) -> dict[str, Any]:
    """Fluxo completo de uma coleta: run → upsert → (absent) → export → finish.

    ``drop_absent=False`` (padrão dos scrapers): **acumula**: nada é descartado, o
    CSV exportado inclui tudo que já foi coletado (uma falha transitória numa cidade
    não apaga DCs anteriores). ``last_seen``/``run_count`` registram o frescor.

    ``drop_absent=True``: modelo "estado atual": marca ausentes (não vistos neste
    run) e exporta só os presentes.
    """
    con = connect(workspace)
    try:
        run_id = start_run(con, source)
        try:
            new, updated = upsert_scraped(con, source, rows, run_id)
            absent = mark_absent(con, source, run_id) if drop_absent else 0
            exported = export_csv(con, source, csv_out, present_only=drop_absent) if csv_out else 0
            present = count_present(con, source)
            total_rows = con.execute(
                "SELECT COUNT(*) AS n FROM raw_scraped WHERE source=?", (source,)
            ).fetchone()["n"]
            finish_run(con, run_id, total=total_rows, new=new, updated=updated, absent=absent)
            return {"run_id": run_id, "new": new, "updated": updated, "absent": absent,
                    "exported": exported, "present": present, "total": total_rows}
        except Exception as e:  # noqa: BLE001
            finish_run(con, run_id, total=0, new=0, updated=0, absent=0, status="failed", error=str(e))
            raise
    finally:
        con.close()


# ── cache dos conectores (substitui JSON em data/cache) ──────────────────────
def read_connector_cache(
    workspace: str | Path, connector_name: str, key: str, max_age_hours: int = 24
) -> Any | None:
    con = connect(workspace)
    try:
        r = con.execute(
            "SELECT data_json, retrieved_at, ttl_hours FROM connector_cache WHERE connector_name=? AND cache_key=?",
            (connector_name, key),
        ).fetchone()
        if not r:
            return None
        try:
            age_h = (datetime.now(UTC) - datetime.fromisoformat(r["retrieved_at"])).total_seconds() / 3600
        except (ValueError, TypeError):
            age_h = 0.0
        ttl = r["ttl_hours"] or max_age_hours
        if age_h > ttl:
            return None
        return json.loads(r["data_json"])
    finally:
        con.close()


def write_connector_cache(
    workspace: str | Path, connector_name: str, key: str, data: Any, ttl_hours: int = 24
) -> None:
    con = connect(workspace)
    try:
        con.execute(
            """INSERT INTO connector_cache(connector_name, cache_key, data_json, retrieved_at, ttl_hours)
               VALUES (?,?,?,?,?)
               ON CONFLICT(connector_name, cache_key) DO UPDATE SET
                 data_json=excluded.data_json, retrieved_at=excluded.retrieved_at, ttl_hours=excluded.ttl_hours""",
            (connector_name, key, json.dumps(data, ensure_ascii=False, default=str), now_iso(), ttl_hours),
        )
        con.commit()
    finally:
        con.close()
