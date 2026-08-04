"""Audio integrity, objective analysis, and spectral evidence plans."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Mapping

from ..audio import EffectiveAudioPolicy, audio_timing_tolerance


@dataclass(frozen=True, slots=True)
class AudioProbe:
    codec: str
    sample_rate: int
    channels: int
    channel_layout: str | None
    sample_count: int | None
    start_time: Decimal
    duration: Decimal | None
    bits_per_raw_sample: int | None
    profile: str | None = None
    bit_rate: int | None = None


@dataclass(frozen=True, slots=True)
class AudioComparison:
    sample_rate_match: bool
    channels_match: bool
    channel_layout_match: bool
    sample_count_delta: int | None
    delay_seconds: Decimal
    duration_delta_seconds: Decimal | None

    @property
    def structurally_lossless(self) -> bool:
        return (
            self.sample_rate_match
            and self.channels_match
            and self.channel_layout_match
            and self.sample_count_delta == 0
            and self.delay_seconds == 0
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["delay_seconds"] = str(self.delay_seconds)
        if self.duration_delta_seconds is not None:
            result["duration_delta_seconds"] = str(self.duration_delta_seconds)
        return result


@dataclass(frozen=True, slots=True)
class AudioVerification:
    verification_mode: str
    expected_codec: str
    expected_sample_rate: int
    expected_channels: int
    codec_match: bool
    bitrate_match: bool
    sample_rate_match: bool
    channels_match: bool
    target_structure_match: bool
    decoded_pcm_sha256_required: bool
    decoded_pcm_sha256_match: bool | None
    timing_within_tolerance: bool
    duration_within_tolerance: bool
    timing_tolerance_seconds: Decimal
    duration_tolerance_seconds: Decimal
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["timing_tolerance_seconds"] = str(self.timing_tolerance_seconds)
        value["duration_tolerance_seconds"] = str(self.duration_tolerance_seconds)
        return value


def verify_audio_output(
    source: AudioProbe,
    encode: AudioProbe,
    policy: EffectiveAudioPolicy,
    *,
    decoded_pcm_sha256_match: bool | None,
) -> AudioVerification:
    comparison = compare_audio_probes(source, encode)
    expected_codec = source.codec if policy.action == "copy" else policy.codec_name
    expected_sample_rate = (
        source.sample_rate if policy.sample_rate is None else policy.sample_rate
    )
    expected_channels = source.channels if policy.channels is None else policy.channels
    codec_match = encode.codec.casefold() == expected_codec.casefold()
    expected_bit_rate = (
        policy.bitrate_kbps * 1000 if policy.bitrate_kbps is not None else None
    )
    bitrate_match = expected_bit_rate is None or encode.bit_rate == expected_bit_rate
    sample_rate_match = encode.sample_rate == expected_sample_rate
    channels_match = encode.channels == expected_channels
    if policy.pcm_match_required:
        # Some Blu-ray PCM streams do not carry a channel-layout label even
        # though their channel count and decoded samples are unambiguous.  A
        # missing source label must not make an otherwise bit-identical FLAC
        # transcode fail.  When the source does declare a layout, keep
        # requiring the encoded track to preserve it.
        channel_layout_compatible = (
            source.channel_layout is None or comparison.channel_layout_match
        )
        target_structure_match = (
            codec_match
            and comparison.sample_rate_match
            and comparison.channels_match
            and channel_layout_compatible
        )
    else:
        target_structure_match = (
            codec_match and bitrate_match and sample_rate_match and channels_match
        )
    tolerance = (
        Decimal(1) / Decimal(expected_sample_rate)
        if policy.pcm_match_required
        else audio_timing_tolerance(policy.action, expected_sample_rate)
    )
    timing_match = abs(comparison.delay_seconds) <= tolerance
    # Lossy source and target codecs can independently round their container
    # endpoints by one codec frame.  Allow two target frames for the duration
    # delta while retaining the stricter one-frame start-time check.
    duration_tolerance = (
        tolerance if policy.pcm_match_required else tolerance * Decimal(2)
    )
    duration_match = (
        comparison.duration_delta_seconds is not None
        and abs(comparison.duration_delta_seconds) <= duration_tolerance
    )
    pcm_gate = (
        decoded_pcm_sha256_match is True
        if policy.pcm_match_required
        else decoded_pcm_sha256_match is None
    )
    passed = (
        target_structure_match
        and timing_match
        and pcm_gate
        and (policy.pcm_match_required or duration_match)
    )
    return AudioVerification(
        verification_mode=policy.verification_mode,
        expected_codec=expected_codec,
        expected_sample_rate=expected_sample_rate,
        expected_channels=expected_channels,
        codec_match=codec_match,
        bitrate_match=bitrate_match,
        sample_rate_match=sample_rate_match,
        channels_match=channels_match,
        target_structure_match=target_structure_match,
        decoded_pcm_sha256_required=policy.pcm_match_required,
        decoded_pcm_sha256_match=decoded_pcm_sha256_match,
        timing_within_tolerance=timing_match,
        duration_within_tolerance=duration_match,
        timing_tolerance_seconds=tolerance,
        duration_tolerance_seconds=duration_tolerance,
        passed=passed,
    )


MAX_SPECTRUM_WINDOW_SECONDS = Decimal("300")


@dataclass(frozen=True, slots=True)
class SpectrumWindow:
    index: int
    start_seconds: Decimal
    duration_seconds: Decimal
    height: int


def plan_spectrum_windows(
    duration_seconds: Decimal,
    *,
    height: int = 2160,
    max_window_seconds: Decimal = MAX_SPECTRUM_WINDOW_SECONDS,
) -> tuple[SpectrumWindow, ...]:
    """Split a whole track into equally sized, bounded-memory spectrum windows."""
    if duration_seconds <= 0:
        raise ValueError("spectrum duration must be positive")
    if height < 1:
        raise ValueError("spectrum height must be positive")
    if max_window_seconds <= 0:
        raise ValueError("maximum spectrum window must be positive")
    count = int(
        (duration_seconds / max_window_seconds).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    while True:
        if count > height:
            raise ValueError("spectrum has more windows than vertical pixels")
        base_height, extra_pixels = divmod(height, count)
        tallest_window = base_height + (1 if extra_pixels else 0)
        tallest_duration = (
            duration_seconds * Decimal(tallest_window) / Decimal(height)
        )
        if tallest_duration <= max_window_seconds:
            break
        count += 1
    windows: list[SpectrumWindow] = []
    start = Decimal(0)
    for index in range(count):
        window_height = base_height + (1 if index < extra_pixels else 0)
        window_duration = (
            duration_seconds - start
            if index == count - 1
            else duration_seconds * Decimal(window_height) / Decimal(height)
        )
        windows.append(
            SpectrumWindow(
                index=index,
                start_seconds=start,
                duration_seconds=window_duration,
                height=window_height,
            )
        )
        start += window_duration
    return tuple(windows)


def parse_audio_probe(document: str | bytes | Mapping[str, Any]) -> AudioProbe:
    raw = json.loads(document) if isinstance(document, (str, bytes)) else document
    streams = [
        item
        for item in raw.get("streams", [])
        if item.get("codec_type") in (None, "audio")
    ]
    if len(streams) != 1:
        raise ValueError("audio probe must contain exactly one selected stream")
    item = streams[0]
    sample_rate = int(item["sample_rate"])
    stream_duration = _decimal_or_none(item.get("duration"))
    duration = stream_duration
    if duration is None:
        format_details = raw.get("format", {})
        if isinstance(format_details, Mapping):
            duration = _decimal_or_none(format_details.get("duration"))
    sample_count = _optional_int(item.get("nb_samples"))
    if sample_count is None and stream_duration is not None:
        sample_count = int((stream_duration * sample_rate).to_integral_value())
    return AudioProbe(
        codec=str(item.get("codec_name", "unknown")),
        profile=_optional_str(item.get("profile")),
        bit_rate=_optional_int(item.get("bit_rate")),
        sample_rate=sample_rate,
        channels=int(item["channels"]),
        channel_layout=item.get("channel_layout"),
        sample_count=sample_count,
        start_time=Decimal(str(item.get("start_time", "0"))),
        duration=duration,
        bits_per_raw_sample=_optional_int(item.get("bits_per_raw_sample")),
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "N/A") else Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    try:
        return None if value in (None, "N/A", "") else int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "N/A", "", "unknown") else str(value)


def compare_audio_probes(source: AudioProbe, encoded: AudioProbe) -> AudioComparison:
    count_delta = None
    if source.sample_count is not None and encoded.sample_count is not None:
        count_delta = encoded.sample_count - source.sample_count
    duration_delta = None
    if source.duration is not None and encoded.duration is not None:
        duration_delta = encoded.duration - source.duration
    return AudioComparison(
        sample_rate_match=source.sample_rate == encoded.sample_rate,
        channels_match=source.channels == encoded.channels,
        channel_layout_match=source.channel_layout == encoded.channel_layout,
        sample_count_delta=count_delta,
        delay_seconds=encoded.start_time - source.start_time,
        duration_delta_seconds=duration_delta,
    )


def audio_probe_command(
    path: Path, stream: int = 0, *, ffprobe: str = "ffprobe"
) -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        f"a:{stream}",
        "-show_entries",
        "stream=codec_type,codec_name,profile,bit_rate,sample_rate,channels,channel_layout,nb_samples,start_time,duration,bits_per_raw_sample:format=duration",
        "-of",
        "json",
        str(path),
    ]


def pcm_hash_command(
    path: Path, stream: int = 0, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """Hash canonical decoded PCM without depending on decoder frame boundaries.

    FFmpeg's ``framemd5`` output also records packet sizes, timestamps and frame
    boundaries.  Those can legitimately differ after a lossless FLAC transcode
    even when every decoded sample is identical.  The hash muxer instead hashes
    the canonical signed 32-bit PCM payload directly and emits only a tiny
    SHA-256 record, so a feature-length multichannel track never has to be
    materialized as raw PCM on disk.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        f"0:a:{stream}",
        "-c:a",
        "pcm_s32le",
        "-f",
        "hash",
        "-hash",
        "sha256",
        "-",
    ]


