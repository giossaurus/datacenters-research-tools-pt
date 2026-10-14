"""Repository text: personal copyright, no institutional branding in code, plain README."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {".py", ".yaml", ".md", ".txt", ".ini"}
TEXT_NAMES = {".gitignore", "LICENSE"}
FORBIDDEN = ("lab" + "404", "uf" + "ba", "cla" + "ude", "anth" + "ropic", "open" + "ai", "co" + "pilot", ".cur" + "sor")


def _text_files():
    for path in ROOT.rglob("*"):
        if ".venv" in path.parts or ".git" in path.parts or not path.is_file():
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path


def test_no_institutional_branding():
    hits = [
        str(p.relative_to(ROOT))
        for p in _text_files()
        if any(word in p.read_text(encoding="utf-8").lower() for word in FORBIDDEN)
    ]
    assert hits == []


def test_python_files_carry_personal_copyright():
    header = "# Copyright (c) 2025-2026 Giovanni Della Dea"
    missing = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "scripts").rglob("*.py")
        if p.read_text(encoding="utf-8").startswith("# Copyright") and not p.read_text(encoding="utf-8").startswith(header + "\n")
    ]
    assert missing == []


def test_license_is_mit_in_author_name():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n\nCopyright (c) 2025-2026 Giovanni Della Dea\n")


def test_readme_has_no_dashes_used_as_punctuation():
    assert "—" not in (ROOT / "README.md").read_text(encoding="utf-8")
