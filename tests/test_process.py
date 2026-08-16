from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bdencode.process import (
    CommandRunner,
    DiagnosticCategory,
    DiagnosticSeverity,
    ProcessInterrupted,
    classify_media_diagnostics,
)


def test_pipeline_tees_last_stderr_and_delivers_lines(tmp_path: Path) -> None:
    stderr = tmp_path / "encode.log"
    audit = tmp_path / "commands.jsonl"
    lines: list[str] = []
    runner = CommandRunner(audit)

    results = runner.run_pipeline(
        [
            [sys.executable, "-c", "import sys; sys.stdout.write('media')"],
            [
                sys.executable,
                "-c",
                (
                    "import sys; sys.stdin.read(); "
                    "sys.stderr.write('frame=12\\nprogress=end\\n'); "
                    "sys.stderr.flush()"
                ),
            ],
        ],
        stderr_paths=[tmp_path / "source.log", stderr],
        stderr_line_callback=lines.append,
    )

    assert [item.returncode for item in results] == [0, 0]
    assert lines == ["frame=12", "progress=end"]
    assert stderr.read_text(encoding="utf-8") == "frame=12\nprogress=end\n"
    audits = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(audits) == 2


def test_pipeline_interruption_terminates_processes_and_keeps_audit(
    tmp_path: Path,
) -> None:
    stderr = tmp_path / "encode.log"
    audit = tmp_path / "commands.jsonl"
    lines: list[str] = []
    runner = CommandRunner(audit)
    started = time.monotonic()

    with pytest.raises(ProcessInterrupted) as raised:
        runner.run_pipeline(
            [
                [
                    sys.executable,
                    "-c",
                    "import sys,time; sys.stdout.write('x'); sys.stdout.flush(); time.sleep(30)",
                ],
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time; sys.stdin.read(1); "
                        "sys.stderr.write('ready=1\\n'); sys.stderr.flush(); time.sleep(30)"
                    ),
                ],
            ],
            stderr_paths=[tmp_path / "source.log", stderr],
            stderr_line_callback=lines.append,
            interrupt_requested=lambda: bool(lines),
            poll_interval=0.02,
        )

    assert time.monotonic() - started < 5
    assert len(raised.value.results) == 2
    assert all(result.returncode is not None for result in raised.value.results)
    assert "ready=1" in stderr.read_text(encoding="utf-8")
    assert len(audit.read_text(encoding="utf-8").splitlines()) == 2


def test_pipeline_callback_failure_does_not_change_success(tmp_path: Path) -> None:
    def broken_callback(_line: str) -> None:
        raise RuntimeError("observer bug")

    result = CommandRunner().run_pipeline(
        [[sys.executable, "-c", "import sys; sys.stderr.write('progress=end\\n')"]],
        stderr_paths=[tmp_path / "encode.log"],
        stderr_line_callback=broken_callback,
    )
    assert result[0].returncode == 0


def test_slow_callback_cannot_block_child_stderr_drain(tmp_path: Path) -> None:
    child_finished = tmp_path / "child-finished"
    first_callback = threading.Event()
    child_was_unblocked: list[bool] = []

    def wait_for_child(_line: str) -> None:
        if first_callback.is_set():
            return
        first_callback.set()
        deadline = time.monotonic() + 3
        while not child_finished.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_was_unblocked.append(child_finished.exists())

    script = (
        "import pathlib,sys; "
        "[sys.stderr.write('progress-line-' + ('x' * 48) + '\\n') "
        "for _ in range(20000)]; "
        "sys.stderr.flush(); "
        f"pathlib.Path({str(child_finished)!r}).write_text('done')"
    )
    CommandRunner().run_pipeline(
        [[sys.executable, "-c", script]],
        stderr_paths=[tmp_path / "encode.log"],
        stderr_line_callback=wait_for_child,
    )

    assert child_was_unblocked == [True]
    assert len((tmp_path / "encode.log").read_text().splitlines()) == 20000


def test_pipeline_timeout_terminates_children_and_audits_results(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "commands.jsonl"
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        CommandRunner(audit).run_pipeline(
            [[sys.executable, "-c", "import time; time.sleep(30)"]],
            stderr_paths=[tmp_path / "encode.log"],
            timeout=0.1,
            poll_interval=0.02,
        )

    assert time.monotonic() - started < 5
    record = json.loads(audit.read_text(encoding="utf-8"))
    assert record["returncode"] is not None


def test_stop_request_wins_race_with_already_exited_child(tmp_path: Path) -> None:
    with pytest.raises(ProcessInterrupted) as raised:
        CommandRunner(tmp_path / "commands.jsonl").run_pipeline(
            [[sys.executable, "-c", "raise SystemExit(9)"]],
            stderr_paths=[tmp_path / "encode.log"],
            interrupt_requested=lambda: True,
        )

    assert raised.value.results[0].returncode != 0
    assert (tmp_path / "commands.jsonl").is_file()


def test_retried_command_preserves_prior_stderr_and_keeps_latest_at_base(
    tmp_path: Path,
) -> None:
    stderr = tmp_path / "reference-remux.log"
    runner = CommandRunner(tmp_path / "commands.jsonl")
    for value in ("first failure", "successful retry"):
        runner.run(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stderr.write({value!r})",
            ],
            stderr_path=stderr,
        )

    assert stderr.read_text(encoding="utf-8") == "successful retry"
    assert (tmp_path / "reference-remux.attempt-01.log").read_text(
        encoding="utf-8"
    ) == "first failure"


