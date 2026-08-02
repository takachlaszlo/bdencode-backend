"""Secret loading with systemd credentials as the production default."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


class SecretUnavailable(RuntimeError):
    pass


def read_secret(
    name: str,
    *,
    environment: Mapping[str, str] | None = None,
    allow_environment: bool = False,
) -> str:
    env = os.environ if environment is None else environment
    credentials_dir = env.get("CREDENTIALS_DIRECTORY")
    if credentials_dir:
        target = (Path(credentials_dir) / name).resolve(strict=True)
        root = Path(credentials_dir).resolve(strict=True)
        if target.parent != root or not target.is_file():
            raise SecretUnavailable(f"invalid credential path for {name}")
        value = target.read_text(encoding="utf-8").strip()
        if value:
            return value
    if allow_environment:
        key = name.upper().replace("-", "_")
        if env.get(key):
            return env[key].strip()
    raise SecretUnavailable(f"credential is not available: {name}")
