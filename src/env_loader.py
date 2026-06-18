from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_env_file(env_path: str | Path | None = None, override: bool = False) -> bool:
    path = Path(env_path) if env_path is not None else _project_root() / ".env"
    if not path.exists() or not path.is_file():
        return False

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if not override and key in os.environ:
            continue
        os.environ[key] = value

    return True