def test_retried_pipeline_preserves_prior_stderr(tmp_path: Path) -> None:
    stderr = tmp_path / "comparison.stderr.log"
    runner = CommandRunner()
    for value in ("attempt one", "attempt two"):
        runner.run_pipeline(
            [[sys.executable, "-c", f"import sys; sys.stderr.write({value!r})"]],
            stderr_paths=[stderr],
        )
    assert stderr.read_text(encoding="utf-8") == "attempt two"
    assert (tmp_path / "comparison.stderr.attempt-01.log").read_text(
        encoding="utf-8"
    ) == "attempt one"


@pytest.mark.parametrize("path_argument", ["stdout_path", "stderr_path"])
def test_run_rejects_linked_output_before_starting_command(
    tmp_path: Path,
    path_argument: str,
) -> None:
    external = tmp_path / "external-output.txt"
    external.write_text("SENTINEL\n", encoding="utf-8")
    linked_output = tmp_path / "predictable-output.log"
    command_marker = tmp_path / "command-ran.txt"
    try:
        linked_output.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"Path({str(command_marker)!r}).write_text('ran')"
        ),
    ]
    with pytest.raises(ValueError, match=f"{path_argument[:-5]} path cannot be"):
        CommandRunner().run(command, **{path_argument: linked_output})

    assert external.read_text(encoding="utf-8") == "SENTINEL\n"
    assert not command_marker.exists()


def test_pipeline_rejects_linked_stderr_before_starting_command(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-stderr.txt"
    external.write_text("SENTINEL\n", encoding="utf-8")
    linked_stderr = tmp_path / "encode.log"
    command_marker = tmp_path / "pipeline-ran.txt"
    try:
        linked_stderr.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="stderr path cannot be"):
        CommandRunner().run_pipeline(
            [
                [
                    sys.executable,
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"Path({str(command_marker)!r}).write_text('ran')"
                    ),
                ]
            ],
            stderr_paths=[linked_stderr],
        )

    assert external.read_text(encoding="utf-8") == "SENTINEL\n"
    assert not command_marker.exists()


@pytest.mark.parametrize("use_pipeline", [False, True])
def test_runner_rejects_linked_audit_before_starting_command(
    tmp_path: Path,
    use_pipeline: bool,
) -> None:
    external = tmp_path / "external-audit.txt"
    external.write_text("SENTINEL\n", encoding="utf-8")
    linked_audit = tmp_path / "commands.jsonl"
    command_marker = tmp_path / "audited-command-ran.txt"
    try:
        linked_audit.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"Path({str(command_marker)!r}).write_text('ran')"
        ),
    ]

    runner = CommandRunner(linked_audit)
    with pytest.raises(ValueError, match="audit path cannot be"):
        if use_pipeline:
            runner.run_pipeline([command])
        else:
            runner.run(command)

    assert external.read_text(encoding="utf-8") == "SENTINEL\n"
    assert not command_marker.exists()


def test_media_diagnostics_distinguish_corruption_from_sample_seek_noise() -> None:
    source = classify_media_diagnostics(
        "\n".join(
            (
                "PES packet size mismatch",
                "Packet corrupt (stream = 20, dts = 5906869)",
                "Packet corrupt (stream = 20, dts = 5906900)",
            )
        ),
        context="source",
    )
    by_code = {item.code: item for item in source}
    assert by_code["pes_packet_size_mismatch"].requires_review
    assert by_code["corrupt_packet"].count == 2
    assert by_code["corrupt_packet"].category is DiagnosticCategory.SOURCE_CORRUPTION

    sampled = classify_media_diagnostics(
        "mmco: unref short failure\nMissing reference picture, default is 0",
        context="sampled",
    )
    assert len(sampled) == 1
    assert sampled[0].count == 2
    assert sampled[0].category is DiagnosticCategory.OPEN_GOP_SEEK
    assert sampled[0].severity is DiagnosticSeverity.WARNING
    assert not sampled[0].requires_review

    final = classify_media_diagnostics(
        "Missing reference picture, default is 0", context="final_decode"
    )
    assert final[0].category is DiagnosticCategory.DECODE_INTEGRITY
    assert final[0].severity is DiagnosticSeverity.ERROR
    assert final[0].requires_review


def test_source_diagnostics_ignore_bdj_runtime_and_corrected_output_timestamps() -> None:
    diagnostics = classify_media_diagnostics(
        "\n".join(
            (
                "bdj.c:795: BD-J check: Failed to load JVM library",
                "[matroska] Non-monotonous DTS in output stream 0:0; changing to 83",
                "[null] Application provided invalid, non monotonically increasing dts to muxer in stream 0: 2 >= 2",
            )
        ),
        context="source",
    )

    assert {item.code for item in diagnostics} == {
        "bdj_runtime_unavailable",
        "output_timestamp_corrected",
    }
    assert all(item.severity is DiagnosticSeverity.WARNING for item in diagnostics)
    assert not any(item.requires_review for item in diagnostics)
