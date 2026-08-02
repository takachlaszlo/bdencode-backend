from __future__ import annotations

import pytest

from bdencode.media import (
    ColorMetadata,
    DetailLevel,
    EncoderSettings,
    Hdr10Metadata,
    Tune,
    VideoEncoder,
    profile_schema,
    recommended_profile,
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
    assert "8x8dct=1" in args[args.index("-x264-params") + 1]


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
    "kwargs, message",
    [
        ({"crf": float("nan")}, "CRF"),
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


def test_hdr10_requires_complete_and_ordered_static_luminance() -> None:
    with pytest.raises(ValueError, match="both MaxCLL"):
        Hdr10Metadata(True, MASTERING, None, None)
    with pytest.raises(ValueError, match="MaxFALL"):
        Hdr10Metadata(True, MASTERING, 400, 1000)


def test_unknown_profile_override_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown encoder setting"):
        recommended_profile("x264", overrides={"magic": True})
