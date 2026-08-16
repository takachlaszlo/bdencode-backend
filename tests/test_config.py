from __future__ import annotations

from pathlib import Path

import pytest

from bdencode.config import ConfigurationError, Settings, load_settings


def test_comparison_pair_count_defaults_to_title_wide_sample() -> None:
    settings = Settings().validate()

    assert settings.comparison_pair_count == 24


@pytest.mark.parametrize("pair_count", (20, 24, 50))
def test_comparison_pair_count_accepts_bounded_values(pair_count: int) -> None:
    settings = Settings(comparison_pair_count=pair_count).validate()

    assert settings.comparison_pair_count == pair_count


@pytest.mark.parametrize("pair_count", (19, 51))
def test_comparison_pair_count_rejects_values_outside_bounds(pair_count: int) -> None:
    with pytest.raises(
        ConfigurationError,
        match="comparison_pair_count must be between 20 and 50",
    ):
        Settings(comparison_pair_count=pair_count).validate()


def test_comparison_pair_count_environment_value_is_coerced_to_int(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        tmp_path / "missing.toml",
        env={"BDENCODE_COMPARISON_PAIR_COUNT": "20"},
    )

    assert settings.comparison_pair_count == 20
    assert isinstance(settings.comparison_pair_count, int)


def test_legacy_comparison_frames_per_type_config_still_loads(
    tmp_path: Path,
) -> None:
    config = tmp_path / "legacy.toml"
    config.write_text(
        "[bdencode]\ncomparison_frames_per_type = 2\n",
        encoding="utf-8",
    )

    settings = load_settings(config, env={})

    assert settings.comparison_pair_count == 24
    assert settings.comparison_frames_per_type == 2


def test_legacy_comparison_frames_environment_value_is_still_coerced(
    tmp_path: Path,
) -> None:
    settings = load_settings(
        tmp_path / "missing.toml",
        env={"BDENCODE_COMPARISON_FRAMES_PER_TYPE": "3"},
    )

    assert settings.comparison_frames_per_type == 3
    assert isinstance(settings.comparison_frames_per_type, int)
