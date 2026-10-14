# datacenters-research-tools-pt

Code and data for the national mapping of data centre infrastructure in Portugal used in the paper
*The Hidden Environmental Infrastructure of AI in Higher Education* (I-HE2026, EADTU).

The repository contains the collection scripts, the curation rules, the documentary enrichment with
its sources, and the script that produces the paper's figures, Table 1 and indicators.

The work was carried out within the research project "AI Governance in Brazil and Portugal: Crossings
and Trends", funded by the National Council for Scientific and Technological Development (CNPq, Brazil).

## Requirements

Python 3.13 or later.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

## Reproducing the results

The curated and enriched dataset is versioned, so the paper's numbers can be regenerated without
collecting again:

```bash
.venv/bin/python scripts/pipeline/apply_enrichment.py
.venv/bin/python scripts/pipeline/portugal_results.py
```

`portugal_results.py` downloads the NUTS 2024 boundaries from Eurostat GISCO on the first run and
caches them in `data/cache/`. It writes:

| File | Content |
|---|---|
| `region_summary_portugal.csv` | Sites and declared capacity per NUTS II region |
| `region_summary_sensibilidade_portugal.csv` | Same, without the three sites with weak evidence of operation |
| `operator_table_portugal.csv`, `table1_operators_portugal.md` | Operators, controller country, sites and capacity (Table 1) |
| `concentration_portugal.json` | HHI on sites and on capacity, four-firm ratio, foreign control |
| `fig1_map_nuts2_portugal.{png,svg}` | Figure 1 |
| `fig2_sites_vs_mw_portugal.{png,svg}` | Figure 2 |

Figures regenerated with other versions of matplotlib or other fonts may differ by a fraction of a
point in size. The tables and indicators are identical.

## Pipeline

1. **Collection** (6 October 2026). `scripts/automated/fetch_datacentermap.py` and
   `fetch_datacenters_com.py` collect the directory listings; the `*_profiles.py` scripts collect each
   facility's profile. Pages that could not be fetched were saved by hand and parsed with
   `scripts/manual/`.
2. **Curation.** `scripts/pipeline/curate_facilities.py` applies rules R1 to R5 declared in
   `config/curation_portugal.yaml` and writes one row per source record (`curation_table_portugal.csv`)
   and one row per site (`facilities_curated_portugal.csv`). It reads the datacenters.com listing
   (`datacenters_portugal_raw.csv`), which is not redistributed; both curation outputs are versioned.
3. **Enrichment.** `data/curation/enrichment_portugal/` holds the values found in operators'
   reports, press releases, licensing documents and the press. Each value has its URL, a verbatim
   quotation, access date, source type and confidence. `apply_enrichment.py` applies high and medium
   confidence values and never overwrites directory values. `validacao_2026-10-09.json` records the
   check of every applied value used in the paper against its source.
4. **Operator documents.** `fetch_operator_documents.py` collects public PDFs linked from the
   operators' websites listed in `config/operator_sources_portugal.yaml`, following each site's
   robots.txt. `extract_operator_documents.py` extracts mentions of capacity (MW), PUE, water and cooling,
   renewable share and certifications, with page references.
5. **Results.** `portugal_results.py`, as above.

## Curation rules

| Rule | Decision |
|---|---|
| R1 | The unit of analysis is the physical site; buildings of the same campus are attributes |
| R2 | Colocation tenants and resellers located in another operator's facility are excluded |
| R3 | Development stage follows the operator's profile and changes only on dated documentary evidence |
| R4 | Records of the same operator within 10 km are merged as cross-directory duplicates |
| R5 | Locations come from coordinates, reverse-geocoded with OpenStreetMap Nominatim and assigned to NUTS II 2024 regions |

Manual decisions are listed with their reason under `overrides` in `config/curation_portugal.yaml`.

## Not included

The raw HTML pages of the directories and the third-party PDFs are not redistributed. Their URLs and
access dates are recorded in the data files.

## Citation

Della Dea, G. (2026). *datacenters-research-tools-pt* [Computer software]. Zenodo.
https://doi.org/10.5281/zenodo.23376594

The DOI above always resolves to the latest version. Citation metadata is in `CITATION.cff`.

## License

MIT. See `LICENSE`.
