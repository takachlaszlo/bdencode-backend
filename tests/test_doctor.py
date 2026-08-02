from __future__ import annotations

from pathlib import Path

from bdencode.cli import main
from bdencode.config import Settings


def test_queue_idle_exit_status(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    source = tmp_path / "storage"
    source.mkdir()
    data = tmp_path / "encode"
    config.write_text(
        "[bdencode]\n"
        f'data_root = "{data.as_posix()}"\n'
        f'source_roots = ["{source.as_posix()}"]\n',
        encoding="utf-8",
    )
    assert main(["--config", str(config), "init-db"]) == 0
    assert main(["--config", str(config), "queue-idle"]) == 0


def test_settings_cpu_quota_is_total_machine_fraction(tmp_path: Path) -> None:
    source = tmp_path / "storage"
    source.mkdir()
    settings = Settings(
        data_root=tmp_path / "encode", source_roots=(source,), cpu_limit_percent=80
    ).validate()
    assert settings.cpu_limit_percent == 80
