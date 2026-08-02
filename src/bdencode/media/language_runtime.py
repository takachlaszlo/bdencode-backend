"""CPU-only runtime adapter for audio content-language inference.

The planning primitives live in :mod:`bdencode.media.inference`; this module
adds the deliberately small amount of stateful runtime behavior needed by the
worker: deterministic WAV extraction, lazy model loading and durable evidence.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from bdencode.process import CommandRunner
from bdencode.utils import atomic_write_json, sha256_file

from .inference import (
    ContentLanguageSample,
    FasterWhisperModel,
    SampleWindow,
    audio_sample_command,
    detect_with_faster_whisper,
    language_consensus,
    sample_windows,
)


MODEL_NAME = "small"
DEVICE = "cpu"
COMPUTE_TYPE = "int8"
SAMPLE_COUNT = 6
SAMPLE_DURATION_SECONDS = 30.0


class LanguageInferenceUnavailable(RuntimeError):
    """A controlled, user-safe failure of the optional inference runtime."""

    _MESSAGES = {
        "dependency_missing": "audio language inference is unavailable because the optional faster-whisper dependency is not installed",
        "model_initialization_failed": "audio language inference is unavailable because the speech model could not be initialized",
        "sample_extraction_failed": "audio language inference is unavailable because an audio sample could not be extracted",
        "invalid_sample": "audio language inference is unavailable because an extracted WAV sample is invalid",
        "transcription_failed": "audio language inference is unavailable because speech analysis failed",
    }

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(
            self._MESSAGES.get(reason_code, "audio language inference is unavailable")
        )


class ModelFactory(Protocol):
    def __call__(self, model_name: str, **kwargs: Any) -> FasterWhisperModel: ...


def _load_whisper_class() -> ModelFactory:
    try:
        from faster_whisper import WhisperModel
    except Exception:
        # Missing Python modules and unavailable native CTranslate2 libraries
        # can surface as ImportError, OSError or RuntimeError depending on OS.
        raise LanguageInferenceUnavailable("dependency_missing") from None
    return WhisperModel


def _package_version() -> str | None:
    try:
        return importlib.metadata.version("faster-whisper")
    except Exception:
        return None


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    import hashlib

    return hashlib.sha256(encoded).hexdigest()


def _wav_is_valid(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 44:
            return False
        with path.open("rb") as handle:
            header = handle.read(12)
        return header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    except OSError:
        return False


def _window_value(window: SampleWindow) -> dict[str, float]:
    return {
        "start_seconds": window.start_seconds,
        "duration_seconds": window.duration_seconds,
    }


class AudioLanguageRuntime:
    """Lazy faster-whisper adapter suitable for one persistent worker process.

    Constructing this object performs no optional imports and no network I/O.
    The first uncached :meth:`infer` call initializes a single ``small`` model
    with CPU/int8 settings; later calls reuse it.
    """

    def __init__(
        self,
        data_root: Path,
        *,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.data_root = data_root.expanduser().resolve(strict=False)
        self.model_cache = self.data_root / "cache" / "faster-whisper"
        self._model_factory = model_factory
        self._model: FasterWhisperModel | None = None

    @property
    def model_loaded(self) -> bool:
        return self._model is not None

    def _get_model(self) -> FasterWhisperModel:
        if self._model is not None:
            return self._model
        try:
            self.model_cache.mkdir(mode=0o750, parents=True, exist_ok=True)
        except OSError:
            raise LanguageInferenceUnavailable("model_initialization_failed") from None
        factory = self._model_factory or _load_whisper_class()
        try:
            model = factory(
                MODEL_NAME,
                device=DEVICE,
                compute_type=COMPUTE_TYPE,
                download_root=str(self.model_cache),
                cpu_threads=0,
                num_workers=1,
            )
        except LanguageInferenceUnavailable:
            raise
        except Exception:
            # Model download libraries can include authorization headers, URLs
            # or cache internals in their exception text. Do not propagate it to
            # logs, the API or provenance.
            raise LanguageInferenceUnavailable("model_initialization_failed") from None
        self._model = model
        return model

    def infer(
        self,
        reference_mkv: Path,
        stream_index: int,
        duration_seconds: float,
        work_dir: Path,
        runner: CommandRunner,
        *,
        source_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Infer one global audio stream and return a JSON-compatible record.

        WAV files and their markers live below ``work_dir/language/stream-N``.
        A completed result is reusable without loading faster-whisper when all
        source/model/sample fingerprints still match.
        """
        if (
            isinstance(stream_index, bool)
            or not isinstance(stream_index, int)
            or stream_index < 0
        ):
            raise ValueError("stream_index must be a non-negative integer")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(float(duration_seconds))
            or duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be finite and positive")
        source = reference_mkv.expanduser().resolve(strict=True)
        if not source.is_file() or source.suffix.lower() != ".mkv":
            raise ValueError("reference_mkv must be an existing Matroska file")
        root = work_dir.expanduser().resolve(strict=False)
        root.mkdir(mode=0o750, parents=True, exist_ok=True)
        stream_root = root / "language" / f"stream-{stream_index:05d}"
        stream_root.mkdir(mode=0o750, parents=True, exist_ok=True)

        source_stat = source.stat()
        if source_sha256 is not None and (
            len(source_sha256) != 64
            or any(
                character not in "0123456789abcdefABCDEF" for character in source_sha256
            )
        ):
            raise ValueError("source_sha256 must be a hexadecimal SHA-256 digest")
        source_record: dict[str, Any] = {
            "path": str(source),
            "sha256": (source_sha256 or sha256_file(source)).lower(),
            "size_bytes": source_stat.st_size,
            "stream_index": stream_index,
            "duration_seconds": float(duration_seconds),
        }
        windows = sample_windows(
            float(duration_seconds),
            count=SAMPLE_COUNT,
            sample_duration=SAMPLE_DURATION_SECONDS,
        )
        samples = self._extract_samples(
            source,
            stream_index,
            windows,
            stream_root,
            runner,
            source_record,
        )
        engine = {
            "name": "faster-whisper",
            "package_version": _package_version(),
            "model": MODEL_NAME,
            "device": DEVICE,
            "compute_type": COMPUTE_TYPE,
            "cache_directory": str(self.model_cache),
            "vad_filter": True,
            "beam_size": 1,
            "condition_on_previous_text": False,
        }
        inference_input = {
            "schema_version": 1,
            "source": source_record,
            "engine": engine,
            "samples": [
                {
                    "index": item["index"],
                    "window": item["window"],
                    "wav_sha256": item["wav_sha256"],
                    "wav_size_bytes": item["wav_size_bytes"],
                }
                for item in samples
            ],
        }
        input_hash = _canonical_hash(inference_input)
        result_path = stream_root / "language-inference.json"
        cached = self._read_cached_result(result_path, input_hash)
        if cached is not None:
            return cached

        model = self._get_model()
        detections: list[ContentLanguageSample] = []
        sample_results: list[dict[str, Any]] = []
        for item, window in zip(samples, windows, strict=True):
            wav_path = Path(item["wav_path"])
            try:
                detected = detect_with_faster_whisper(model, wav_path, window)
            except Exception:
                raise LanguageInferenceUnavailable("transcription_failed") from None
            detections.append(detected)
            sample_results.append(
                {
                    **item,
                    "detection": {
                        "code": detected.code,
                        "confidence": detected.confidence,
                        "speech_seconds": detected.speech_seconds,
                    },
                }
            )
        consensus = language_consensus(detections)
        result: dict[str, Any] = {
            "schema_version": 1,
            "input_sha256": input_hash,
            "source": source_record,
            "engine": engine,
            "samples": sample_results,
            "consensus": consensus.to_dict(),
            "provenance": {
                "sample_count_requested": SAMPLE_COUNT,
                "sample_duration_seconds": SAMPLE_DURATION_SECONDS,
                "sample_count_actual": len(samples),
                "stream_addressing": "global_input_stream_index",
                "sample_audio": "pcm_s16le_mono_16000_hz",
                "result_path": str(result_path),
            },
        }
        atomic_write_json(result_path, result)
        return result

    def _extract_samples(
        self,
        source: Path,
        stream_index: int,
        windows: Sequence[SampleWindow],
        stream_root: Path,
        runner: CommandRunner,
        source_record: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for index, window in enumerate(windows, start=1):
            start_ms = round(window.start_seconds * 1000)
            duration_ms = round(window.duration_seconds * 1000)
            stem = f"sample-{index:02d}-{start_ms:010d}-{duration_ms:06d}"
            wav_path = stream_root / f"{stem}.wav"
            marker_path = stream_root / f"{stem}.json"
            command = audio_sample_command(source, stream_index, window, wav_path)
            marker_input = {
                "schema_version": 1,
                "source_sha256": source_record["sha256"],
                "stream_index": stream_index,
                "window": _window_value(window),
                "command": command,
            }
            marker_hash = _canonical_hash(marker_input)
            marker = self._valid_sample_marker(marker_path, wav_path, marker_hash)
            if marker is None:
                try:
                    runner.run(
                        command,
                        cwd=stream_root,
                        stderr_path=stream_root / f"{stem}.stderr.log",
                    )
                except Exception:
                    raise LanguageInferenceUnavailable(
                        "sample_extraction_failed"
                    ) from None
                if not _wav_is_valid(wav_path):
                    raise LanguageInferenceUnavailable("invalid_sample") from None
                marker = {
                    **marker_input,
                    "input_sha256": marker_hash,
                    "wav_path": str(wav_path),
                    "wav_sha256": sha256_file(wav_path),
                    "wav_size_bytes": wav_path.stat().st_size,
                }
                atomic_write_json(marker_path, marker)
            result.append(
                {
                    "index": index,
                    "window": _window_value(window),
                    "wav_path": str(wav_path),
                    "wav_sha256": marker["wav_sha256"],
                    "wav_size_bytes": marker["wav_size_bytes"],
                    "marker_path": str(marker_path),
                }
            )
        return result

    @staticmethod
    def _valid_sample_marker(
        marker_path: Path, wav_path: Path, input_hash: str
    ) -> dict[str, Any] | None:
        if not marker_path.is_file() or not _wav_is_valid(wav_path):
            return None
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker.get("input_sha256") != input_hash:
                return None
            if marker.get("wav_path") != str(wav_path):
                return None
            if marker.get("wav_size_bytes") != wav_path.stat().st_size:
                return None
            if marker.get("wav_sha256") != sha256_file(wav_path):
                return None
            return marker
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _read_cached_result(path: Path, input_hash: str) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("input_sha256") != input_hash:
                return None
            if not isinstance(value.get("consensus"), dict):
                return None
            if not isinstance(value.get("samples"), list):
                return None
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


def infer_audio_language(
    reference_mkv: Path,
    stream_index: int,
    duration_seconds: float,
    work_dir: Path,
    runner: CommandRunner,
    *,
    data_root: Path,
    model_factory: ModelFactory | None = None,
) -> dict[str, Any]:
    """One-shot integration helper; prefer a shared runtime in a worker."""
    return AudioLanguageRuntime(data_root, model_factory=model_factory).infer(
        reference_mkv,
        stream_index,
        duration_seconds,
        work_dir,
        runner,
    )


__all__ = [
    "AudioLanguageRuntime",
    "LanguageInferenceUnavailable",
    "infer_audio_language",
]
