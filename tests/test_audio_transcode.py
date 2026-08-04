from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from bdencode.audio import (
    audio_encode_args,
    audio_output_channels,
    effective_audio_policy,
)
from bdencode.qc.audio import AudioProbe, verify_audio_output


@pytest.mark.parametrize(
    ("action", "codec", "bitrate"),
    (("ac3", "ac3", "640k"), ("eac3", "eac3", "1024k"), ("dts", "dca", "1536k")),
)
def test_lossy_audio_presets_are_explicit_and_5_1_limited(
    action: str, codec: str, bitrate: str
) -> None:
    command = audio_encode_args(
        action,
        source_codec="truehd",
        source_profile="TrueHD",
        source_channels=8,
        source_sample_rate=96_000,
    )

    assert command[command.index("-c:a") + 1] == codec
    assert command[command.index("-b:a") + 1] == bitrate
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-ac") + 1] == "6"
    assert (
        ("-strict", "-2") == tuple(command[-2:])
        if action == "dts"
        else "-strict" not in command
    )


def test_dts_hd_uses_embedded_core_without_reencoding() -> None:
    policy = effective_audio_policy(
        "dts",
        source_codec="dts",
        source_profile="DTS-HD MA",
        source_channels=8,
        source_sample_rate=48_000,
    )

    assert policy.strategy == "dts_core_extract"
    assert policy.verification_mode == "dts_core_extract"
    assert policy.channels == 6
    assert audio_encode_args(
        "dts",
        source_codec="dts",
        source_profile="DTS-HD MA",
        source_channels=8,
        source_sample_rate=48_000,
    ) == ["-c:a", "copy", "-bsf:a", "dca_core"]


def test_plain_dts_target_is_passthrough_and_rare_three_channel_encode_is_stereo() -> (
    None
):
    assert audio_encode_args(
        "dts",
        source_codec="dts",
        source_profile="DTS",
        source_channels=6,
        source_sample_rate=48_000,
    ) == ["-c:a", "copy"]
    assert (
        audio_output_channels("dts", 3, source_codec="truehd", source_profile="TrueHD")
        == 2
    )


def test_lossy_qc_accepts_codec_padding_without_requiring_pcm_hash() -> None:
    source = AudioProbe(
        "truehd", 48_000, 8, "7.1", 480_000, Decimal("0"), Decimal("10"), 24
    )
    encode = AudioProbe(
        "ac3",
        48_000,
        6,
        "5.1(side)",
        480_768,
        Decimal("0"),
        Decimal("10.016"),
        None,
        bit_rate=640_000,
    )
    policy = effective_audio_policy(
        "ac3",
        source_codec="truehd",
        source_profile="TrueHD",
        source_channels=8,
        source_sample_rate=48_000,
    )

    verification = verify_audio_output(
        source, encode, policy, decoded_pcm_sha256_match=None
    )

    assert verification.passed
    assert verification.decoded_pcm_sha256_required is False
    assert verification.target_structure_match
    assert verification.duration_within_tolerance


def test_lossy_qc_allows_two_codec_frames_of_container_duration_rounding() -> None:
    source = AudioProbe(
        "dts",
        48_000,
        6,
        "5.1(side)",
        None,
        Decimal("0.042"),
        Decimal("6479.557"),
        24,
        profile="DTS-HD MA",
    )
    encode = AudioProbe(
        "eac3",
        48_000,
        6,
        "5.1(side)",
        None,
        Decimal("0.037"),
        Decimal("6479.515"),
        None,
        bit_rate=1_024_000,
    )
    policy = effective_audio_policy(
        "eac3",
        source_codec="dts",
        source_profile="DTS-HD MA",
        source_channels=6,
        source_sample_rate=48_000,
    )

    verification = verify_audio_output(
        source, encode, policy, decoded_pcm_sha256_match=None
    )

    assert verification.passed
    assert verification.timing_tolerance_seconds == Decimal("0.032")
    assert verification.duration_tolerance_seconds == Decimal("0.064")


def test_lossless_qc_accepts_missing_source_layout_when_pcm_is_identical() -> None:
    source = AudioProbe(
        "pcm_s24le",
        48_000,
        2,
        None,
        None,
        Decimal("0.042"),
        Decimal("6807.847"),
        24,
    )
    encode = AudioProbe(
        "flac",
        48_000,
        2,
        "stereo",
        None,
        Decimal("0.042"),
        Decimal("6807.847"),
        24,
    )
    policy = effective_audio_policy(
        "flac",
        source_codec="pcm_s24le",
        source_channels=2,
        source_sample_rate=48_000,
    )

    verification = verify_audio_output(
        source, encode, policy, decoded_pcm_sha256_match=True
    )

    assert verification.passed
    assert verification.target_structure_match
    assert verification.decoded_pcm_sha256_match is True

    declared_layout = replace(source, channel_layout="stereo")
    wrong_layout = replace(encode, channel_layout="2.0")
    assert not verify_audio_output(
        declared_layout,
        wrong_layout,
        policy,
        decoded_pcm_sha256_match=True,
    ).passed


def test_lossy_qc_rejects_wrong_bitrate_and_lossless_qc_still_requires_pcm() -> None:
    source = AudioProbe(
        "truehd", 48_000, 8, "7.1", 480_000, Decimal("0"), Decimal("10"), 24
    )
    wrong = AudioProbe(
        "ac3",
        48_000,
        6,
        "5.1(side)",
        480_768,
        Decimal("0"),
        Decimal("10.016"),
        None,
        bit_rate=448_000,
    )
    lossy = effective_audio_policy(
        "ac3", source_codec="truehd", source_channels=8, source_sample_rate=48_000
    )
    assert not verify_audio_output(
        source, wrong, lossy, decoded_pcm_sha256_match=None
    ).passed

    copied = effective_audio_policy(
        "copy", source_codec="truehd", source_channels=8, source_sample_rate=48_000
    )
    assert not verify_audio_output(
        source, source, copied, decoded_pcm_sha256_match=False
    ).passed
