from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bdencode.process import CommandRunner, ProcessInterrupted


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
