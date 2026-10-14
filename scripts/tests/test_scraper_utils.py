"""The collectors stop at a block page; they never try to solve a CAPTCHA."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "automated"))

import scraper_utils  # noqa: E402


def test_handle_interruption_never_calls_a_captcha_solver(monkeypatch):
    monkeypatch.setenv("TWO_CAPTCHA_API_KEY", "dummy")
    real_import = builtins.__import__
    attempted: list[str] = []

    def guarded_import(name, *args, **kwargs):
        if name.startswith("twocaptcha"):
            attempted.append(name)
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert scraper_utils.handle_interruption("https://example.org", site_key="key") is False
    assert attempted == []


def test_module_has_no_captcha_solver_reference():
    source = Path(scraper_utils.__file__).read_text(encoding="utf-8").lower()
    assert "captcha" not in source.replace("handle_interruption", "")
