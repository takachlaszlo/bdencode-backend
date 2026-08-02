"""CPU-only language inference plans with conservative consensus rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from .language import LanguageDecision, LanguageResolver


_ISO1_TO_ISO2T = {
    "ar": "ara",
    "bg": "bul",
    "bn": "ben",
    "bs": "bos",
    "ca": "cat",
    "cs": "ces",
    "cy": "cym",
    "da": "dan",
    "de": "deu",
    "el": "ell",
    "en": "eng",
    "es": "spa",
    "et": "est",
    "eu": "eus",
    "fa": "fas",
    "fi": "fin",
    "fr": "fra",
    "ga": "gle",
    "he": "heb",
    "hi": "hin",
    "hr": "hrv",
    "hu": "hun",
    "hy": "hye",
    "id": "ind",
    "is": "isl",
    "it": "ita",
    "ja": "jpn",
    "ka": "kat",
    "kk": "kaz",
    "ko": "kor",
    "lt": "lit",
    "lv": "lav",
    "mk": "mkd",
    "ms": "msa",
    "my": "mya",
    "nl": "nld",
    "no": "nor",
    "pa": "pan",
    "pl": "pol",
    "pt": "por",
    "ro": "ron",
    "ru": "rus",
    "si": "sin",
    "sk": "slk",
    "sl": "slv",
    "sq": "sqi",
    "sr": "srp",
    "sv": "swe",
    "ta": "tam",
    "te": "tel",
    "th": "tha",
    "tr": "tur",
    "uk": "ukr",
    "ur": "urd",
    "vi": "vie",
    "zh": "zho",
}


@dataclass(frozen=True, slots=True)
class SampleWindow:
    start_seconds: float
    duration_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.start_seconds < 0 or self.duration_seconds <= 0:
            raise ValueError("invalid language sample window")


@dataclass(frozen=True, slots=True)
class ContentLanguageSample:
    code: str | None
    confidence: float
    speech_seconds: float
    window: SampleWindow

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1 or self.speech_seconds < 0:
            raise ValueError("invalid language sample result")


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    iso639_2t: str | None
    confidence: float
    agreement: float
    usable_samples: int
    needs_review: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sample_windows(
    duration_seconds: float,
    *,
    count: int = 6,
    sample_duration: float = 30.0,
) -> tuple[SampleWindow, ...]:
    if duration_seconds <= 0 or count < 1 or sample_duration <= 0:
        raise ValueError("duration, count and sample duration must be positive")
    if duration_seconds <= sample_duration:
        return (SampleWindow(0, duration_seconds),)
    # Avoid logos/recaps and end credits while still covering the whole feature.
    earliest = min(
        duration_seconds * 0.08, max(0.0, duration_seconds - sample_duration)
    )
    latest = max(earliest, duration_seconds * 0.92 - sample_duration)
    if count == 1:
        starts = [(earliest + latest) / 2]
    else:
        starts = [
            earliest + index * (latest - earliest) / (count - 1)
            for index in range(count)
        ]
    return tuple(
        SampleWindow(round(start, 3), min(sample_duration, duration_seconds - start))
        for start in starts
    )


def audio_sample_command(
    input_path: Path,
    stream_index: int,
    window: SampleWindow,
    output_wav: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    if stream_index < 0:
        raise ValueError("stream index cannot be negative")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-ss",
        f"{window.start_seconds:.3f}",
        "-t",
        f"{window.duration_seconds:.3f}",
        "-i",
        str(input_path),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-y",
        str(output_wav),
    ]


def subtitle_ocr_command(
    sup_path: Path,
    *,
    ocr_language: str,
    seconv: str = "seconv",
) -> list[str]:
    if not ocr_language or any(
        char not in "abcdefghijklmnopqrstuvwxyz-" for char in ocr_language.lower()
    ):
        raise ValueError("invalid OCR language")
    return [
        seconv,
        str(sup_path),
        "subrip",
        "--ocr-engine:tesseract",
        f"--ocr-language:{ocr_language.lower()}",
    ]


def _normalize_detected(code: str | None) -> str | None:
    if not code:
        return None
    normalized = code.strip().lower().removeprefix("__label__")
    if len(normalized) > 2:
        normalized = normalized.split("-", 1)[0]
    return _ISO1_TO_ISO2T.get(normalized)


def language_consensus(
    samples: Sequence[ContentLanguageSample],
    *,
    minimum_speech_seconds: float = 4.0,
    minimum_usable_samples: int = 3,
    minimum_agreement: float = 2 / 3,
    minimum_confidence: float = 0.75,
) -> ConsensusResult:
    usable = [
        (_normalize_detected(item.code), item)
        for item in samples
        if item.speech_seconds >= minimum_speech_seconds and item.confidence > 0
    ]
    usable = [(code, item) for code, item in usable if code]
    if len(usable) < minimum_usable_samples:
        return ConsensusResult(
            None, 0.0, 0.0, len(usable), True, "insufficient_speech_samples"
        )
    weights: dict[str, float] = {}
    counts: dict[str, int] = {}
    for code, item in usable:
        assert code is not None
        weights[code] = weights.get(code, 0.0) + item.confidence
        counts[code] = counts.get(code, 0) + 1
    selected = max(weights, key=weights.get)
    agreement = counts[selected] / len(usable)
    confidence = weights[selected] / counts[selected]
    accepted = agreement >= minimum_agreement and confidence >= minimum_confidence
    return ConsensusResult(
        selected if accepted else None,
        round(confidence if accepted else 0.0, 4),
        round(agreement, 4),
        len(usable),
        not accepted or confidence < 0.9,
        "consensus" if accepted else "language_conflict_or_low_confidence",
    )


class FasterWhisperModel(Protocol):
    def transcribe(self, audio: str, **kwargs: Any) -> tuple[Iterable[Any], Any]: ...


def detect_with_faster_whisper(
    model: FasterWhisperModel,
    wav_path: Path,
    window: SampleWindow,
) -> ContentLanguageSample:
    segments, info = model.transcribe(
        str(wav_path),
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
        without_timestamps=False,
    )
    materialized = list(segments)
    speech = sum(max(0.0, float(item.end) - float(item.start)) for item in materialized)
    return ContentLanguageSample(
        code=getattr(info, "language", None),
        confidence=float(getattr(info, "language_probability", 0.0)),
        speech_seconds=speech,
        window=window,
    )


def merge_content_inference(
    resolver: LanguageResolver,
    consensus: ConsensusResult,
    *,
    mpls: str | None,
    clpi: str | None,
    pmt: str | None,
    subtitle: bool = False,
) -> LanguageDecision:
    kwargs: dict[str, Any] = {"mpls": mpls, "clpi": clpi, "pmt": pmt}
    if subtitle:
        kwargs.update(
            subtitle_ocr=consensus.iso639_2t, subtitle_confidence=consensus.confidence
        )
    else:
        kwargs.update(
            audio_lid=consensus.iso639_2t, audio_confidence=consensus.confidence
        )
    return resolver.resolve(**kwargs)
