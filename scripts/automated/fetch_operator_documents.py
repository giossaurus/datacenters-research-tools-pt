# Copyright (c) 2025-2026 Giovanni Della Dea
"""
fetch_operator_documents.py
Discovers and downloads PDFs that data-centre operators publish on their official sites
(datasheets, brochures, ESG/CSRD reports, press kits): the automated counterpart of the
manually collected "Dados_Folders" used for Brazil.

Discovery per operator (config/operator_sources_<country>.yaml):
  1. robots.txt (rules + Sitemap: lines): URLs disallowed for "*" are never fetched;
  2. sitemap.xml / sitemap indexes → .pdf URLs and keyword-matching HTML pages;
  3. shallow same-host crawl (depth ≤ MAX_DEPTH, within allow_prefixes) collecting <a href="*.pdf">.

Scope: only publicly published files; no authentication, no directory listing, no bypass.

Output:
  data/documents/<country>/raw/operator_docs/<operator_id>/<sha8>_<filename>.pdf   (gitignored)
  data/processed/operator_documents_manifest_<country>.csv
"""

import argparse
import hashlib
import logging
import re
import sys
import xml.etree.ElementTree as ET
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from scraper_utils import build_session, random_delay

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_PROJECT_FALLBACK = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_FALLBACK) not in sys.path:
    sys.path.insert(0, str(_PROJECT_FALLBACK))

from scripts.lib.workspace_root import resolve_project_root

PROJECT_ROOT = resolve_project_root(_PROJECT_FALLBACK)

with open(PROJECT_ROOT / "config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

COUNTRY_SLUG = config["country"]["slug"]
SOURCES_YAML = PROJECT_ROOT / "config" / f"operator_sources_{COUNTRY_SLUG}.yaml"
DOCS_DIR = PROJECT_ROOT / "data" / "documents" / COUNTRY_SLUG / "raw" / "operator_docs"
MANIFEST_CSV = PROJECT_ROOT / "data" / "processed" / f"operator_documents_manifest_{COUNTRY_SLUG}.csv"

MAX_DEPTH = 2
MAX_PAGES = 80          # páginas HTML visitadas por operador
MAX_PDFS = 40           # PDFs baixados por operador
MAX_SITEMAPS = 8        # sitemaps lidos por operador (índices incluídos)
MAX_PDF_BYTES = 40 * 1024 * 1024

_SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|svg|webp|mp4|zip|docx?|xlsx?|pptx?|css|js|ico)(\?|$)", re.I)


def _is_pdf_url(url: str) -> bool:
    return bool(re.search(r"\.pdf(\?|#|$)", url, re.I))


