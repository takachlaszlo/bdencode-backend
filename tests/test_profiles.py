from __future__ import annotations

import pytest

from bdencode.media import (
    ColorMetadata,
    DetailLevel,
    EncoderSettings,
    Hdr10Metadata,
    Tune,
    VbvSettings,
    VideoEncoder,
    profile_schema,
    recommended_profile,
)
from bdencode.media.profiles import (
    gop_for_frame_rate,
    h264_level_4_1_compatibility,
    source_adapted_settings,
)


MASTERING = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"


def test_beginner_advanced_and_pro_schemas_expand_without_codec_leaks() -> None:
    beginner = {item["name"] for item in profile_schema("x264", "beginner")}
    advanced = {item["name"] for item in profile_schema("x264", "advanced")}
    pro_x264 = {item["name"] for item in profile_schema("x264", "pro")}
    pro_x265 = {item["name"] for item in profile_schema("x265", "pro")}

    assert {"encoder", "crf", "preset", "tune", "color"} <= beginner
    assert beginner < advanced < pro_x264
    assert {"keyint", "bframes", "vbv"} <= advanced
    assert {"me", "psy_rd", "deblock_alpha"} <= pro_x264
    assert {
        "level",
        "b_pyramid",
        "partitions",
        "direct",
        "chroma_qp_offset",
        "annexb",
    } <= pro_x264
    assert "sao" not in pro_x264
    assert {"sao", "limit_sao", "early_skip", "rskip", "hdr10", "annexb"} <= pro_x265
    assert "partitions" not in pro_x265
    assert "trellis" in pro_x264
    assert "trellis" not in pro_x265
    assert all(item["required"] for item in profile_schema("x264", "beginner"))


def test_recommended_anime_bd_profile_is_valid_x264() -> None:
    settings = recommended_profile(
        VideoEncoder.X264,
        detail_level=DetailLevel.ADVANCED,
        content_type="anime",
    )

    assert settings.tune is Tune.ANIMATION
    assert settings.profile == "high"
    assert settings.pixel_format == "yuv420p"
    args = settings.ffmpeg_video_args()
    assert args[:2] == ("-c:v", "libx264")
    assert "-x264-params" in args
    assert "bframes=8" in args[args.index("-x264-params") + 1]
    assert "colorprim=bt709" in args[args.index("-x264-params") + 1]
    assert "chromaloc=0" in args[args.index("-x264-params") + 1]
    assert "8x8dct=1" in args[args.index("-x264-params") + 1]


@pytest.mark.parametrize("encoder", (VideoEncoder.X264, VideoEncoder.X265))
@pytest.mark.parametrize(
    ("location", "location_id"),
    (
        ("left", 0),
        ("center", 1),
        ("topleft", 2),
        ("top", 3),
        ("bottomleft", 4),
        ("bottom", 5),
    ),
)
def test_private_encoder_params_pin_reviewed_chroma_location(
    encoder: VideoEncoder, location: str, location_id: int
) -> None:
    settings = recommended_profile(
        encoder,
        color=ColorMetadata(chroma_location=location),
    )

    assert settings.private_params()["chromaloc"] == location_id
    args = settings.ffmpeg_video_args()
    private_name = "-x264-params" if encoder is VideoEncoder.X264 else "-x265-params"
    assert f"chromaloc={location_id}" in args[args.index(private_name) + 1]


@pytest.mark.parametrize(
    ("frame_rate", "expected"),
    (
        ("24000/1001", (240, 24)),
        ("24", (240, 24)),
        ("25", (250, 25)),
        ("30000/1001", (300, 30)),
    ),
)
def test_gop_is_derived_from_exact_frame_rate(
    frame_rate: str, expected: tuple[int, int]
) -> None:
    assert gop_for_frame_rate(frame_rate) == expected


def test_h264_level_4_1_uses_cropped_macroblock_geometry_for_dpb() -> None:
    full = h264_level_4_1_compatibility(1920, 1080, "24000/1001", 5)
    letterboxed = h264_level_4_1_compatibility(1920, 804, "25", 5)

    assert full.frame_macroblocks == 120 * 68
    assert full.max_reference_frames == 4
    assert full.effective_reference_frames == 4
    assert full.reference_frames_adjusted
    assert full.compatible
    assert letterboxed.frame_macroblocks == 120 * 51
    assert letterboxed.max_reference_frames == 5
    assert letterboxed.effective_reference_frames == 5
    assert not letterboxed.reference_frames_adjusted


