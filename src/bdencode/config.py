"""Configuration and filesystem policy for the backend."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping


class ConfigurationError(ValueError):
    """Raised when a configuration value violates a safety invariant."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.context = dict(context or {})


@dataclass(frozen=True, slots=True)
class Settings:
    data_root: Path = Path("/home/accofil/encode")
    source_roots: tuple[Path, ...] = (Path("/home/accofil/storage"),)
    database_path: Path | None = None
    bind_host: str = "127.0.0.1"
    bind_port: int = 8796
    api_root_path: str = "/encoder"
    worker_poll_seconds: float = 2.0
    cpu_limit_percent: int = 80
    # A five-frame comparison routinely collapses to only two or three scenes.
    # Version 2 uses a title-wide, scene-separated sample large enough to cover
    # bright/dark, motion, texture and all I/P/B categories.
    comparison_pair_count: int = 24
    # Deprecated compatibility key. New comparison code uses
    # ``comparison_pair_count``; retaining this field lets older TOML files and
    # environment overrides load without becoming unknown configuration.
    comparison_frames_per_type: int = 4
    log_level: str = "INFO"
    # Optional, operator-confirmed AI profile adviser.  The API endpoint is
    # intentionally fixed in code; only the model and timeout are configurable.
    ai_model: str = "gpt-5.6-terra"
    ai_timeout_seconds: float = 60.0
    # Release preparation is intentionally stored outside ``completed``.  A
    # completed directory is a public release tree, while release kits may
    # contain a private tracker torrent and therefore remain server-side.
    release_profiles_path: Path | None = None
    config_path: Path | None = None

    @property
    def state_root(self) -> Path:
        return self.data_root / "state"

    @property
    def jobs_root(self) -> Path:
        return self.data_root / "jobs"

    @property
    def completed_root(self) -> Path:
        return self.data_root / "completed"

    @property
    def cache_root(self) -> Path:
        return self.data_root / "cache"

    @property
    def updates_root(self) -> Path:
        return self.data_root / "updates"

    @property
    def release_kits_root(self) -> Path:
        return self.data_root / "release-kits"

    @property
    def resolved_release_profiles_path(self) -> Path:
        return self.release_profiles_path or self.state_root / "release-profiles.json"

    @property
    def resolved_database_path(self) -> Path:
        return self.database_path or self.state_root / "encoder.sqlite3"

    def validate(self) -> "Settings":
        if not 1 <= self.cpu_limit_percent <= 100:
            raise ConfigurationError("cpu_limit_percent must be between 1 and 100")
        if not 1 <= self.bind_port <= 65535:
            raise ConfigurationError("bind_port must be between 1 and 65535")
        if self.bind_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ConfigurationError("the backend must bind to loopback")
        if not self.source_roots:
            raise ConfigurationError("at least one source root is required")
        if not 20 <= self.comparison_pair_count <= 50:
            raise ConfigurationError("comparison_pair_count must be between 20 and 50")
        if self.comparison_frames_per_type < 1:
            raise ConfigurationError("comparison_frames_per_type must be positive")
        if (
            not self.ai_model
            or len(self.ai_model) > 100
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", self.ai_model) is None
        ):
            raise ConfigurationError("ai_model contains unsupported characters")
        if not 5 <= self.ai_timeout_seconds <= 180:
            raise ConfigurationError("ai_timeout_seconds must be between 5 and 180")
        data = self.data_root.expanduser().resolve(strict=False)
        sources = tuple(
            item.expanduser().resolve(strict=False) for item in self.source_roots
        )
        if any(data == source or data.is_relative_to(source) for source in sources):
            raise ConfigurationError("data_root must not be inside a source root")
        return replace(self, data_root=data, source_roots=sources)

    def create_directories(self) -> None:
        for path in (
            self.data_root,
            self.state_root,
            self.jobs_root,
            self.completed_root,
            self.cache_root,
            self.updates_root,
            self.release_kits_root,
            self.state_root / "overrides",
            self.state_root / "backups",
        ):
            path.mkdir(mode=0o750, parents=True, exist_ok=True)

    def authorize_source(
        self, candidate: str | Path, *, must_exist: bool = True
    ) -> Path:
        path = Path(candidate).expanduser().resolve(strict=must_exist)
        if not any(
            path == root or path.is_relative_to(root) for root in self.source_roots
        ):
            raise ConfigurationError(f"source is outside configured roots: {path}")
        if path == self.data_root or path.is_relative_to(self.data_root):
            raise ConfigurationError("work/output paths cannot be used as sources")
        return path

    def job_root(self, job_id: str) -> Path:
        if not job_id or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in job_id
        ):
            raise ConfigurationError("invalid job id")
        return self.jobs_root / job_id


def _coerce(name: str, value: Any) -> Any:
    path_fields = {
        "data_root",
        "database_path",
        "release_profiles_path",
        "config_path",
    }
    if name in path_fields:
        return None if value in (None, "") else Path(value)
    if name == "source_roots":
        if isinstance(value, str):
            value = [part for part in value.split(os.pathsep) if part]
        return tuple(Path(part) for part in value)
    if name in {
        "bind_port",
        "cpu_limit_percent",
        "comparison_pair_count",
        "comparison_frames_per_type",
    }:
        return int(value)
    if name in {"worker_poll_seconds", "ai_timeout_seconds"}:
        return float(value)
    return value


def load_settings(
    path: str | Path | None = None, env: Mapping[str, str] | None = None
) -> Settings:
    environment = os.environ if env is None else env
    selected = Path(
        path or environment.get("BDENCODE_CONFIG", "/etc/bdencode/config.toml")
    )
    raw: dict[str, Any] = {}
    if selected.is_file():
        with selected.open("rb") as handle:
            document = tomllib.load(handle)
        raw.update(document.get("bdencode", document))

    known = {item.name for item in fields(Settings)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigurationError(
            f"unknown configuration keys: {', '.join(sorted(unknown))}"
        )

    for name in known:
        key = f"BDENCODE_{name.upper()}"
        if key in environment:
            raw[name] = environment[key]
    raw["config_path"] = selected
    values = {name: _coerce(name, value) for name, value in raw.items()}
    return Settings(**values).validate()
