from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from bdencode.encode import encode_pipeline_commands
from bdencode.media.profiles import (
    _MATRICES,
    _PRIMARIES,
    _TRANSFERS,
    ColorMetadata,
    VideoEncoder,
    recommended_profile,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs a real ffmpeg"
)


def _encoders() -> str:
    if shutil.which("ffmpeg") is None:
        return ""
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout


def test_color_input_args_repeat_the_colour_options_the_encoder_is_given() -> None:
    uhd = recommended_profile(
        VideoEncoder.X265,
        color=ColorMetadata("bt2020", "smpte2084", "bt2020nc", "limited", "left"),
    )
    assert uhd.ffmpeg_color_input_args() == (
        "-color_primaries", "bt2020",
        "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc",
        "-color_range", "tv",
    )
    bluray = recommended_profile(VideoEncoder.X264)
    assert bluray.ffmpeg_color_input_args() == (
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
    )
    full = replace(bluray, color=replace(bluray.color, range="full"))
    assert full.ffmpeg_color_input_args()[-2:] == ("-color_range", "pc")
    # Whatever the encoder is told to write, the input is described the same way.
    output = list(uhd.ffmpeg_video_args())
    for name, value in zip(
        uhd.ffmpeg_color_input_args()[::2], uhd.ffmpeg_color_input_args()[1::2]
    ):
        assert output[output.index(name) + 1] == value


def test_encode_pipeline_describes_the_untagged_y4m_before_reading_it() -> None:
    settings = recommended_profile(
        VideoEncoder.X265,
        color=ColorMetadata("bt2020", "smpte2084", "bt2020nc", "limited", "left"),
    )

    _vspipe, ffmpeg = encode_pipeline_commands(
        Path("source.vpy"), Path("video.mkv"), settings
    )

    demuxer = ffmpeg.index("yuv4mpegpipe")
    source = ffmpeg.index("pipe:0")
    assert ffmpeg[demuxer + 1 : source - 1] == list(settings.ffmpeg_color_input_args())
    assert ffmpeg[source - 1] == "-i"
    # ...and the output options are still there, after the input.
    assert ffmpeg.index("-c:v") > source
    assert ffmpeg.count("-colorspace") == 2


@needs_ffmpeg
@pytest.mark.parametrize(
    "option, values",
    [
        ("primaries", sorted(_PRIMARIES)),
        ("transfer", sorted(_TRANSFERS)),
        ("matrix", sorted(_MATRICES)),
    ],
)
def test_ffmpeg_accepts_every_colour_name_the_profiles_allow_as_input_options(
    tmp_path: Path, option: str, values: list[str]
) -> None:
    tiny = tmp_path / "tiny.y4m"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
            "color=c=gray:s=16x16:r=1:d=1", "-pix_fmt", "yuv420p", "-f",
            "yuv4mpegpipe", "-y", str(tiny),
        ],
        check=True,
    )
    # x265 is the only encoder that may carry PQ/HLG; the name checks are the same.
    base = recommended_profile(VideoEncoder.X265)
    for value in values:
        settings = replace(base, color=replace(base.color, **{option: value}))
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-f",
                "yuv4mpegpipe", *settings.ffmpeg_color_input_args(), "-i",
                str(tiny), "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, (value, completed.stderr)


def _psnr(encoded: Path, source: Path) -> float:
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(encoded), "-i", str(source),
            "-lavfi", "[0:v][1:v]psnr", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    match = re.search(r"PSNR.*average:(\S+)", completed.stderr)
    assert match, completed.stderr[-400:]
    return 99.0 if match.group(1) == "inf" else float(match.group(1))


def _source_clip(tmp_path: Path, pixel_format: str, colour: ColorMetadata) -> Path:
    clip = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=320x180:rate=24:duration=1", "-vf",
            f"format={pixel_format}", "-c:v", "ffv1",
            "-color_primaries", colour.primaries, "-color_trc", colour.transfer,
            "-colorspace", colour.matrix, "-color_range", "tv", "-y", str(clip),
        ],
        check=True,
    )
    return clip


@needs_ffmpeg
@pytest.mark.parametrize(
    "encoder, pixel_format, colour",
    [
        (
            VideoEncoder.X264,
            "yuv420p",
            ColorMetadata("bt709", "bt709", "bt709", "limited", "left"),
        ),
        (
            VideoEncoder.X265,
            "yuv420p10le",
            ColorMetadata("bt2020", "smpte2084", "bt2020nc", "limited", "left"),
        ),
    ],
)
def test_real_ffmpeg_encode_keeps_the_pixels_of_an_untagged_y4m_stream(
    tmp_path: Path, encoder: VideoEncoder, pixel_format: str, colour: ColorMetadata
) -> None:
    library = "libx264" if encoder is VideoEncoder.X264 else "libx265"
    if library not in _encoders():
        pytest.skip(f"this ffmpeg has no {library}")
    source = _source_clip(tmp_path, pixel_format, colour)
    # What vspipe sends: planar video in a Y4M header without colour properties.
    untagged = tmp_path / "untagged.y4m"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-i", str(source),
            "-vf", "setparams=colorspace=unknown:color_primaries=unknown"
            ":color_trc=unknown:range=unknown",
            "-strict", "-1", "-f", "yuv4mpegpipe", "-y", str(untagged),
        ],
        check=True,
    )
    settings = replace(
        recommended_profile(encoder, color=colour), preset="ultrafast", crf=1.0
    )
    output = tmp_path / "video.mkv"
    _vspipe, ffmpeg = encode_pipeline_commands(Path("unused.vpy"), output, settings)

    with untagged.open("rb") as stdin:
        completed = subprocess.run(
            ffmpeg, stdin=stdin, capture_output=True, text=True
        )

    assert completed.returncode == 0, completed.stderr[-600:]
    # Without describing the input FFmpeg 7 converts the matrix and lands near
    # 35 dB (BT.709) or 26 dB (BT.2020) against the source, at any CRF.
    assert _psnr(output, source) > 50