class Operator:
    def __init__(self, spec: dict, keywords: list[str]) -> None:
        self.id = spec["id"]
        self.name = spec.get("name", self.id)
        self.base = spec["base"].rstrip("/")
        self.host = urlparse(self.base).netloc
        self.prefixes = spec.get("allow_prefixes") or ["/"]
        self.seeds = [urljoin(self.base + "/", s) for s in spec.get("seeds") or []]
        self.keywords = [k.lower() for k in keywords]

    def in_scope(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc == self.host and any(p.path.startswith(pref) for pref in self.prefixes)

    def matched_keywords(self, *texts: str) -> list[str]:
        blob = " ".join(t.lower() for t in texts if t)
        return [k for k in self.keywords if k in blob]


def _robots(session: requests.Session, op: Operator) -> tuple[RobotFileParser, list[str]]:
    rp = RobotFileParser()
    sitemaps: list[str] = []
    try:
        r = session.get(f"{op.base}/robots.txt", timeout=20)
        lines = r.text.splitlines() if r.ok else []
    except requests.RequestException:
        lines = []
    rp.parse(lines)
    for line in lines:
        if line.lower().startswith("sitemap:"):
            sitemaps.append(line.split(":", 1)[1].strip())
    return rp, sitemaps or [f"{op.base}/sitemap.xml"]


def _sitemap_urls(session: requests.Session, op: Operator, rp: RobotFileParser, start: list[str]) -> list[str]:
    """Lê sitemaps (e índices) e devolve as URLs <loc> dentro do escopo."""
    found: list[str] = []
    queue, seen = deque(start), set()
    while queue and len(seen) < MAX_SITEMAPS:
        sm = queue.popleft()
        if sm in seen or not rp.can_fetch("*", sm):
            continue
        seen.add(sm)
        try:
            r = session.get(sm, timeout=30)
            if not r.ok:
                continue
            root = ET.fromstring(r.content)
        except (requests.RequestException, ET.ParseError):
            continue
        for loc in root.iter():
            if not loc.tag.endswith("loc") or not loc.text:
                continue
            url = loc.text.strip()
            if url.endswith(".xml") or "sitemap" in url.rsplit("/", 1)[-1]:
                queue.append(url)
            elif op.in_scope(url):
                found.append(url)
        random_delay(min_s=1.0, max_s=2.0)
    return found


def discover(session: requests.Session, op: Operator) -> list[dict]:
    """Devolve candidatos a PDF: {url, found_on, anchor_text, via}."""
    rp, sitemaps = _robots(session, op)
    pdfs: dict[str, dict] = {}

    pages: deque[tuple[str, int]] = deque((u, 0) for u in [op.base + "/", *op.seeds] if op.in_scope(u))
    for url in _sitemap_urls(session, op, rp, sitemaps):
        if _is_pdf_url(url):
            pdfs.setdefault(url, {"url": url, "found_on": "sitemap", "anchor_text": "", "via": "sitemap"})
        elif op.matched_keywords(url):
            pages.append((url, 1))
    logger.info("  sitemap: %d PDF(s), %d página(s) na fila", len(pdfs), len(pages))

    visited: set[str] = set()
    while pages and len(visited) < MAX_PAGES:
        url, depth = pages.popleft()
        url = urldefrag(url)[0]
        if url in visited or not rp.can_fetch("*", url):
            continue
        visited.add(url)
        try:
            r = session.get(url, timeout=25)
        except requests.RequestException:
            continue
        if not r.ok or "html" not in r.headers.get("Content-Type", ""):
            continue
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.find_all("a", href=True):
            link = urldefrag(urljoin(url, a["href"]))[0]
            text = a.get_text(" ", strip=True)[:200]
            if _is_pdf_url(link):
                # PDFs podem estar em CDN de outro host: aceitar, desde que linkados por página do operador.
                pdfs.setdefault(link, {"url": link, "found_on": url, "anchor_text": text, "via": "crawl"})
            elif depth < MAX_DEPTH and op.in_scope(link) and not _SKIP_EXT.search(link):
                pages.append((link, depth + 1))
        random_delay(min_s=1.5, max_s=3.0)
    logger.info("  crawl: %d página(s) visitada(s), %d PDF(s) candidato(s)", len(visited), len(pdfs))
    return list(pdfs.values())


def _filename(url: str) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "document.pdf"
    name = re.sub(r"[^\w.\-]+", "_", name)[:120]
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def download(session: requests.Session, op: Operator, cand: dict) -> dict:
    row = {
        "operator_id": op.id,
        "operator": op.name,
        **cand,
        "keywords_matched": "; ".join(op.matched_keywords(cand["url"], cand.get("anchor_text", ""))),
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        r = session.get(cand["url"], timeout=60, stream=True)
        row["http_status"] = r.status_code
        if not r.ok:
            return row
        chunks, size = [], 0
        for chunk in r.iter_content(64 * 1024):
            size += len(chunk)
            if size > MAX_PDF_BYTES:
                row["note"] = "maior que o limite; não salvo"
                return row
            chunks.append(chunk)
        body = b"".join(chunks)
    except requests.RequestException as exc:
        row["note"] = f"erro: {type(exc).__name__}"
        return row
    if not body.startswith(b"%PDF"):
        row["note"] = "não é PDF"
        return row
    sha = hashlib.sha256(body).hexdigest()
    out = DOCS_DIR / op.id / f"{sha[:8]}_{_filename(cand['url'])}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    row.update({"bytes": len(body), "sha256": sha, "path": str(out.relative_to(PROJECT_ROOT))})
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description="Coleta documentos públicos (PDF) dos operadores.")
    parser.add_argument("--only", nargs="*", help="ids de operador a coletar (default: todos)")
    args = parser.parse_args()

    spec = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    keywords = spec.get("keywords") or []
    operators = [Operator(o, keywords) for o in spec["operators"] if not args.only or o["id"] in args.only]

    session = build_session()
    rows: list[dict] = []
    for op in operators:
        logger.info("[%s] %s", op.id, op.base)
        cands = discover(session, op)
        # Prioriza PDFs com palavra-chave na URL/âncora; os demais entram se sobrar orçamento.
        cands.sort(key=lambda c: -len(op.matched_keywords(c["url"], c.get("anchor_text", ""))))
        for cand in cands[:MAX_PDFS]:
            row = download(session, op, cand)
            rows.append(row)
            logger.info("    %s %s", row.get("http_status", "-"), row.get("path") or row.get("note") or cand["url"])
            random_delay(min_s=1.5, max_s=3.0)

    MANIFEST_CSV.parent.mkdir(parents=True, exist_ok=True)
    manifest = pd.DataFrame(rows)
    if MANIFEST_CSV.is_file() and not manifest.empty:
        prev = pd.read_csv(MANIFEST_CSV)
        manifest = pd.concat([prev, manifest]).drop_duplicates(subset=["operator_id", "url"], keep="last")
    manifest.to_csv(MANIFEST_CSV, index=False, encoding="utf-8-sig")
    saved = int(manifest["path"].notna().sum()) if "path" in manifest.columns else 0
    logger.info("Manifesto: %s (%d linhas, %d PDF salvos)", MANIFEST_CSV, len(manifest), saved)


if __name__ == "__main__":
    main()
