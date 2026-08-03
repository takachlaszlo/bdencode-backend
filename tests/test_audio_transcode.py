from __future__ import annotations

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
