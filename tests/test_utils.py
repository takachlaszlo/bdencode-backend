from __future__ import annotations

import json
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
