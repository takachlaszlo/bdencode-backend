from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bdencode.api import create_app
from bdencode.db import Database
from bdencode.media.noise_profiles import (
    apply_noise_profile,
    noise_profile,
    noise_profiles,
    preset_settings,
)
from bdencode.media.profiles import (
    EncoderSettings,
    VideoEncoder,
    profile_schema,
    recommended_profile,
)


@pytest.mark.parametrize("encoder", ["x264", "x265"])
def test_every_preset_builds_valid_settings_and_a_coherent_command(encoder: str) -> None:
    for preset in noise_profiles(encoder):
        values = preset_settings(encoder, preset.id)
        settings = recommended_profile(encoder, overrides=values)
        assert isinstance(settings, EncoderSettings)
        assert apply_noise_profile(recommended_profile(encoder), preset.id) == settings
        argv = settings.ffmpeg_video_args()
        params = argv[argv.index("-x264-params" if encoder == "x264" else "-x265-params") + 1]
        assert any(
            item.startswith(("nr=", "nr-")) for item in params.split(":")
        ) == (settings.noise_reduction > 0)


def test_default_command_is_unchanged_when_noise_reduction_is_off() -> None:
    for encoder in ("x264", "x265"):
        params = " ".join(recommended_profile(encoder).ffmpeg_video_args())
        assert ":nr=" not in params and "nr-intra" not in params and "nr-inter" not in params


def test_x264_emits_nr_and_x265_emits_both_directions() -> None:
    x264 = recommended_profile("x264", overrides={"noise_reduction": 120})
    assert x264.private_params()["nr"] == 120
    x265 = recommended_profile("x265", overrides={"noise_reduction": 250})
    params = x265.private_params()
    assert params["nr-intra"] == params["nr-inter"] == 250 and "nr" not in params


def test_noise_reduction_limits_follow_the_encoder() -> None:
    recommended_profile("x265", overrides={"noise_reduction": 2000})
    with pytest.raises(ValueError, match="noise_reduction"):
        recommended_profile("x264", overrides={"noise_reduction": 1001})
    with pytest.raises(ValueError, match="noise_reduction"):
        recommended_profile("x265", overrides={"noise_reduction": -1})
    with pytest.raises(TypeError):
        recommended_profile("x264", overrides={"noise_reduction": 1.5})
    fields = {item["name"]: item for item in profile_schema("x264", "advanced")}
    assert fields["noise_reduction"]["maximum"] == 1000
    fields = {item["name"]: item for item in profile_schema("x265", "advanced")}
    assert fields["noise_reduction"]["maximum"] == 2000
    assert "noise_reduction" not in {item["name"] for item in profile_schema("x264", "beginner")}


def test_presets_are_mutually_exclusive_not_cumulative() -> None:
    grain = apply_noise_profile(recommended_profile("x264"), "preserve_grain")
    assert grain.tune.value == "grain" and grain.qcomp == 0.75
    denoised = apply_noise_profile(grain, "medium_denoise")
    assert denoised.tune.value == "film"
    assert denoised.qcomp == recommended_profile("x264").qcomp
    assert denoised.noise_reduction == 120
    assert apply_noise_profile(denoised, "off") == recommended_profile("x264")


def test_grain_preset_matches_the_existing_coherent_x264_grain_profile() -> None:
    via_tune = recommended_profile("x264", overrides={"tune": "grain"})
    via_preset = apply_noise_profile(recommended_profile("x264"), "preserve_grain")
    for name in ("tune", "qcomp", "aq_strength", "deblock_alpha", "deblock_beta", "psy_rdoq"):
        assert getattr(via_tune, name) == getattr(via_preset, name)


def test_x265_grain_preset_disables_sao_and_raises_psy_strength() -> None:
    settings = apply_noise_profile(recommended_profile("x265"), "preserve_grain")
    params = settings.private_params()
    assert params["sao"] == 0 and params["psy-rd"] == 2.5 and params["psy-rdoq"] == 3.0
    assert settings.tune.value == "grain"


def test_unknown_preset_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown noise profile"):
        noise_profile(VideoEncoder.X264, "extreme")


def test_noise_profiles_endpoint_lists_editable_concrete_settings(tmp_path) -> None:
    with TestClient(create_app(Database(tmp_path / "api.sqlite3"))) as client:
        response = client.get("/api/v1/profiles/x265/noise-profiles")
        assert response.status_code == 200
        body = response.json()
        assert body["encoder"] == "x265" and body["requires_operator_confirmation"]
        assert [item["id"] for item in body["profiles"]] == [
            "off",
            "preserve_grain",
            "light_denoise",
            "medium_denoise",
            "strong_denoise",
        ]
        light = next(item for item in body["profiles"] if item["id"] == "light_denoise")
        assert light["settings"]["noise_reduction"] == 100
        assert light["label"] and light["description"]
        assert client.get("/api/v1/profiles/av1/noise-profiles").status_code == 422
