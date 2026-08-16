"""Safe, auditable subprocess execution."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence


SECRET_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "token",
    "password",
    "secret",
    "userhash",
    "credential",
)
LOG = logging.getLogger(__name__)


class ProcessFailure(RuntimeError):
    def __init__(self, result: "ProcessResult") -> None:
        safe_command = shlex.join(redact_argv(result.argv))
        super().__init__(
            f"command failed with exit code {result.returncode}: {safe_command}"
        )
        self.result = result


class ProcessInterrupted(RuntimeError):
    """A pipeline was stopped by an explicit cancellation/shutdown request."""

    def __init__(
        self,
        message: str = "pipeline interrupted",
        *,
        results: Sequence["ProcessResult"] = (),
    ) -> None:
        super().__init__(message)
        self.results = tuple(results)


class DiagnosticCategory(StrEnum):
    SOURCE_CORRUPTION = "source_corruption"
    DECODE_INTEGRITY = "decode_integrity"
    OPEN_GOP_SEEK = "open_gop_seek"
    TIMESTAMP = "timestamp"
    ENVIRONMENT = "environment"
    UNKNOWN = "unknown"


class DiagnosticSeverity(StrEnum):
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class MediaDiagnostic:
    code: str
    category: DiagnosticCategory
    severity: DiagnosticSeverity
    count: int
    examples: tuple[str, ...]
    requires_review: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category.value,
            "severity": self.severity.value,
            "count": self.count,
            "examples": list(self.examples),
            "requires_review": self.requires_review,
        }


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    started_at: float
    ended_at: float
    stdout_path: Path | None
    stderr_path: Path | None

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at

    @property
    def display_command(self) -> str:
        return shlex.join(redact_argv(self.argv))


def redact_argv(argv: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for item in argv:
        lowered = item.lower()
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        if any(marker in lowered for marker in SECRET_MARKERS):
            separators = [
                position for token in ("=", ":") if (position := item.find(token)) >= 0
            ]
            if separators:
                position = min(separators)
                redacted.append(item[: position + 1] + " <redacted>")
            else:
                redacted.append(item)
                hide_next = True
        elif re.search(r"(?i)([?&](?:key|token|userhash|api[_-]?key)=)[^&\s]+", item):
            redacted.append(
                re.sub(
                    r"(?i)([?&](?:key|token|userhash|api[_-]?key)=)[^&\s]+",
                    r"\1<redacted>",
                    item,
                )
            )
        else:
            redacted.append(item)
    return redacted


def next_stderr_attempt_path(path: Path) -> Path:
    """Return the first unused, human-sortable archive name for ``path``."""

    for attempt in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}.attempt-{attempt:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many preserved stderr attempts for {path.name}")


def _reject_link_output_path(path: Path, *, description: str) -> None:
    """Reject predictable output files redirected through links/reparse points."""

    if not os.path.lexists(path):
        return
    is_junction = getattr(path, "is_junction", None)
    if path.is_symlink() or (callable(is_junction) and is_junction()):
        raise ValueError(
            f"{description} cannot be a symbolic link or junction"
        )


def preserve_previous_stderr(path: Path) -> Path | None:
    """Archive an existing stderr log while keeping the base name for latest."""

    _reject_link_output_path(path, description="stderr path")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("stderr path exists but is not a regular file")
    archived = next_stderr_attempt_path(path)
    path.replace(archived)
    return archived


_DIAGNOSTIC_RULES: tuple[
    tuple[str, DiagnosticCategory, DiagnosticSeverity, re.Pattern[str]], ...
] = (
    (
        "pes_packet_size_mismatch",
        DiagnosticCategory.SOURCE_CORRUPTION,
        DiagnosticSeverity.ERROR,
        re.compile(r"(?i)PES packet size mismatch"),
    ),
    (
        "corrupt_packet",
        DiagnosticCategory.SOURCE_CORRUPTION,
        DiagnosticSeverity.ERROR,
        re.compile(r"(?i)(?:packet corrupt|corrupt(?:ed)? input packet|CRC error)"),
    ),
    (
        "truncated_input",
        DiagnosticCategory.SOURCE_CORRUPTION,
        DiagnosticSeverity.ERROR,
        re.compile(r"(?i)(?:truncated (?:file|packet|nal)|unexpected end of file)"),
    ),
    (
        "missing_reference_picture",
        DiagnosticCategory.OPEN_GOP_SEEK,
        DiagnosticSeverity.WARNING,
        re.compile(
            r"(?i)(?:missing reference picture|mmco:\s*unref short failure|"
            r"reference picture missing during reorder)"
        ),
    ),
    (
        "decode_error",
        DiagnosticCategory.DECODE_INTEGRITY,
        DiagnosticSeverity.ERROR,
        re.compile(
            r"(?i)(?:error while decoding|invalid NAL|decode_slice_header error|"
            r"concealing \d+ DC, \d+ AC, \d+ MV errors)"
        ),
    ),
    (
        "timestamp_discontinuity",
        DiagnosticCategory.TIMESTAMP,
        DiagnosticSeverity.WARNING,
        re.compile(
            r"(?i)(?:non-monoton(?:ous|ically increasing) DTS|timestamp discontinuity|"
            r"invalid, non monotonically increasing dts)"
        ),
    ),
    (
        "storage_or_io_failure",
        DiagnosticCategory.ENVIRONMENT,
        DiagnosticSeverity.FATAL,
        re.compile(
            r"(?i)(?:No space left on device|Input/output error|Permission denied|"
            r"Read-only file system)"
        ),
    ),
    (
        "unclassified_error",
        DiagnosticCategory.UNKNOWN,
        DiagnosticSeverity.ERROR,
        re.compile(r"(?i)\b(?:error|failed|invalid data found)\b"),
    ),
)


def classify_media_diagnostics(
    text: str,
    *,
    context: Literal["source", "sampled", "final_decode", "generic"] = "generic",
    max_examples: int = 3,
) -> tuple[MediaDiagnostic, ...]:
    """Classify FFmpeg/demux diagnostics without equating seek noise to corruption."""

    if max_examples < 1:
        raise ValueError("max diagnostic examples must be positive")
    grouped: dict[
        str,
        tuple[DiagnosticCategory, DiagnosticSeverity, list[str], int],
    ] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for code, category, severity, pattern in _DIAGNOSTIC_RULES:
            if not pattern.search(line):
                continue
            effective_category = category
            effective_severity = severity
            if category is DiagnosticCategory.OPEN_GOP_SEEK and context != "sampled":
                effective_category = DiagnosticCategory.DECODE_INTEGRITY
                effective_severity = DiagnosticSeverity.ERROR
            prior = grouped.get(code)
            if prior is None:
                examples = [line]
                grouped[code] = (
                    effective_category,
                    effective_severity,
                    examples,
                    1,
                )
            else:
                prior_category, prior_severity, examples, count = prior
                if len(examples) < max_examples and line not in examples:
                    examples.append(line)
                grouped[code] = (
                    prior_category,
                    prior_severity,
                    examples,
                    count + 1,
                )
            break

    results = []
    for code, (category, severity, examples, count) in grouped.items():
        requires_review = not (
            category is DiagnosticCategory.OPEN_GOP_SEEK and context == "sampled"
        )
        results.append(
            MediaDiagnostic(
                code=code,
                category=category,
                severity=severity,
                count=count,
                examples=tuple(examples),
                requires_review=requires_review,
            )
        )
    return tuple(
        sorted(
            results,
            key=lambda item: (
                -{
                    DiagnosticSeverity.WARNING: 1,
                    DiagnosticSeverity.ERROR: 2,
                    DiagnosticSeverity.FATAL: 3,
                }[item.severity],
                item.code,
            ),
        )
    )


class CommandRunner:
    def __init__(self, audit_path: Path | None = None) -> None:
        self.audit_path = audit_path

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdout_path: Path | None = None,
        stderr_path: Path | None = None,
        check: bool = True,
        ok_returncodes: Sequence[int] = (0,),
        timeout: float | None = None,
        preserve_stderr_attempts: bool = True,
    ) -> ProcessResult:
        command = tuple(os.fspath(item) for item in argv)
        if not command or not command[0]:
            raise ValueError("argv must not be empty")
        if any("\x00" in item for item in command):
            raise ValueError("argv contains a NUL byte")
        if cwd is not None:
            cwd = cwd.resolve(strict=True)

        self._validate_audit_path()
        if stdout_path:
            stdout_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            _reject_link_output_path(stdout_path, description="stdout path")
        if stderr_path:
            stderr_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            _reject_link_output_path(stderr_path, description="stderr path")
            if preserve_stderr_attempts:
                preserve_previous_stderr(stderr_path)

        out_handle = stdout_path.open("wb") if stdout_path else subprocess.DEVNULL
        err_handle = stderr_path.open("wb") if stderr_path else subprocess.DEVNULL
        started = time.time()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=None if env is None else dict(env),
                stdin=subprocess.DEVNULL,
                stdout=out_handle,
                stderr=err_handle,
                timeout=timeout,
                check=False,
                shell=False,
                start_new_session=False,
            )
        finally:
            if stdout_path:
                out_handle.close()
            if stderr_path:
                err_handle.close()
        result = ProcessResult(
            command,
            completed.returncode,
            started,
            time.time(),
            stdout_path,
            stderr_path,
        )
        self._audit(result)
        if check and result.returncode not in ok_returncodes:
            raise ProcessFailure(result)
        return result

    def capture(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float = 30,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(os.fspath(item) for item in argv)
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=check,
            shell=False,
        )

    def _audit(self, result: ProcessResult) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        _reject_link_output_path(self.audit_path, description="audit path")
        record = {
            "argv": redact_argv(result.argv),
            "returncode": result.returncode,
            "started_at_epoch": result.started_at,
            "ended_at_epoch": result.ended_at,
            "duration_seconds": result.duration_seconds,
            "stdout": str(result.stdout_path) if result.stdout_path else None,
            "stderr": str(result.stderr_path) if result.stderr_path else None,
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _validate_audit_path(self) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        _reject_link_output_path(self.audit_path, description="audit path")

    def run_pipeline(
        self,
        commands: Sequence[Sequence[str | os.PathLike[str]]],
        *,
        cwd: Path | None = None,
        stderr_paths: Sequence[Path] | None = None,
        timeout: float | None = None,
        check: bool = True,
        stderr_line_callback: Callable[[str], None] | None = None,
        interrupt_requested: Callable[[], bool] | None = None,
        poll_interval: float = 0.2,
        preserve_stderr_attempts: bool = True,
    ) -> list[ProcessResult]:
        """Connect commands with OS pipes without invoking a shell.

        Every process remains in the worker's systemd cgroup, so the configured
        whole-tree CPU quota applies to VapourSynth, FFmpeg, x264/x265 and their
        decoder threads together.
        """
        normalized = [
            tuple(os.fspath(item) for item in command) for command in commands
        ]
        if not normalized or any(
            not command or not command[0] for command in normalized
        ):
            raise ValueError("pipeline commands must not be empty")
        if stderr_paths is not None and len(stderr_paths) != len(normalized):
            raise ValueError("stderr_paths must match the number of commands")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if cwd is not None:
            cwd = cwd.resolve(strict=True)

        self._validate_audit_path()
        errors = list(stderr_paths or [None] * len(normalized))
        for error_path in errors:
            if error_path is None:
                continue
            error_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            _reject_link_output_path(error_path, description="stderr path")
        if preserve_stderr_attempts:
            archived_paths: set[Path] = set()
            for error_path in errors:
                if error_path is None:
                    continue
                normalized_path = error_path.resolve(strict=False)
                if normalized_path in archived_paths:
                    continue
                error_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
                preserve_previous_stderr(error_path)
                archived_paths.add(normalized_path)
        error_handles: list[object] = []
        processes: list[subprocess.Popen[bytes]] = []
        started: list[float] = []
        previous_stdout = None
        stderr_thread: threading.Thread | None = None
        callback_thread: threading.Thread | None = None
        callback_queue: queue.SimpleQueue[str | object] | None = None
        callback_stop = object()
        interrupted = False
        timed_out = False
        deadline = None if timeout is None else time.monotonic() + timeout

        def observe_stderr(
            pending: queue.SimpleQueue[str | object],
            callback: Callable[[str], None],
        ) -> None:
            while True:
                item = pending.get()
                if item is callback_stop:
                    return
                try:
                    callback(str(item))
                except Exception:
                    # Progress is observational. A broken parser, disk-full
                    # JSONL, or transient DB conflict cannot poison media.
                    LOG.exception("stderr line callback failed; process continues")

        def drain_last_stderr(stream: object, output_handle: object) -> None:
            try:
                while True:
                    raw = stream.readline()  # type: ignore[attr-defined]
                    if not raw:
                        break
                    if hasattr(output_handle, "write"):
                        output_handle.write(raw)  # type: ignore[attr-defined]
                        output_handle.flush()  # type: ignore[attr-defined]
                    if callback_queue is not None:
                        callback_queue.put(
                            raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        )
            finally:
                if hasattr(stream, "close"):
                    stream.close()  # type: ignore[attr-defined]
                if callback_queue is not None:
                    callback_queue.put(callback_stop)

        def terminate_processes() -> None:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        def requested_interruption() -> bool:
            if interrupt_requested is None:
                return False
            try:
                return bool(interrupt_requested())
            except Exception:
                LOG.exception("interrupt polling failed; process continues")
                return False

        try:
            for index, command in enumerate(normalized):
                error_path = errors[index]
                if error_path:
                    error_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
                error_handle = error_path.open("wb") if error_path else None
                error_handles.append(error_handle)
                started.append(time.time())
                is_last = index == len(normalized) - 1
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=previous_stdout
                    if previous_stdout is not None
                    else subprocess.DEVNULL,
                    stdout=subprocess.PIPE
                    if index < len(normalized) - 1
                    else subprocess.DEVNULL,
                    # The last process carries FFmpeg's machine progress
                    # protocol. Drain it continuously, teeing every byte to the
                    # original log before presenting decoded lines to observers.
                    stderr=subprocess.PIPE
                    if is_last
                    else (error_handle or subprocess.DEVNULL),
                    shell=False,
                    start_new_session=False,
                )
                processes.append(process)
                if previous_stdout is not None:
                    previous_stdout.close()
                previous_stdout = process.stdout

            last_stderr = processes[-1].stderr
            if last_stderr is None:
                raise RuntimeError("last pipeline process has no stderr pipe")
            if stderr_line_callback is not None:
                callback_queue = queue.SimpleQueue()
                callback_thread = threading.Thread(
                    target=observe_stderr,
                    args=(callback_queue, stderr_line_callback),
                    name="bdencode-stderr-observer",
                    daemon=True,
                )
                callback_thread.start()
            stderr_thread = threading.Thread(
                target=drain_last_stderr,
                args=(last_stderr, error_handles[-1]),
                name="bdencode-stderr-tee",
                daemon=True,
            )
            stderr_thread.start()

            while processes[-1].poll() is None:
                interrupted = requested_interruption()
                if interrupted:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(poll_interval)

            # systemd's KillMode=control-group may terminate FFmpeg at the same
            # instant it signals the worker. Check once after process exit so a
            # service stop cannot be misclassified as an encode failure.
            if not interrupted:
                interrupted = requested_interruption()

            if interrupted or timed_out:
                terminate_processes()
            else:
                processes[-1].wait()
            for process in reversed(processes[:-1]):
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=10)
        except BaseException:
            terminate_processes()
            raise
        finally:
            if previous_stdout is not None:
                previous_stdout.close()
            if stderr_thread is not None:
                stderr_thread.join()
            if callback_thread is not None:
                callback_thread.join()
            for handle in error_handles:
                if handle is not None and hasattr(handle, "close"):
                    handle.close()

        ended = time.time()
        results = [
            ProcessResult(
                argv=command,
                returncode=process.returncode,
                started_at=start,
                ended_at=ended,
                stdout_path=None,
                stderr_path=errors[index],
            )
            for index, (command, process, start) in enumerate(
                zip(normalized, processes, started, strict=True)
            )
        ]
        for result in results:
            self._audit(result)
        if interrupted:
            raise ProcessInterrupted(results=results)
        if timed_out:
            raise subprocess.TimeoutExpired(normalized[-1], timeout)
        failures = [result for result in results if result.returncode != 0]
        if check and failures:
            raise ProcessFailure(failures[-1])
        return results
