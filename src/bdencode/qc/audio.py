"""Audio integrity, objective analysis, and spectral evidence plans."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from ..audio import (
    EffectiveAudioPolicy,
    audio_decode_input_args,
    audio_timing_tolerance,
)


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
    duration_evidence: str | None = None
    packet_count: int | None = None


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


AUDIO_FRAME_CONTINUITY_SCHEMA_VERSION = 1
_MAX_REPORTED_AUDIO_DISCONTINUITIES = 32


@dataclass(frozen=True, slots=True)
class AudioFrameContinuity:
    """Whole-track decoded audio-frame sample-cursor evidence.

    Container timestamps can only prove continuity to their time-base
    resolution.  ``timestamp_tolerance_samples`` records that uncertainty;
    gaps and overlaps are counted only when they exceed it.  Total decoded
    samples and the normalized presentation endpoint remain independent gates,
    so matching endpoints cannot hide an internal gap followed by an overlap.
    """

    codec: str
    sample_rate: int
    time_base: str
    frame_count: int
    first_pts_seconds: Decimal
    last_pts_seconds: Decimal
    normalized_end_seconds: Decimal
    total_samples: int
    timestamp_tolerance_samples: int
    gap_count: int
    overlap_count: int
    maximum_gap_samples: int
    maximum_overlap_samples: int
    discontinuity_frame_indexes: tuple[int, ...]

    @property
    def continuous(self) -> bool:
        return self.gap_count == 0 and self.overlap_count == 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field in (
            "first_pts_seconds",
            "last_pts_seconds",
            "normalized_end_seconds",
        ):
            value[field] = str(value[field])
        value["discontinuity_frame_indexes"] = list(self.discontinuity_frame_indexes)
        value["continuous"] = self.continuous
        return value


@dataclass(frozen=True, slots=True)
class AudioFrameContinuityVerdict:
    """Source-to-encoded decoded sample-cursor comparison."""

    action: str
    source_continuous: bool
    encoded_continuous: bool
    source_sample_rate: int
    encoded_sample_rate: int
    expected_encoded_samples: int
    encoded_total_samples: int
    total_sample_delta: int
    normalized_end_delta_seconds: Decimal
    tolerance_samples: int
    tolerance_seconds: Decimal
    total_samples_within_tolerance: bool
    normalized_end_within_tolerance: bool
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["normalized_end_delta_seconds"] = str(self.normalized_end_delta_seconds)
        value["tolerance_seconds"] = str(self.tolerance_seconds)
        return value


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


@dataclass(frozen=True, slots=True)
class AudioSignalAnalysis:
    """Final whole-track signal statistics parsed from an FFmpeg analysis log.

    ``clipping_detection`` records whether the clipped-sample count came from
    an explicit analyzer counter or was derived from the final ``astats``
    sample peak and peak count.  A full-scale sample (0 dBFS) is intentionally
    treated as clipping; an ebur128 intersample true peak above 0 dBTP is kept
    separate so copy and lossless policies can report it without pretending
    that decoded PCM samples were clipped.
    """

    integrated_lufs: Decimal | None
    loudness_range_lu: Decimal | None
    true_peak_dbfs: Decimal | None
    sample_peak_dbfs: Decimal | None
    peak_count: int | None
    clipped_samples: int | None
    nan_samples: int | None
    inf_samples: int | None
    denormal_samples: int | None
    clipping_detection: str

    @property
    def missing_fields(self) -> tuple[str, ...]:
        fields = (
            "integrated_lufs",
            "loudness_range_lu",
            "true_peak_dbfs",
            "sample_peak_dbfs",
            "peak_count",
            "clipped_samples",
            "nan_samples",
            "inf_samples",
            "denormal_samples",
        )
        return tuple(name for name in fields if getattr(self, name) is None)

    @property
    def complete(self) -> bool:
        return not self.missing_fields

    @property
    def has_non_finite_samples(self) -> bool:
        return (self.nan_samples or 0) > 0 or (self.inf_samples or 0) > 0

    @property
    def has_clipping(self) -> bool:
        return (self.clipped_samples or 0) > 0

    @property
    def has_denormals(self) -> bool:
        return (self.denormal_samples or 0) > 0

    @property
    def invalid_metric_fields(self) -> tuple[str, ...]:
        invalid: list[str] = []
        for name in (
            "integrated_lufs",
            "loudness_range_lu",
            "true_peak_dbfs",
            "sample_peak_dbfs",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            # Negative infinity is the defined result for silence for
            # integrated loudness and peak measurements.  LRA, NaN and
            # positive infinity are never useful release-QC results.
            negative_infinity_is_silence = name != "loudness_range_lu"
            if (
                value.is_nan()
                or value == Decimal("Infinity")
                or (value == Decimal("-Infinity") and not negative_infinity_is_silence)
            ):
                invalid.append(name)
        return tuple(invalid)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field in (
            "integrated_lufs",
            "loudness_range_lu",
            "true_peak_dbfs",
            "sample_peak_dbfs",
        ):
            if value[field] is not None:
                value[field] = str(value[field])
        value.update(
            {
                "complete": self.complete,
                "missing_fields": list(self.missing_fields),
                "has_non_finite_samples": self.has_non_finite_samples,
                "has_clipping": self.has_clipping,
                "has_denormals": self.has_denormals,
                "invalid_metric_fields": list(self.invalid_metric_fields),
            }
        )
        return value


@dataclass(frozen=True, slots=True)
class AudioSignalVerification:
    """Fail-closed source/encode signal-safety decision."""

    action: str
    strategy: str
    lossy_transcode: bool
    source_complete: bool
    encode_complete: bool
    source_true_peak_dbfs: Decimal | None
    encode_true_peak_dbfs: Decimal | None
    true_peak_increase_db: Decimal | None
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for field in (
            "source_true_peak_dbfs",
            "encode_true_peak_dbfs",
            "true_peak_increase_db",
        ):
            if value[field] is not None:
                value[field] = str(value[field])
        value["failures"] = list(self.failures)
        value["warnings"] = list(self.warnings)
        return value


_FFMPEG_NUMBER = r"[+-]?(?:nan|inf(?:inity)?|(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)"
_LOG_PREFIX = r"(?:\[[^\]\r\n]+\]\s*)?"


def _summary_pattern(label: str, unit: str) -> re.Pattern[str]:
    return re.compile(
        rf"^\s*{_LOG_PREFIX}{label}\s*:\s*({_FFMPEG_NUMBER})\s*{unit}\s*$",
        re.IGNORECASE | re.MULTILINE,
    )


_INTEGRATED_LOUDNESS = _summary_pattern(r"I", r"LUFS")
_LOUDNESS_RANGE = _summary_pattern(r"LRA", r"LU")
_TRUE_PEAK = _summary_pattern(r"Peak", r"dBFS")
_SAMPLE_PEAK = _summary_pattern(r"Peak\s+level\s+dB", r"")
_PEAK_COUNT = _summary_pattern(r"Peak\s+count", r"")
_NAN_COUNT = _summary_pattern(r"Number\s+of\s+NaNs?", r"")
_INF_COUNT = _summary_pattern(r"Number\s+of\s+Infs?", r"")
_DENORMAL_COUNT = _summary_pattern(r"Number\s+of\s+denormals?", r"")
_EXPLICIT_CLIP_COUNT = re.compile(
    rf"^\s*{_LOG_PREFIX}(?:Number\s+of\s+)?(?:clipped\s+samples|"
    rf"clipping\s+count)\s*:\s*({_FFMPEG_NUMBER})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CLIPPING_TIMES = re.compile(
    rf"\bclipping\s+({_FFMPEG_NUMBER})\s+times\b", re.IGNORECASE
)


def _last_decimal(pattern: re.Pattern[str], document: str) -> Decimal | None:
    matches = pattern.findall(document)
    if not matches:
        return None
    token = matches[-1]
    try:
        return Decimal(token)
    except InvalidOperation as exc:
        raise ValueError(f"invalid FFmpeg analysis number: {token!r}") from exc


def _count_or_none(value: Decimal | None, *, field: str) -> int | None:
    if value is None:
        return None
    if not value.is_finite() or value < 0:
        raise ValueError(f"FFmpeg {field} must be a finite non-negative count")
    # FFmpeg formats astats counters as decimal values.  They should be whole
    # numbers, but ceiling a fractional diagnostic count is safer than hiding
    # evidence due to an analyzer formatting quirk.
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def parse_audio_analysis(document: str | bytes) -> AudioSignalAnalysis:
    """Parse the final ebur128/astats results from a whole-track FFmpeg log.

    The anchored summary expressions deliberately ignore ebur128's rolling
    progress lines.  Taking the last matching astats value selects its final
    ``Overall`` block after any per-channel values.
    """

    text = (
        document.decode("utf-8", errors="replace")
        if isinstance(document, bytes)
        else document
    )
    integrated = _last_decimal(_INTEGRATED_LOUDNESS, text)
    loudness_range = _last_decimal(_LOUDNESS_RANGE, text)
    true_peak = _last_decimal(_TRUE_PEAK, text)
    sample_peak = _last_decimal(_SAMPLE_PEAK, text)
    peak_count = _count_or_none(_last_decimal(_PEAK_COUNT, text), field="peak count")
    nan_samples = _count_or_none(
        _last_decimal(_NAN_COUNT, text), field="NaN sample count"
    )
    inf_samples = _count_or_none(
        _last_decimal(_INF_COUNT, text), field="Inf sample count"
    )
    denormal_samples = _count_or_none(
        _last_decimal(_DENORMAL_COUNT, text), field="denormal sample count"
    )

    explicit_clipping = _last_decimal(_EXPLICIT_CLIP_COUNT, text)
    if explicit_clipping is None:
        explicit_clipping = _last_decimal(_CLIPPING_TIMES, text)
    if explicit_clipping is not None:
        clipped_samples = _count_or_none(
            explicit_clipping, field="clipped sample count"
        )
        clipping_detection = "explicit_counter"
    elif sample_peak is not None and (
        sample_peak == Decimal("-Infinity")
        or (sample_peak.is_finite() and sample_peak < 0)
    ):
        clipped_samples = 0
        clipping_detection = "below_full_scale"
    elif (
        sample_peak is not None
        and sample_peak.is_finite()
        and sample_peak >= 0
        and peak_count is not None
    ):
        clipped_samples = peak_count
        clipping_detection = "full_scale_peak_count"
    else:
        clipped_samples = None
        clipping_detection = "unknown"

    return AudioSignalAnalysis(
        integrated_lufs=integrated,
        loudness_range_lu=loudness_range,
        true_peak_dbfs=true_peak,
        sample_peak_dbfs=sample_peak,
        peak_count=peak_count,
        clipped_samples=clipped_samples,
        nan_samples=nan_samples,
        inf_samples=inf_samples,
        denormal_samples=denormal_samples,
        clipping_detection=clipping_detection,
    )


def _finite_decimal(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value.is_finite() else None


def verify_audio_signal(
    source: AudioSignalAnalysis,
    encode: AudioSignalAnalysis,
    policy: EffectiveAudioPolicy,
    *,
    maximum_lossy_true_peak_dbfs: Decimal = Decimal("0"),
    near_ceiling_dbfs: Decimal = Decimal("-1.0"),
    maximum_near_ceiling_increase_db: Decimal = Decimal("0.3"),
) -> AudioSignalVerification:
    """Validate whole-track audio signal safety for an effective output policy.

    Only ``policy.strategy == \"lossy_transcode\"`` receives the lossy
    intersample-peak gates.  Copy, FLAC and DTS core extraction still fail for
    non-finite samples or decoded PCM clipping, while positive intersample
    peaks remain explicit warnings.
    """

    if maximum_near_ceiling_increase_db < 0:
        raise ValueError("maximum true-peak increase must not be negative")
    if near_ceiling_dbfs > maximum_lossy_true_peak_dbfs:
        raise ValueError("near-ceiling threshold must not exceed the peak ceiling")

    failures: list[str] = []
    warnings: list[str] = []
    for label, analysis in (("source", source), ("encode", encode)):
        if analysis.missing_fields:
            failures.append(
                f"{label} audio analysis is incomplete: "
                + ", ".join(analysis.missing_fields)
            )
        if analysis.invalid_metric_fields:
            failures.append(
                f"{label} audio analysis has invalid metrics: "
                + ", ".join(analysis.invalid_metric_fields)
            )
        if analysis.has_non_finite_samples:
            failures.append(
                f"{label} audio contains non-finite samples "
                f"(NaN={analysis.nan_samples or 0}, Inf={analysis.inf_samples or 0})"
            )
        if analysis.has_clipping:
            failures.append(
                f"{label} audio contains {analysis.clipped_samples} clipped/full-scale samples"
            )
        if analysis.has_denormals:
            warnings.append(
                f"{label} audio contains {analysis.denormal_samples} denormal samples"
            )

    source_peak = _finite_decimal(source.true_peak_dbfs)
    encode_peak = _finite_decimal(encode.true_peak_dbfs)
    peak_increase = (
        encode_peak - source_peak
        if source_peak is not None and encode_peak is not None
        else None
    )
    lossy_transcode = policy.strategy == "lossy_transcode"
    if encode_peak is not None and encode_peak > maximum_lossy_true_peak_dbfs:
        message = (
            f"encode true peak is {encode_peak} dBTP, above "
            f"{maximum_lossy_true_peak_dbfs} dBTP"
        )
        if lossy_transcode:
            failures.append(message)
        else:
            warnings.append(message)
    if source_peak is not None and source_peak > maximum_lossy_true_peak_dbfs:
        warnings.append(
            f"source true peak is {source_peak} dBTP, above "
            f"{maximum_lossy_true_peak_dbfs} dBTP"
        )
    if (
        lossy_transcode
        and source_peak is not None
        and source_peak >= near_ceiling_dbfs
        and peak_increase is not None
        and peak_increase > maximum_near_ceiling_increase_db
    ):
        failures.append(
            f"lossy transcode increased near-ceiling true peak by "
            f"{peak_increase} dB (limit {maximum_near_ceiling_increase_db} dB)"
        )

    return AudioSignalVerification(
        action=policy.action,
        strategy=policy.strategy,
        lossy_transcode=lossy_transcode,
        source_complete=source.complete,
        encode_complete=encode.complete,
        source_true_peak_dbfs=source.true_peak_dbfs,
        encode_true_peak_dbfs=encode.true_peak_dbfs,
        true_peak_increase_db=peak_increase,
        failures=tuple(failures),
        warnings=tuple(warnings),
        passed=not failures,
    )


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
    # delta while retaining the stricter one-frame start-time check.  Matroska
    # timestamps are commonly rounded to milliseconds, so lossless outputs use
    # a bounded 1 ms endpoint tolerance instead of the sub-millisecond
    # one-sample start-time tolerance.
    duration_tolerance = (
        Decimal("0.001") if policy.pcm_match_required else tolerance * Decimal(2)
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
    passed = target_structure_match and timing_match and pcm_gate and duration_match
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
        tallest_duration = duration_seconds * Decimal(tallest_window) / Decimal(height)
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


def parse_audio_frame_continuity(
    document: str | bytes | Mapping[str, Any],
) -> AudioFrameContinuity:
    """Parse a complete decoded-audio frame walk into a normalized cursor.

    The ffprobe document must contain exactly one selected stream and its
    ``nb_read_frames`` count.  This makes a truncated ``frames`` array invalid
    rather than treating its last decoded frame as the real track endpoint.
    """

    raw = json.loads(document) if isinstance(document, (str, bytes)) else document
    if not isinstance(raw, Mapping):
        raise ValueError("audio frame evidence must be a JSON object")
    streams_value = raw.get("streams")
    if not isinstance(streams_value, list):
        raise ValueError("audio frame evidence has no selected stream")
    streams = [
        item
        for item in streams_value
        if isinstance(item, Mapping) and item.get("codec_type") in (None, "audio")
    ]
    if len(streams) != 1:
        raise ValueError("audio frame evidence must contain one selected stream")
    stream = streams[0]
    frames = raw.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("audio frame evidence is empty or malformed")
    counted_frames = _strict_optional_int(stream.get("nb_read_frames"))
    if counted_frames is None or counted_frames != len(frames):
        raise ValueError("audio frame evidence is incomplete: frame count mismatch")

    stream_index = _strict_optional_int(stream.get("index"))
    stream_sample_rate = _strict_optional_int(stream.get("sample_rate"))
    if stream_sample_rate is None or stream_sample_rate <= 0:
        raise ValueError("audio frame stream sample rate must be positive")
    time_base_text = str(stream.get("time_base", ""))
    time_base_seconds = _parse_positive_time_base(time_base_text)
    timestamp_tolerance_samples = int(
        (time_base_seconds * Decimal(stream_sample_rate)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )

    first_pts: Decimal | None = None
    last_pts: Decimal | None = None
    previous_end_cursor: int | None = None
    total_samples = 0
    gap_count = 0
    overlap_count = 0
    maximum_gap_samples = 0
    maximum_overlap_samples = 0
    discontinuity_indexes: list[int] = []
    last_frame_samples = 0
    for index, frame in enumerate(frames):
        if not isinstance(frame, Mapping):
            raise ValueError(f"audio frame {index} is malformed")
        if frame.get("media_type") not in (None, "audio"):
            raise ValueError("audio frame evidence contains a non-audio frame")
        frame_stream_index = _strict_optional_int(frame.get("stream_index"))
        if (
            stream_index is not None
            and frame_stream_index is not None
            and frame_stream_index != stream_index
        ):
            raise ValueError("audio frame evidence contains another stream")
        sample_rate = _strict_optional_int(frame.get("sample_rate"))
        if sample_rate != stream_sample_rate:
            raise ValueError(f"audio frame {index} sample rate changed or is missing")
        frame_samples = _strict_optional_int(frame.get("nb_samples"))
        if frame_samples is None or frame_samples <= 0:
            raise ValueError(f"audio frame {index} has no positive sample count")
        pts = _decimal_or_none(frame.get("pts_time"))
        if pts is None:
            pts = _decimal_or_none(frame.get("best_effort_timestamp_time"))
        if pts is None or not pts.is_finite():
            raise ValueError(f"audio frame {index} has no finite presentation PTS")
        if first_pts is None:
            first_pts = pts
        normalized_pts = pts - first_pts
        cursor_decimal = normalized_pts * Decimal(stream_sample_rate)
        cursor = int(cursor_decimal.to_integral_value(rounding=ROUND_HALF_UP))
        if previous_end_cursor is not None:
            delta = cursor - previous_end_cursor
            if delta > timestamp_tolerance_samples:
                gap_count += 1
                maximum_gap_samples = max(maximum_gap_samples, delta)
                if len(discontinuity_indexes) < _MAX_REPORTED_AUDIO_DISCONTINUITIES:
                    discontinuity_indexes.append(index)
            elif delta < -timestamp_tolerance_samples:
                overlap_count += 1
                maximum_overlap_samples = max(maximum_overlap_samples, -delta)
                if len(discontinuity_indexes) < _MAX_REPORTED_AUDIO_DISCONTINUITIES:
                    discontinuity_indexes.append(index)
        previous_end_cursor = cursor + frame_samples
        total_samples += frame_samples
        last_pts = pts
        last_frame_samples = frame_samples

    assert first_pts is not None and last_pts is not None
    normalized_end = (
        last_pts - first_pts + Decimal(last_frame_samples) / Decimal(stream_sample_rate)
    )
    if normalized_end <= 0:
        raise ValueError("audio frame evidence has no positive normalized endpoint")
    return AudioFrameContinuity(
        codec=str(stream.get("codec_name", "unknown")),
        sample_rate=stream_sample_rate,
        time_base=time_base_text,
        frame_count=len(frames),
        first_pts_seconds=first_pts,
        last_pts_seconds=last_pts,
        normalized_end_seconds=normalized_end,
        total_samples=total_samples,
        timestamp_tolerance_samples=timestamp_tolerance_samples,
        gap_count=gap_count,
        overlap_count=overlap_count,
        maximum_gap_samples=maximum_gap_samples,
        maximum_overlap_samples=maximum_overlap_samples,
        discontinuity_frame_indexes=tuple(discontinuity_indexes),
    )


def compare_audio_frame_continuity(
    source: AudioFrameContinuity,
    encoded: AudioFrameContinuity,
    policy: EffectiveAudioPolicy,
) -> AudioFrameContinuityVerdict:
    """Compare normalized decoded sample counts and endpoints under policy."""

    source_sample_duration = Decimal(source.total_samples) / Decimal(source.sample_rate)
    expected_encoded_samples = int(
        (source_sample_duration * Decimal(encoded.sample_rate)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    total_sample_delta = encoded.total_samples - expected_encoded_samples
    normalized_end_delta = (
        encoded.normalized_end_seconds - source.normalized_end_seconds
    )
    tolerance_seconds = (
        Decimal("0.001")
        if policy.pcm_match_required
        else audio_timing_tolerance(policy.action, encoded.sample_rate) * Decimal(2)
    )
    tolerance_samples = int(
        (tolerance_seconds * Decimal(encoded.sample_rate)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    sample_count_match = abs(total_sample_delta) <= tolerance_samples
    endpoint_match = abs(normalized_end_delta) <= tolerance_seconds
    passed = (
        source.continuous
        and encoded.continuous
        and sample_count_match
        and endpoint_match
    )
    return AudioFrameContinuityVerdict(
        action=policy.action,
        source_continuous=source.continuous,
        encoded_continuous=encoded.continuous,
        source_sample_rate=source.sample_rate,
        encoded_sample_rate=encoded.sample_rate,
        expected_encoded_samples=expected_encoded_samples,
        encoded_total_samples=encoded.total_samples,
        total_sample_delta=total_sample_delta,
        normalized_end_delta_seconds=normalized_end_delta,
        tolerance_samples=tolerance_samples,
        tolerance_seconds=tolerance_seconds,
        total_samples_within_tolerance=sample_count_match,
        normalized_end_within_tolerance=endpoint_match,
        passed=passed,
    )


def _parse_positive_time_base(value: str) -> Decimal:
    parts = value.split("/", 1)
    if len(parts) != 2:
        raise ValueError("audio frame stream time base is missing or malformed")
    try:
        numerator = Decimal(parts[0])
        denominator = Decimal(parts[1])
    except InvalidOperation as exc:
        raise ValueError("audio frame stream time base is malformed") from exc
    if (
        not numerator.is_finite()
        or not denominator.is_finite()
        or numerator <= 0
        or denominator <= 0
    ):
        raise ValueError("audio frame stream time base must be finite and positive")
    return numerator / denominator


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
    if sample_rate <= 0:
        raise ValueError("audio probe sample rate must be positive")
    stream_start = _decimal_or_none(item.get("start_time"))
    packet_duration, packet_start, packet_count = _packet_tail_duration(raw, item)
    start_time = (
        stream_start
        if stream_start is not None
        else packet_start
        if packet_start is not None
        else Decimal(0)
    )
    stream_duration = _decimal_or_none(item.get("duration"))
    sample_count = _optional_int(item.get("nb_samples"))
    if sample_count is not None and sample_count < 0:
        raise ValueError("audio probe sample count must not be negative")

    # Packet evidence is authoritative whenever the command returned packets.
    # A stream-level sample count or duration remains a track-specific fallback
    # for legacy/foreign probe documents, but the multiplexed container duration
    # must never stand in for an audio track that ended early.
    if packet_duration is not None:
        duration = packet_duration - start_time
        duration_evidence = "complete_packet_tail"
    elif sample_count is not None:
        duration = Decimal(sample_count) / Decimal(sample_rate)
        duration_evidence = "stream_sample_count"
    else:
        duration = stream_duration
        duration_evidence = "stream_duration" if duration is not None else None
    if duration is not None and (not duration.is_finite() or duration <= 0):
        raise ValueError("audio track duration must be finite and positive")
    return AudioProbe(
        codec=str(item.get("codec_name", "unknown")),
        profile=_optional_str(item.get("profile")),
        bit_rate=_optional_int(item.get("bit_rate")),
        sample_rate=sample_rate,
        channels=int(item["channels"]),
        channel_layout=item.get("channel_layout"),
        sample_count=sample_count,
        start_time=start_time,
        duration=duration,
        bits_per_raw_sample=_optional_int(item.get("bits_per_raw_sample")),
        duration_evidence=duration_evidence,
        packet_count=packet_count,
    )


def _packet_tail_duration(
    raw: Mapping[str, Any], stream: Mapping[str, Any]
) -> tuple[Decimal | None, Decimal | None, int | None]:
    """Return the absolute selected-audio packet tail and first packet PTS.

    ``audio_probe_command`` asks ffprobe both to emit every selected packet and
    to count the packets it read.  Requiring those counts to agree prevents a
    partial/truncated JSON document from being accepted as whole-track tail
    evidence.
    """

    packets_value = raw.get("packets")
    if packets_value is None:
        return None, None, None
    if not isinstance(packets_value, list) or not packets_value:
        raise ValueError("audio packet-tail evidence is empty or malformed")
    packet_count = _optional_int(stream.get("nb_read_packets"))
    if packet_count is None or packet_count != len(packets_value):
        raise ValueError(
            "audio packet-tail evidence is incomplete: packet count mismatch"
        )

    stream_index = _optional_int(stream.get("index"))
    first_pts: Decimal | None = None
    last_end: Decimal | None = None
    for index, packet in enumerate(packets_value):
        if not isinstance(packet, Mapping):
            raise ValueError(f"audio packet {index} is malformed")
        packet_stream_index = _optional_int(packet.get("stream_index"))
        if (
            stream_index is not None
            and packet_stream_index is not None
            and packet_stream_index != stream_index
        ):
            raise ValueError("audio packet-tail evidence contains another stream")
        pts = _decimal_or_none(packet.get("pts_time"))
        if pts is None:
            pts = _decimal_or_none(packet.get("dts_time"))
        packet_duration = _decimal_or_none(packet.get("duration_time"))
        if (
            pts is None
            or packet_duration is None
            or not pts.is_finite()
            or not packet_duration.is_finite()
            or packet_duration < 0
        ):
            raise ValueError(f"audio packet {index} has no finite endpoint evidence")
        packet_end = pts + packet_duration
        first_pts = pts if first_pts is None else min(first_pts, pts)
        last_end = packet_end if last_end is None else max(last_end, packet_end)
    assert first_pts is not None and last_end is not None
    return last_end, first_pts, packet_count


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value in (None, "N/A") else Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    try:
        return None if value in (None, "N/A", "") else int(value)
    except (TypeError, ValueError):
        return None


def _strict_optional_int(value: Any) -> int | None:
    """Parse ffprobe integer scalars without accepting bools or floats."""

    if value in (None, "N/A", ""):
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not valid integer audio-frame evidence")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    raise ValueError("audio-frame integer evidence is malformed")


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
        "-count_packets",
        "-show_packets",
        "-show_entries",
        "stream=index,codec_type,codec_name,profile,bit_rate,sample_rate,channels,channel_layout,nb_samples,nb_read_packets,start_time,duration,bits_per_raw_sample:packet=stream_index,pts_time,dts_time,duration_time",
        "-of",
        "json",
        str(path),
    ]


def audio_frame_continuity_probe_command(
    path: Path,
    stream: int = 0,
    *,
    input_codec: str = "unknown",
    ffprobe: str = "ffprobe",
) -> list[str]:
    """Decode and report every selected audio frame with cursor evidence."""

    return [
        ffprobe,
        "-v",
        "error",
        *audio_decode_input_args(input_codec),
        "-select_streams",
        f"a:{stream}",
        "-count_frames",
        "-show_frames",
        "-show_streams",
        "-show_entries",
        "stream=index,codec_type,codec_name,sample_rate,time_base,nb_read_frames:frame=media_type,stream_index,pts_time,best_effort_timestamp_time,nb_samples,sample_rate",
        "-of",
        "json",
        str(path),
    ]


def pcm_hash_command(
    path: Path,
    stream: int = 0,
    *,
    input_codec: str = "unknown",
    ffmpeg: str = "ffmpeg",
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
        *audio_decode_input_args(input_codec),
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
    path: Path,
    stream: int,
    log_path: Path,
    *,
    input_codec: str = "unknown",
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        *audio_decode_input_args(input_codec),
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
    input_codec: str = "unknown",
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
        *audio_decode_input_args(input_codec),
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
        filter_value = f"{labels}vstack=inputs={len(inputs)},format=rgb48be[spectrum]"
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