def test_source_policy_sets_level_gop_ref_and_effective_chroma_offset() -> None:
    requested = recommended_profile("x264")
    effective, policy = source_adapted_settings(
        requested,
        width=1920,
        height=1080,
        frame_rate="25",
    )

    assert (effective.keyint, effective.min_keyint) == (250, 25)
    assert effective.level == "4.1"
    assert effective.ref == 4
    assert effective.vbv is not None
    assert effective.vbv.maxrate_kbps == 62_500
    assert effective.vbv.bufsize_kbps == 78_125
    assert effective.chroma_qp_offset == -2
    assert effective.x264_psy_chroma_qp_adjustment() == -2
    assert effective.private_params()["chroma-qp-offset"] == 0
    assert policy["h264_level_4_1"]["reference_frames_adjusted"] is True
    assert policy["x264_chroma_qp_offset"] == {
        "effective": -2,
        "emitted_before_psy_adjustment": 0,
        "encoder_open_psy_adjustment": -2,
    }
    args = effective.ffmpeg_video_args()
    assert args[args.index("-level:v") + 1] == "4.1"
    private = args[args.index("-x264-params") + 1]
    assert "vbv-maxrate=62500" in private
    assert "vbv-bufsize=78125" in private


@pytest.mark.parametrize(
    ("kwargs", "error"),
    (
        ({"maxrate_kbps": True, "bufsize_kbps": 78_125}, TypeError),
        ({"maxrate_kbps": 62_500.0, "bufsize_kbps": 78_125}, TypeError),
        ({"maxrate_kbps": 62_500, "bufsize_kbps": 78_125.0}, TypeError),
        (
            {
                "maxrate_kbps": 62_500,
                "bufsize_kbps": 78_125,
                "initial_fullness": float("nan"),
            },
            ValueError,
        ),
    ),
)
def test_vbv_settings_reject_non_integral_caps_and_nonfinite_fullness(
    kwargs: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        VbvSettings(**kwargs)  # type: ignore[arg-type]


def test_source_policy_rejects_non_4_1_x264_level_override() -> None:
    requested = recommended_profile("x264", overrides={"level": "3.1"})

    with pytest.raises(ValueError, match="only automatic level or level 4.1"):
        source_adapted_settings(
            requested,
            width=1920,
            height=1080,
            frame_rate="24000/1001",
        )


@pytest.mark.parametrize(
    "vbv",
    (
        VbvSettings(62_501, 78_125),
        VbvSettings(62_500, 78_126),
    ),
)
def test_source_policy_rejects_level_4_1_vbv_above_caps(vbv: VbvSettings) -> None:
    requested = recommended_profile("x264", overrides={"vbv": vbv})

    with pytest.raises(ValueError, match="VBV exceeds"):
        source_adapted_settings(
            requested,
            width=1920,
            height=1080,
            frame_rate="24000/1001",
        )


def test_source_policy_accepts_level_4_1_vbv_caps_exactly() -> None:
    vbv = VbvSettings(62_500, 78_125)
    requested = recommended_profile("x264", overrides={"vbv": vbv})

    effective, _policy = source_adapted_settings(
        requested,
        width=1920,
        height=1080,
        frame_rate="24000/1001",
    )

    assert effective.vbv == vbv


def test_grain_tune_uses_one_coherent_psychovisual_baseline() -> None:
    settings = recommended_profile("x264", overrides={"tune": "grain"})

    assert settings.tune is Tune.GRAIN
    assert settings.qcomp == 0.75
    assert settings.aq_strength == 0.65
    assert (settings.deblock_alpha, settings.deblock_beta) == (-2, -2)
    assert settings.psy_rdoq == 0.15


def test_configured_chroma_offset_is_effective_not_pre_psy_input() -> None:
    settings = EncoderSettings(encoder=VideoEncoder.X264, chroma_qp_offset=0)

    assert settings.to_dict()["chroma_qp_offset"] == 0
    assert settings.private_params()["chroma-qp-offset"] == 2


def test_level_4_1_is_not_claimed_when_macroblock_rate_is_too_high() -> None:
    requested = recommended_profile("x264")
    effective, policy = source_adapted_settings(
        requested,
        width=1920,
        height=1080,
        frame_rate="60000/1001",
    )

    assert effective.level is None
    assert effective.ref == requested.ref
    assert policy["h264_level_4_1"]["macroblock_rate_compatible"] is False
    assert policy["h264_level_4_1"]["reason"] == "structurally-incompatible"


def test_static_hdr10_profile_is_main10_pq_and_serializable() -> None:
    hdr = Hdr10Metadata(
        enabled=True,
        mastering_display=MASTERING,
        max_cll=1000,
        max_fall=400,
    )
    settings = recommended_profile("x265", hdr10=hdr)

    assert settings.profile == "main10"
    assert settings.tune is Tune.NONE
    assert settings.bit_depth == 10
    assert settings.color == ColorMetadata(
        "bt2020", "smpte2084", "bt2020nc", "limited", "left"
    )
    args = settings.ffmpeg_video_args()
    private = args[args.index("-x265-params") + 1]
    assert "hdr10=1" in private
    assert f"master-display={MASTERING}" in private
    assert "max-cll=1000,400" in private
    assert settings.to_dict()["hdr10"]["enabled"] is True


def test_x265_tunes_and_private_options_are_codec_specific() -> None:
    anime = recommended_profile("x265", content_type="anime")
    assert anime.tune is Tune.ANIMATION
    params = anime.private_params()
    assert params["weightp"] == 1
    assert params["b-pyramid"] == 1
    assert "direct" not in params
    assert "trellis" not in params
    assert anime.subme <= 7

    with pytest.raises(ValueError, match="not supported by x265"):
        EncoderSettings(encoder=VideoEncoder.X265, profile="main", tune=Tune.FILM)
    with pytest.raises(ValueError, match="x265 scenecut"):
        EncoderSettings(
            encoder=VideoEncoder.X265,
            profile="main",
            tune=Tune.NONE,
            scenecut=-1,
        )
    x265_schema = {
        item["name"]: item for item in profile_schema("x265", DetailLevel.ADVANCED)
    }
    assert x265_schema["scenecut"]["minimum"] == 0
    assert "level" not in {
        item["name"] for item in profile_schema("x265", DetailLevel.PRO)
    }
    with pytest.raises(ValueError, match="explicit x265 level"):
        EncoderSettings(encoder=VideoEncoder.X265, profile="main", level="5.1")


@pytest.mark.parametrize(
    ("transfer", "ffmpeg_name"),
    (("bt470m", "gamma22"), ("bt470bg", "gamma28")),
)
def test_legacy_transfer_names_are_mapped_for_ffmpeg_5(
    transfer: str, ffmpeg_name: str
) -> None:
    settings = recommended_profile(
        "x264",
        color=ColorMetadata("bt709", transfer, "bt709", "limited", "left"),
    )
    args = settings.ffmpeg_video_args()
    assert args[args.index("-color_trc") + 1] == ffmpeg_name
    assert f"transfer={transfer}" in args[args.index("-x264-params") + 1]


@pytest.mark.parametrize(
    "field_name",
    (
        "bit_depth",
        "keyint",
        "min_keyint",
        "scenecut",
        "bframes",
        "b_adapt",
        "ref",
        "rc_lookahead",
        "weightp",
        "merange",
        "subme",
        "trellis",
        "aq_mode",
        "deblock_alpha",
        "deblock_beta",
        "chroma_qp_offset",
        "rskip",
    ),
)
@pytest.mark.parametrize("invalid_value", (True, 8.0, "8"))
def test_encoder_integer_fields_require_exact_non_boolean_ints(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(TypeError, match=field_name):
        EncoderSettings(
            encoder=VideoEncoder.X264,
            **{field_name: invalid_value},
        )


@pytest.mark.parametrize(
    "field_name",
    ("crf", "aq_strength", "qcomp", "psy_rd", "psy_rdoq"),
)
@pytest.mark.parametrize("invalid_value", (True, "0.8"))
def test_encoder_real_fields_reject_booleans_and_strings(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(TypeError, match=f"(?i){field_name}"):
        EncoderSettings(
            encoder=VideoEncoder.X264,
            **{field_name: invalid_value},
        )


@pytest.mark.parametrize(
    "field_name",
    ("crf", "aq_strength", "qcomp", "psy_rd", "psy_rdoq"),
)
@pytest.mark.parametrize(
    "invalid_value",
    (float("nan"), float("inf"), float("-inf")),
)
def test_encoder_real_fields_require_finite_values(
    field_name: str, invalid_value: float
) -> None:
    with pytest.raises(ValueError, match=f"(?i){field_name}"):
        EncoderSettings(
            encoder=VideoEncoder.X264,
            **{field_name: invalid_value},
        )


@pytest.mark.parametrize(
    "field_name",
    (
        "open_gop",
        "b_pyramid",
        "weightb",
        "sao",
        "limit_sao",
        "strong_intra_smoothing",
        "rect",
        "amp",
        "early_skip",
        "aud",
        "repeat_headers",
        "annexb",
    ),
)
@pytest.mark.parametrize("invalid_value", (0, 1.0, "true"))
def test_encoder_boolean_fields_require_exact_bools(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(TypeError, match=field_name):
        EncoderSettings(
            encoder=VideoEncoder.X264,
            **{field_name: invalid_value},
        )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"crf": float("nan")}, "CRF"),
        ({"crf": 0}, "CRF"),
        ({"crf": 52}, "CRF"),
        ({"bframes": 17}, "bframes"),
        ({"min_keyint": 241}, "min_keyint"),
        ({"pixel_format": "yuv420p10le"}, "requires yuv420p"),
        ({"partitions": "all:crf=0"}, "partitions"),
        ({"direct": "auto:fake"}, "direct"),
        ({"level": "4.1:fake"}, "level"),
    ],
)
def test_x264_validation_rejects_unsafe_or_incompatible_values(
    kwargs: dict, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        EncoderSettings(encoder=VideoEncoder.X264, **kwargs)


def test_hdr10_cannot_be_enabled_on_x264_or_with_bt709() -> None:
    hdr = Hdr10Metadata(True, MASTERING, 1000, 400)
    with pytest.raises(ValueError, match="only through x265"):
        EncoderSettings(encoder=VideoEncoder.X264, hdr10=hdr)

    with pytest.raises(ValueError, match="BT.2020"):
        EncoderSettings(
            encoder=VideoEncoder.X265,
            profile="main10",
            bit_depth=10,
            pixel_format="yuv420p10le",
            hdr10=hdr,
        )


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_x264_rejects_hdr_transfer_without_static_metadata(transfer: str) -> None:
    with pytest.raises(ValueError, match="PQ and HLG.*x265"):
        EncoderSettings(
            encoder=VideoEncoder.X264,
            level="3.1",
            color=ColorMetadata(
                "bt2020", transfer, "bt2020nc", "limited", "left"
            ),
        )


def test_hdr10_requires_complete_and_ordered_static_luminance() -> None:
    with pytest.raises(ValueError, match="both MaxCLL"):
        Hdr10Metadata(True, MASTERING, None, None)
    with pytest.raises(ValueError, match="MaxFALL"):
        Hdr10Metadata(True, MASTERING, 400, 1000)


@pytest.mark.parametrize(
    ("mastering_display", "message"),
    [
        (
            "G(50001,0)B(6550,2300)R(35400,14600)"
            "WP(15635,16450)L(10000000,1)",
            "coordinates",
        ),
        (
            "G(30000,30000)B(6550,2300)R(35400,14600)"
            "WP(15635,16450)L(10000000,1)",
            "chromaticity pairs",
        ),
        (
            "G(0,0)B(6550,2300)R(35400,14600)"
            "WP(15635,16450)L(10000000,1)",
            "chromaticity pairs",
        ),
        (
            "G(8500,39850)B(6550,2300)R(35400,14600)"
            "WP(15635,16450)L(100000001,1)",
            "luminance",
        ),
        (
            "G(8500,39850)B(6550,2300)R(35400,14600)"
            "WP(15635,16450)L(1,1)",
            "luminance",
        ),
    ],
)
def test_hdr10_mastering_display_rejects_invalid_semantic_bounds(
    mastering_display: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Hdr10Metadata(True, mastering_display, 1000, 400)


def test_hdr10_mastering_display_accepts_boundary_luminance() -> None:
    mastering_display = (
        "G(8500,39850)B(6550,2300)R(35400,14600)"
        "WP(15635,16450)L(100000000,0)"
    )

    assert Hdr10Metadata(True, mastering_display, 1000, 400).enabled is True


@pytest.mark.parametrize("field_name", ("enabled", "hdr10_opt"))
@pytest.mark.parametrize("invalid_value", (0, 1.0, "true"))
def test_hdr10_boolean_fields_require_exact_bools(
    field_name: str, invalid_value: object
) -> None:
    with pytest.raises(TypeError, match=field_name):
        Hdr10Metadata(**{field_name: invalid_value})


@pytest.mark.parametrize("field_name", ("max_cll", "max_fall"))
@pytest.mark.parametrize("invalid_value", (True, 1000.0, "1000"))
def test_hdr10_luminance_fields_require_exact_non_boolean_ints(
    field_name: str, invalid_value: object
) -> None:
    kwargs: dict[str, object] = {
        "enabled": True,
        "mastering_display": MASTERING,
        "max_cll": 1000,
        "max_fall": 400,
    }
    kwargs[field_name] = invalid_value

    with pytest.raises(TypeError, match=field_name):
        Hdr10Metadata(**kwargs)


def test_unknown_profile_override_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown encoder setting"):
        recommended_profile("x264", overrides={"magic": True})
