"""Citation metadata read by GitHub and Zenodo."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _cff():
    return yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))


def test_single_author_with_compound_surname():
    assert _cff()["authors"] == [
        {"family-names": "Della Dea", "given-names": "Giovanni", "orcid": "https://orcid.org/0009-0008-5203-3652"}
    ]


def test_points_to_the_concept_doi_and_repository():
    cff = _cff()
    assert cff["doi"] == "10.5281/zenodo.23376594"
    assert cff["repository-code"] == "https://github.com/giossaurus/datacenters-research-tools-pt"
    assert cff["license"] == "MIT"
    assert cff["type"] == "software"