def analysis_command(
    path: Path, stream: int, log_path: Path, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-i",
        str(path),
        "-map",
        f"0:a:{stream}",
        "-af",
        "ebur128=peak=true,astats=metadata=1:reset=0,aphasemeter=video=0:phasing=1",
        "-f",
        "null",
        "-",
    ]


def spectrum_command(
    path: Path,
    stream: int,
    output_path: Path,
    *,
    start_seconds: Decimal,
    duration_seconds: Decimal,
    width: int = 3840,
    height: int = 2160,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if width < 320 or height < 1:
        raise ValueError("spectrum image is too small")
    if start_seconds < 0:
        raise ValueError("spectrum start must not be negative")
    if not 0 < duration_seconds <= MAX_SPECTRUM_WINDOW_SECONDS:
        raise ValueError("spectrum window must be between 0 and 300 seconds")
    # Fixed scale/legend settings are shared by source and encode for visual parity.
    filter_value = (
        f"[0:a:{stream}]showspectrumpic=s={width}x{height}:legend=0:"
        "color=intensity:scale=log:fscale=log:win_func=blackman:"
        "orientation=horizontal,format=rgb48be[spectrum]"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "warning",
        "-ss",
        str(start_seconds),
        "-t",
        str(duration_seconds),
        "-i",
        str(path),
        "-filter_complex",
        filter_value,
        "-map",
        "[spectrum]",
        "-an",
        "-frames:v",
        "1",
        "-c:v",
        "png",
        "-pix_fmt",
        "rgb48be",
        "-compression_level",
        "6",
        "-update",
        "1",
        "-y",
        str(output_path),
    ]


def spectrum_stitch_command(
    inputs: tuple[Path, ...],
    output_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if not inputs:
        raise ValueError("at least one spectrum window is required")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "warning"]
    for path in inputs:
        command.extend(("-i", str(path)))
    if len(inputs) == 1:
        filter_value = "[0:v:0]format=rgb48be[spectrum]"
    else:
        labels = "".join(f"[{index}:v:0]" for index in range(len(inputs)))
        filter_value = (
            f"{labels}vstack=inputs={len(inputs)},format=rgb48be[spectrum]"
        )
    command.extend(
        (
            "-filter_complex",
            filter_value,
            "-map",
            "[spectrum]",
            "-an",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-pix_fmt",
            "rgb48be",
            "-compression_level",
            "6",
            "-update",
            "1",
            "-y",
            str(output_path),
        )
    )
    return command


def flac_encode_args(*, compression_level: int = 8) -> list[str]:
    if not 0 <= compression_level <= 12:
        raise ValueError("FLAC compression level must be between 0 and 12")
    return ["-c:a", "flac", "-compression_level", str(compression_level)]
