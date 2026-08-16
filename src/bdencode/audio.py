"""Deterministic audio output presets shared by planning, encoding and QC."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any


AUDIO_ACTIONS = ("copy", "flac", "ac3", "eac3", "dts", "omit")
LOSSLESS_AUDIO_ACTIONS = frozenset({"copy", "flac"})
LOSSY_AUDIO_ACTIONS = frozenset({"ac3", "eac3", "dts"})
AUDIO_DECODE_POLICY_SCHEMA_VERSION = 1

_AUDIO_CODEC_ALIASES = {
    "ac3": "ac3",
    "ac3fixed": "ac3",
    "dolbydigital": "ac3",
    "eac3": "eac3",
    "eac3secondary": "eac3",
    "dolbydigitalplus": "eac3",
}


@dataclass(frozen=True, slots=True)
class AudioTranscodePreset:
    action: str
    label: str
    encoder: str
    codec_name: str
    bitrate_kbps: int
    sample_rate: int = 48_000
    max_channels: int = 6
    experimental: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["lossy"] = True
        return value


AUDIO_TRANSCODE_PRESETS: dict[str, AudioTranscodePreset] = {
    "ac3": AudioTranscodePreset(
        action="ac3",
        label="AC-3",
        encoder="ac3",
        codec_name="ac3",
        bitrate_kbps=640,
    ),
    "eac3": AudioTranscodePreset(
        action="eac3",
        label="E-AC-3",
        encoder="eac3",
        codec_name="eac3",
        bitrate_kbps=1024,
    ),
    "dts": AudioTranscodePreset(
        action="dts",
        label="DTS core",
        encoder="dca",
        codec_name="dts",
        bitrate_kbps=1536,
        experimental=True,
    ),
}


@dataclass(frozen=True, slots=True)
class EffectiveAudioPolicy:
    action: str
    strategy: str
    verification_mode: str
    encoder: str
    codec_name: str
    bitrate_kbps: int | None
    sample_rate: int | None
    channels: int | None
    bit_depth: int | None = None

    @property
    def pcm_match_required(self) -> bool:
        return self.verification_mode == "lossless_pcm"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["pcm_match_required"] = self.pcm_match_required
        return value


def normalize_audio_action(action: str) -> str:
    normalized = action.casefold()
    if normalized not in AUDIO_ACTIONS:
        raise ValueError("audio action must be copy, flac, ac3, eac3, dts or omit")
    return normalized


def normalize_audio_codec_name(codec: str) -> str:
    """Return a stable codec token for decoder-policy decisions.

    FFprobe, MediaInfo, and Blu-ray scanners use several punctuation and alias
    variants for AC-3 and E-AC-3.  Canonicalization is deliberately based on
    exact normalized tokens so an unrelated codec name containing ``ac3`` can
    never accidentally enable an AC-3 decoder option.
    """

    if not isinstance(codec, str):
        raise ValueError("audio codec name must be a string")
    value = codec.strip().casefold()
    if not value:
        raise ValueError("audio codec name must not be empty")
    token = "".join(
        character for character in value if character.isascii() and character.isalnum()
    )
    if not token:
        raise ValueError("audio codec name has no ASCII alphanumeric token")
    return _AUDIO_CODEC_ALIASES.get(token, token)


def audio_decode_input_args(input_codec: str) -> list[str]:
    """Return codec-aware FFmpeg input options for deterministic PCM decode."""

    normalized = normalize_audio_codec_name(input_codec)
    return ["-drc_scale", "0"] if normalized in {"ac3", "eac3"} else []


def is_lossless_audio_action(action: str) -> bool:
    return normalize_audio_action(action) in LOSSLESS_AUDIO_ACTIONS


def is_lossy_audio_action(action: str) -> bool:
    return normalize_audio_action(action) in LOSSY_AUDIO_ACTIONS


def _is_dts_source(source_codec: str | None, source_profile: str | None) -> bool:
    value = f"{source_codec or ''} {source_profile or ''}".casefold()
    return "dts" in value or "dca" in value


def _is_dts_hd_source(source_codec: str | None, source_profile: str | None) -> bool:
    value = f"{source_codec or ''} {source_profile or ''}".casefold()
    compact = "".join(character for character in value if character.isalnum())
    return _is_dts_source(source_codec, source_profile) and "hd" in compact


def audio_output_channels(
    action: str,
    source_channels: int | None,
    *,
    source_codec: str | None = None,
    source_profile: str | None = None,
) -> int | None:
    """Return the deterministic output channel count for an audio action.

    The Debian FFmpeg ``dca`` encoder accepts mono, stereo, quad, 5.0 and 5.1.
    A rare 3-channel source therefore uses a stereo DTS target instead of an
    implicit and unsupported three-channel layout.
    """

    normalized = normalize_audio_action(action)
    if normalized not in LOSSY_AUDIO_ACTIONS:
        return source_channels
    if source_channels is None or source_channels < 1:
        raise ValueError(
            f"source channel count is required for {normalized} transcoding"
        )
    if normalized == "dts" and _is_dts_source(source_codec, source_profile):
        if _is_dts_hd_source(source_codec, source_profile):
            return min(source_channels, 6)
        return source_channels
    channels = min(source_channels, AUDIO_TRANSCODE_PRESETS[normalized].max_channels)
    if normalized == "dts" and channels == 3:
        return 2
    return channels


def effective_audio_policy(
    action: str,
    *,
    source_codec: str,
    source_profile: str | None = None,
    source_channels: int | None = None,
    source_sample_rate: int | None = None,
    source_bit_depth: int | None = None,
) -> EffectiveAudioPolicy:
    normalized = normalize_audio_action(action)
    if normalized == "omit":
        raise ValueError("omit does not produce an audio stream")
    if normalized == "copy":
        return EffectiveAudioPolicy(
            normalized,
            "copy",
            "lossless_pcm",
            "copy",
            source_codec,
            None,
            source_sample_rate,
            source_channels,
            source_bit_depth,
        )
    if normalized == "flac":
        return EffectiveAudioPolicy(
            normalized,
            "lossless_transcode",
            "lossless_pcm",
            "flac",
            "flac",
            None,
            source_sample_rate,
            source_channels,
            source_bit_depth,
        )
    if normalized == "dts" and _is_dts_source(source_codec, source_profile):
        core_extract = _is_dts_hd_source(source_codec, source_profile)
        return EffectiveAudioPolicy(
            normalized,
            "dts_core_extract" if core_extract else "copy",
            "dts_core_extract" if core_extract else "lossless_pcm",
            "copy",
            "dts",
            None,
            48_000 if core_extract else source_sample_rate,
            audio_output_channels(
                normalized,
                source_channels,
                source_codec=source_codec,
                source_profile=source_profile,
            ),
            source_bit_depth,
        )

    preset = AUDIO_TRANSCODE_PRESETS[normalized]
    return EffectiveAudioPolicy(
        normalized,
        "lossy_transcode",
        "lossy_transcode",
        preset.encoder,
        preset.codec_name,
        preset.bitrate_kbps,
        preset.sample_rate,
        audio_output_channels(
            normalized,
            source_channels,
            source_codec=source_codec,
            source_profile=source_profile,
        ),
        None,
    )


def flac_sample_format(source_bit_depth: int | None) -> str | None:
    """Return the lossless FFmpeg sample format for a reviewed source depth.

    FFmpeg represents 24-bit integer samples in its 32-bit sample format.  A
    16-bit Blu-ray PCM source must stay ``s16``; promoting it to ``s32`` makes
    the FLAC advertise 24 bits even though the extra low bits are only zero
    padding.  Unknown depth remains automatic rather than being guessed.
    """

    if source_bit_depth is None:
        return None
    if source_bit_depth == 16:
        return "s16"
    if source_bit_depth == 24:
        return "s32"
    raise ValueError(
        "FLAC source bit depth must be confirmed as 16 or 24 bits; "
        f"got {source_bit_depth}"
    )


def audio_encode_args(
    action: str,
    *,
    source_codec: str = "unknown",
    source_profile: str | None = None,
    source_channels: int | None = None,
    source_sample_rate: int | None = None,
    source_bit_depth: int | None = None,
) -> list[str]:
    policy = effective_audio_policy(
        action,
        source_codec=source_codec,
        source_profile=source_profile,
        source_channels=source_channels,
        source_sample_rate=source_sample_rate,
        source_bit_depth=source_bit_depth,
    )
    if policy.strategy == "copy":
        return ["-c:a", "copy"]
    if policy.strategy == "dts_core_extract":
        return ["-c:a", "copy", "-bsf:a", "dca_core"]
    if policy.action == "flac":
        args = ["-c:a", "flac", "-compression_level", "8"]
        sample_format = flac_sample_format(source_bit_depth)
        if sample_format is not None:
            args.extend(("-sample_fmt", sample_format))
        return args

    preset = AUDIO_TRANSCODE_PRESETS[policy.action]
    assert policy.channels is not None
    args = [
        "-c:a",
        preset.encoder,
        "-b:a",
        f"{preset.bitrate_kbps}k",
        "-ar",
        str(preset.sample_rate),
        "-ac",
        str(policy.channels),
    ]
    if preset.experimental:
        args.extend(("-strict", "-2"))
    return args


def expected_audio_codec(action: str, source_codec: str) -> str:
    normalized = normalize_audio_action(action)
    if normalized == "copy":
        return source_codec
    if normalized == "flac":
        return "flac"
    if normalized in AUDIO_TRANSCODE_PRESETS:
        return AUDIO_TRANSCODE_PRESETS[normalized].codec_name
    raise ValueError("omitted audio has no expected output codec")


def audio_timing_tolerance(action: str, sample_rate: int) -> Decimal:
    """Return one relevant codec-frame of timing tolerance in seconds."""

    if sample_rate < 1:
        raise ValueError("sample rate must be positive")
    normalized = normalize_audio_action(action)
    frame_samples = {
        "copy": 1,
        "flac": 1,
        "ac3": 1536,
        "eac3": 1536,
        # DTS core frame sizes vary with mode; 2048 samples is a conservative
        # upper bound for container duration rounding and encoder padding.
        "dts": 2048,
    }.get(normalized)
    if frame_samples is None:
        raise ValueError("omitted audio has no timing tolerance")
    return Decimal(frame_samples) / Decimal(sample_rate)


def audio_presets_payload() -> dict[str, dict[str, Any]]:
    return {
        action: preset.to_dict() for action, preset in AUDIO_TRANSCODE_PRESETS.items()
    }
