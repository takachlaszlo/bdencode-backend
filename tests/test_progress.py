from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdencode.models import JobState
from bdencode.progress import (
    EncodeProgressReporter,
    FFmpegProgressParser,
    encoding_overall_progress,
    pipeline_progress_baseline,
)


def _block(parser: FFmpegProgressParser, **values: object):
    result = None
    for key, value in values.items():
        result = parser.feed_line(f"{key}={value}") or result
    return result


def test_ffmpeg_progress_parser_computes_monotonic_fraction_and_eta() -> None:
    parser = FFmpegProgressParser(100.0)

    first = _block(
        parser,
        frame=240,
        fps="23.98",
        out_time_us=25_000_000,
        speed="0.50x",
        progress="continue",
    )
    assert first is not None
    assert first.stage_fraction == pytest.approx(0.25)
    assert first.frame == 240
    assert first.fps == pytest.approx(23.98)
    assert first.speed == pytest.approx(0.5)
    assert first.eta_seconds == pytest.approx(150.0)

    # A timestamp regression can occur around filters/concatenation. The
    # operator-facing meter must never move backwards.
    regressed = _block(
        parser,
        out_time="00:00:20.000000",
        fps="N/A",
        speed="N/A",
        progress="continue",
    )
    assert regressed is not None
    assert regressed.stage_fraction == pytest.approx(0.25)
    assert regressed.fps is None
    assert regressed.speed is None
    assert regressed.eta_seconds is None

    finished = _block(parser, out_time_us="invalid", progress="end")
    assert finished is not None
    assert finished.stage_fraction == 1.0
    assert finished.out_time_seconds == 100.0


def test_ffmpeg_progress_parser_ignores_regular_stderr_and_invalid_values() -> None:
    parser = FFmpegProgressParser(60.0)
    assert parser.feed_line("[libx264 @ 0x123] profile High, level 4.1") is None
    assert parser.feed_line("unrelated=value") is None
    result = _block(
        parser,
        frame="-3",
        out_time_ms=30_000_000,
        fps="nan",
        speed="-1x",
        progress="continue",
    )
    assert result is not None
    assert result.stage_fraction == 0.5
    assert result.frame is None
    assert result.fps is None
    assert result.speed is None


def test_reporter_throttles_database_updates_and_keeps_jsonl(
    tmp_path: Path,
) -> None:
    now = [10.0]
    records: list[tuple[float, str, dict[str, object]]] = []
    output = tmp_path / "video-progress.jsonl"
    reporter = EncodeProgressReporter(
        100.0,
        output,
        lambda progress, message, details: records.append(
            (progress, message, details)
        ),
        minimum_db_interval=2.0,
        monotonic_clock=lambda: now[0],
    )

    reporter.start()
    for line in (
        "frame=100",
        "fps=20.0",
        "out_time_us=10000000",
        "speed=0.50x",
        "progress=continue",
    ):
        reporter.handle_line(line)
    now[0] += 1.0
    for line in ("out_time_us=20000000", "progress=continue"):
        reporter.handle_line(line)
    now[0] += 2.0
    for line in ("out_time_us=30000000", "progress=continue"):
        reporter.handle_line(line)

    # start + first protocol block + third protocol block; the middle block is
    # still in JSONL but intentionally avoids an extra SQLite write.
    assert len(records) == 3
    assert records[0][0] == pipeline_progress_baseline(JobState.ENCODING)
    assert records[-1][0] == pytest.approx(encoding_overall_progress(0.3))
    assert "Videó kódolása: 30.0%" in records[-1][1]
    assert "fps" in records[1][1]
    assert "ETA" in records[1][1]
    assert records[0][2]["milestone_percent"] == 0
    assert records[1][2]["milestone_percent"] == 10
    assert records[-1][2]["milestone_percent"] == 25
    lines = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [item["stage_fraction"] for item in lines] == [0.1, 0.2, 0.3]


def test_reporter_observation_failure_never_escapes(tmp_path: Path) -> None:
    def fail_record(*_args):
        raise RuntimeError("database temporarily unavailable")

    reporter = EncodeProgressReporter(10.0, tmp_path, fail_record)
    reporter.start()
    reporter.handle_line("out_time_us=1000000")
    reporter.handle_line("progress=continue")
    reporter.complete()


def test_reporter_never_follows_progress_jsonl_symlink(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    external = tmp_path / "external-progress.txt"
    external.write_text("SENTINEL\n", encoding="utf-8")
    linked_progress = tmp_path / "encode-progress.jsonl"
    try:
        linked_progress.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    reporter = EncodeProgressReporter(
        10.0,
        linked_progress,
        lambda *_args: None,
    )

    reporter.handle_line("out_time_us=1000000")
    reporter.handle_line("progress=continue")
    reporter.complete()

    assert external.read_text(encoding="utf-8") == "SENTINEL\n"
    assert "progress JSONL path cannot be a symbolic link" in caplog.text
