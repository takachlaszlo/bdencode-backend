from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bdencode.cli import main


@pytest.fixture
def api_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[dict[str, Any]]]:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[bdencode]\ndata_root = "{(tmp_path / "data").as_posix()}"\n'
        f'source_roots = ["{source.as_posix()}"]\nbind_port = 8801\n',
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    def fake_run(app: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return config, calls


def test_api_uses_configured_loopback_host_and_port(
    api_config: tuple[Path, list[dict[str, Any]]],
) -> None:
    config, calls = api_config
    assert main(["--config", str(config), "api"]) == 0
    (call,) = calls
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8801


def test_api_accepts_loopback_overrides(
    api_config: tuple[Path, list[dict[str, Any]]],
) -> None:
    config, calls = api_config
    assert main(["--config", str(config), "api", "--host", "::1", "--port", "9100"]) == 0
    (call,) = calls
    assert call["host"] == "::1"
    assert call["port"] == 9100


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.test"])
def test_api_rejects_non_loopback_host_override(
    api_config: tuple[Path, list[dict[str, Any]]],
    host: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, calls = api_config
    assert main(["--config", str(config), "api", "--host", host]) == 2
    assert "loopback" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("port", ["0", "-1", "65536"])
def test_api_rejects_out_of_range_port_override(
    api_config: tuple[Path, list[dict[str, Any]]],
    port: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, calls = api_config
    assert main(["--config", str(config), "api", "--port", port]) == 2
    assert "bind_port" in capsys.readouterr().err
    assert calls == []
