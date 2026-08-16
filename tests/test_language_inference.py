from __future__ import annotations

from pathlib import Path

from bdencode.media.inference import (
    ContentLanguageSample,
    SampleWindow,
    audio_sample_command,
    language_consensus,
    sample_windows,
)


def _sample(
    code: str, confidence: float = 0.9, speech: float = 20
) -> ContentLanguageSample:
    return ContentLanguageSample(code, confidence, speech, SampleWindow(0))


def test_windows_cover_feature_without_intro_or_credits() -> None:
    windows = sample_windows(7200, count=6)
    assert len(windows) == 6
    assert windows[0].start_seconds > 0
    assert windows[-1].start_seconds + windows[-1].duration_seconds < 7200


def test_audio_sample_is_mono_16k_and_stream_specific() -> None:
    command = audio_sample_command(
        Path("reference.mkv"), 3, SampleWindow(90), Path("sample.wav")
    )
    assert command[command.index("-map") + 1] == "0:a:3"
    assert command[command.index("-ac") + 1] == "1"
    assert command[command.index("-ar") + 1] == "16000"


def test_consensus_accepts_repeated_language() -> None:
    result = language_consensus(
        [_sample("en"), _sample("en", 0.95), _sample("de", 0.8), _sample("en")]
    )
    assert result.iso639_2t == "eng"
    assert result.agreement == 0.75


def test_music_or_conflicting_samples_require_review() -> None:
    no_speech = language_consensus([_sample("en", speech=1), _sample("en", speech=0)])
    assert no_speech.needs_review and no_speech.iso639_2t is None
    conflict = language_consensus([_sample("en"), _sample("de"), _sample("fr")])
    assert conflict.needs_review and conflict.iso639_2t is None
