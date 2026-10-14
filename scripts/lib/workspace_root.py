"""Raiz gravável do projeto; em modo empacotado vem de DATACENTER_WORKSPACE_ROOT."""

from __future__ import annotations

import os
from pathlib import Path

ENV_KEY = "DATACENTER_WORKSPACE_ROOT"


def resolve_project_root(fallback_repo_root: Path) -> Path:
    raw = os.environ.get(ENV_KEY)
    if raw:
        return Path(raw).expanduser().resolve()
    return fallback_repo_root.resolve()
