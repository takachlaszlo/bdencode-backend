from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pytest

import bdencode.media.language_runtime as language_runtime
from bdencode.media.language_runtime import (
    AudioLanguageRuntime,
    LanguageInferenceUnavailable,
    infer_audio_language,
)


def _wav_bytes() -> bytes:
    # The runtime only needs a valid, non-empty RIFF/WAVE extraction artifact;
    # faster-whisper is replaced by a fake in these unit tests.
    return b"RIFF" + (64).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 64


class FakeRunner:
    def __init__(self, *, valid_wav: bool = True) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.valid_wav = valid_wav

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path | None = None,
        env: Any = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        check: bool = True,
        ok_returncodes: Sequence[int] = (0,),
        timeout: float | None = None,
    ) -> None:
        command = tuple(os.fspath(item) for item in argv)
        self.commands.append(command)
        output = Path(command[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_wav_bytes() if self.valid_wav else b"not-wave")
        if stderr_path:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_path.write_text("", encoding="utf-8")


@dataclass
class Segment:
    start: float = 0.0
    end: float = 20.0


@dataclass
class Info:
    language: str
    language_probability: float


class FakeModel:
    def __init__(self, languages: list[str] | None = None) -> None:
        self.languages = languages or ["en"] * 6
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def transcribe(self, audio: str, **kwargs: Any):
        index = len(self.calls)
        self.calls.append((audio, kwargs))
        return iter([Segment()]), Info(self.languages[index], 0.92)


class CapturingFactory:
    def __init__(self, model: FakeModel, *, error: Exception | None = None) -> None:
        self.model = model
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, model_name: str, **kwargs: Any):
        self.calls.append((model_name, kwargs))
        if self.error:
            raise self.error
        return self.model


@pytest.fixture
def media(tmp_path: Path) -> tuple[Path, Path, Path]:
    reference = tmp_path / "reference.mkv"
    reference.write_bytes(b"mock-reference-matroska")
    return reference, tmp_path / "work", tmp_path / "data"


def test_cpu_int8_runtime_extracts_six_samples_and_returns_provenance(media):
    reference, work, data = media
    runner = FakeRunner()
    model = FakeModel()
    factory = CapturingFactory(model)
    runtime = AudioLanguageRuntime(data, model_factory=factory)
    assert runtime.model_loaded is False

    result = runtime.infer(reference, 3, 7200, work, runner)

    assert result["consensus"]["iso639_2t"] == "eng"
    assert result["consensus"]["usable_samples"] == 6
    assert result["provenance"]["sample_count_actual"] == 6
    assert len(result["samples"]) == 6
    assert len(runner.commands) == 6
    assert runtime.model_loaded is True
    assert factory.calls == [
        (
            "small",
            {
                "device": "cpu",
                "compute_type": "int8",
                "download_root": str(data / "cache" / "faster-whisper"),
                "cpu_threads": 0,
                "num_workers": 1,
            },
        )
    ]
    for command in runner.commands:
        assert command[command.index("-map") + 1] == "0:a:3"
        assert command[command.index("-t") + 1] == "30.000"
        assert command[command.index("-ac") + 1] == "1"
        assert command[command.index("-ar") + 1] == "16000"
    assert all(call[1]["vad_filter"] is True for call in model.calls)
    json.dumps(result)  # explicitly JSON-compatible


def test_result_and_wavs_are_idempotent_without_loading_second_model(media):
    reference, work, data = media
    runner = FakeRunner()
    first_model = FakeModel()
    first = AudioLanguageRuntime(data, model_factory=CapturingFactory(first_model))
    expected = first.infer(reference, 1, 7200, work, runner)
    sample_mtimes = {
        path.name: path.stat().st_mtime_ns
        for path in work.joinpath("language", "audio-00001").glob("*.wav")
    }

    second_factory = CapturingFactory(FakeModel())
    restarted = AudioLanguageRuntime(data, model_factory=second_factory)
    actual = restarted.infer(reference, 1, 7200, work, runner)

    assert actual == expected
    assert len(runner.commands) == 6
    assert second_factory.calls == []
    assert restarted.model_loaded is False
    assert sample_mtimes == {
        path.name: path.stat().st_mtime_ns
        for path in work.joinpath("language", "audio-00001").glob("*.wav")
    }


def test_conflicting_detected_languages_return_review_consensus(media):
    reference, work, data = media
    model = FakeModel(["en", "de", "fr", "en", "de", "fr"])
    result = infer_audio_language(
        reference,
        2,
        7200,
        work,
        FakeRunner(),
        data_root=data,
        model_factory=CapturingFactory(model),
    )
    assert result["consensus"]["iso639_2t"] is None
    assert result["consensus"]["needs_review"] is True
    assert result["consensus"]["reason"] == "language_conflict_or_low_confidence"


def test_model_initialization_error_is_controlled_and_does_not_leak_details(media):
    reference, work, data = media
    secret_text = "authorization=do-not-leak https://private.invalid/model"
    factory = CapturingFactory(FakeModel(), error=RuntimeError(secret_text))
    runtime = AudioLanguageRuntime(data, model_factory=factory)

    with pytest.raises(LanguageInferenceUnavailable) as raised:
        runtime.infer(reference, 0, 7200, work, FakeRunner())

    assert raised.value.reason_code == "model_initialization_failed"
    assert "do-not-leak" not in str(raised.value)
    assert "private.invalid" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_missing_optional_dependency_is_controlled(media, monkeypatch):
    reference, work, data = media

    def unavailable():
        raise LanguageInferenceUnavailable("dependency_missing")

    monkeypatch.setattr(language_runtime, "_load_whisper_class", unavailable)
    runtime = AudioLanguageRuntime(data)
    with pytest.raises(LanguageInferenceUnavailable) as raised:
        runtime.infer(reference, 0, 7200, work, FakeRunner())
    assert raised.value.reason_code == "dependency_missing"
    assert runtime.model_loaded is False


def test_invalid_extracted_wav_has_controlled_failure(media):
    reference, work, data = media
    factory = CapturingFactory(FakeModel())
    runtime = AudioLanguageRuntime(data, model_factory=factory)

    with pytest.raises(LanguageInferenceUnavailable) as raised:
        runtime.infer(reference, 0, 7200, work, FakeRunner(valid_wav=False))

    assert raised.value.reason_code == "invalid_sample"
    assert factory.calls == []
    assert runtime.model_loaded is False


def test_input_validation_happens_before_optional_model_import(media):
    reference, work, data = media
    runtime = AudioLanguageRuntime(data, model_factory=CapturingFactory(FakeModel()))
    with pytest.raises(ValueError, match="audio_ordinal"):
        runtime.infer(reference, -1, 7200, work, FakeRunner())
    with pytest.raises(ValueError, match="duration_seconds"):
        runtime.infer(reference, 0, float("nan"), work, FakeRunner())
