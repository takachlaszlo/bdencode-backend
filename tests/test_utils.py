from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bdencode import utils


def test_atomic_write_json_syncs_parent_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "checkpoint.json"
    synced: list[Path] = []
    monkeypatch.setattr(utils, "_fsync_directory", synced.append)

    utils.atomic_write_json(target, {"provider": "catbox"})

    assert synced == [tmp_path]
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "provider": "catbox"
    }


def test_atomic_write_text_never_follows_predictable_temporary_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "comparison.bbcode"
    external = tmp_path / "external.txt"
    external.write_text("SENTINEL\n", encoding="utf-8")
    predictable = tmp_path / f".{target.name}.{os.getpid()}.tmp"
    try:
        predictable.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    utils.atomic_write_text(target, "PUBLIC\n")

    assert external.read_text(encoding="utf-8") == "SENTINEL\n"
    assert predictable.is_symlink()
    assert target.read_text(encoding="utf-8") == "PUBLIC\n"
    assert target.is_file()
    assert not target.is_symlink()
