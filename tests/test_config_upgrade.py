from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import tomllib


MODULE_PATH = Path(__file__).parents[1] / "install" / "config_upgrade.py"
SPEC = importlib.util.spec_from_file_location("bdencode_config_upgrade", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
config_upgrade = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = config_upgrade
SPEC.loader.exec_module(config_upgrade)


def test_upgrade_inserts_release_profiles_inside_bdencode_table(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """[bdencode]
data_root = "/srv/bdencode"
source_roots = ["/srv/media"]

[operator_notes]
owner = "example"
""",
        encoding="utf-8",
    )

    assert config_upgrade.upgrade(config, "/etc/bdencode/release-profiles.json")

    with config.open("rb") as stream:
        document = tomllib.load(stream)
    assert (
        document["bdencode"]["release_profiles_path"]
        == "/etc/bdencode/release-profiles.json"
    )
    assert "release_profiles_path" not in document["operator_notes"]


def test_upgrade_is_idempotent_and_preserves_existing_custom_path(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    original = b"""[bdencode]\r
release_profiles_path = \"/etc/custom/profiles.json\"\r
"""
    config.write_bytes(original)

    assert not config_upgrade.upgrade(config, "/etc/bdencode/release-profiles.json")
    assert config.read_bytes() == original
