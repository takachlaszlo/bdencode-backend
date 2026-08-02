"""Safe, auditable subprocess execution."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


SECRET_MARKERS = ("api_key", "apikey", "authorization", "token", "password", "secret")


class ProcessFailure(RuntimeError):
    def __init__(self, result: "ProcessResult") -> None:
        safe_command = shlex.join(redact_argv(result.argv))
        super().__init__(
            f"command failed with exit code {result.returncode}: {safe_command}"
        )
        self.result = result


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
        return shlex.join(self.argv)


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
        elif re.search(r"(?i)([?&](?:key|token|api[_-]?key)=)[^&\s]+", item):
            redacted.append(
                re.sub(
                    r"(?i)([?&](?:key|token|api[_-]?key)=)[^&\s]+",
                    r"\1<redacted>",
                    item,
                )
            )
        else:
            redacted.append(item)
    return redacted


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
    ) -> ProcessResult:
        command = tuple(os.fspath(item) for item in argv)
        if not command or not command[0]:
            raise ValueError("argv must not be empty")
        if any("\x00" in item for item in command):
            raise ValueError("argv contains a NUL byte")
        if cwd is not None:
            cwd = cwd.resolve(strict=True)

        if stdout_path:
            stdout_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if stderr_path:
            stderr_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)

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

    def run_pipeline(
        self,
        commands: Sequence[Sequence[str | os.PathLike[str]]],
        *,
        cwd: Path | None = None,
        stderr_paths: Sequence[Path] | None = None,
        timeout: float | None = None,
        check: bool = True,
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
        if cwd is not None:
            cwd = cwd.resolve(strict=True)

        errors = list(stderr_paths or [None] * len(normalized))
        error_handles: list[object] = []
        processes: list[subprocess.Popen[bytes]] = []
        started: list[float] = []
        previous_stdout = None
        try:
            for index, command in enumerate(normalized):
                error_path = errors[index]
                if error_path:
                    error_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
                error_handle = (
                    error_path.open("wb") if error_path else subprocess.DEVNULL
                )
                error_handles.append(error_handle)
                started.append(time.time())
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=previous_stdout
                    if previous_stdout is not None
                    else subprocess.DEVNULL,
                    stdout=subprocess.PIPE
                    if index < len(normalized) - 1
                    else subprocess.DEVNULL,
                    stderr=error_handle,
                    shell=False,
                    start_new_session=False,
                )
                processes.append(process)
                if previous_stdout is not None:
                    previous_stdout.close()
                previous_stdout = process.stdout

            processes[-1].wait(timeout=timeout)
            for process in reversed(processes[:-1]):
                process.wait(timeout=30)
        except BaseException:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise
        finally:
            if previous_stdout is not None:
                previous_stdout.close()
            for handle in error_handles:
                if hasattr(handle, "close"):
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
        failures = [result for result in results if result.returncode != 0]
        if check and failures:
            raise ProcessFailure(failures[-1])
        return results
