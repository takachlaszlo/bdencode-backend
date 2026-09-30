from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bdencode import capabilities


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


class _HungRunner:
    def capture(self, argv, *, timeout: float = 30, check: bool = True):
        raise subprocess.TimeoutExpired(list(argv), timeout)


def _fake_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    binary = tmp_path / name
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(capabilities.shutil, "which", lambda _name: str(binary))
    return binary


def test_a_hung_version_probe_does_not_abort_tool_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _fake_tool(tmp_path, monkeypatch, "mediainfo")

    tool = capabilities.discover_tool("mediainfo", _HungRunner())

    assert tool.available
    assert tool.path == str(binary.resolve())
    assert tool.version is None
    assert tool.sha256 is not None


def test_version_probe_output_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tool(tmp_path, monkeypatch, "ffmpeg")

    class Runner:
        def capture(self, argv, *, timeout: float = 30, check: bool = True):
            return _Result(stdout="ffmpeg version 7.1\nbuilt with gcc\n")

    tool = capabilities.discover_tool("ffmpeg", Runner())

    assert tool.version == "ffmpeg version 7.1"


def test_hung_ffmpeg_feature_probe_reports_capabilities_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_tool(tmp_path, monkeypatch, "ffmpeg")

    features = capabilities.ffmpeg_features(_HungRunner())

    assert features == {
        "encoders": [],
        "filters": [],
        "protocols": [],
        "bitstream_filters": [],
    }
