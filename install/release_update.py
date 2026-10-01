#!/usr/bin/env python3
"""Daily BDEncode release check with an unattended, transactional update.

The daily systemd timer asks the configured repository for its newest stable
release tag (``vMAJOR.MINOR.PATCH``). Nothing is installed unless that tag is
newer than the installed backend. A newer release is installed by running that
release's own installer (``install/install.sh``, or ``install/wsl-install.sh`` on
a Windows-managed WSL distribution) as the unprivileged BDEncode account, so the
installer's durable snapshot, health check and rollback apply unchanged. This
helper only decides whether and when to start it:

* the installer is never started while a job owns the pipeline;
* the account needs passwordless ``sudo`` (the Windows installer provisions it);
  otherwise the new release is only reported and the installation stays manual;
* a release whose installation failed twice is not retried until a newer tag
  appears, so a broken release cannot burn CPU every night.

Every outcome is written to ``<state-root>/status.json`` and ``release-update.log``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:  # pragma: no cover - production is Linux; the decision logic is tested everywhere.
    import fcntl
except ModuleNotFoundError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
try:  # pragma: no cover
    import pwd
except ModuleNotFoundError:  # pragma: no cover
    pwd = None  # type: ignore[assignment]


DEFAULT_STATE_ROOT = Path("/var/lib/bdencode/release-update")
DEFAULT_RELEASE_CONFIG = Path("/etc/bdencode/release-update.toml")
DEFAULT_SERVICE_CONFIG = Path("/etc/bdencode/config.toml")
WINDOWS_MARKER = Path("/etc/bdencode/windows-managed")
WSL_NGINX_CONFIG = Path("/etc/nginx/conf.d/bdencode-wsl.conf")
DEFAULT_REPOSITORY = "https://github.com/takachlaszlo/bdencode-backend.git"
DEFAULT_WINDOWS_PORT = 8787
RESERVED_API_PORT = 8796

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HTTPS_REPOSITORY_RE = re.compile(
    r"^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?/[A-Za-z0-9._~/-]+$"
)
FILE_REPOSITORY_RE = re.compile(r"^file:///[A-Za-z0-9._~/:%+-]+$")
SCRATCH_NAME_RE = re.compile(r"^v\d+\.\d+\.\d+-[0-9]{8}T[0-9]{6}Z-[0-9]+$")

MAX_FAILED_ATTEMPTS = 2
STATUS_SCHEMA = 1
COMMAND_TIMEOUT_SECONDS = 120
CLONE_TIMEOUT_SECONDS = 900
INSTALL_TIMEOUT_SECONDS = 2 * 3600 + 1800
INSTALL_TERMINATE_GRACE_SECONDS = 300
SHORT_TERMINATE_GRACE_SECONDS = 10
TAIL_LINES = 25
LOG_LIMIT_BYTES = 5 * 1024 * 1024

STATE_UP_TO_DATE = "up_to_date"
STATE_AVAILABLE = "update_available"
STATE_MANUAL = "manual_update_required"
STATE_DEFERRED = "deferred"
STATE_INSTALLED = "installed"
STATE_CHECK_FAILED = "check_failed"
STATE_INSTALL_FAILED = "install_failed"
STATE_BLOCKED = "blocked"
STATE_INVALID = "invalid_release"
# States that end the service successfully; everything else needs attention.
HEALTHY_STATES = frozenset(
    {STATE_UP_TO_DATE, STATE_AVAILABLE, STATE_MANUAL, STATE_DEFERRED, STATE_INSTALLED}
)

Version = tuple[int, int, int]


class ReleaseUpdateError(RuntimeError):
    """A fail-closed problem with the release check or the unattended update."""


class InvalidReleaseError(ReleaseUpdateError):
    """The published release is inconsistent; retrying cannot fix it."""


class CommandTimeoutError(ReleaseUpdateError):
    """A child process exceeded its time limit and was terminated."""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def parse_version(text: str) -> Version:
    match = VERSION_RE.match(text.strip())
    if match is None:
        raise ReleaseUpdateError(f"unrecognised version string: {text.strip()!r}")
    return int(match[1]), int(match[2]), int(match[3])


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def select_latest_release(ls_remote_output: str) -> tuple[str, Version] | None:
    """Return the highest stable ``vX.Y.Z`` tag in ``git ls-remote --tags --refs`` output."""

    best: tuple[str, Version] | None = None
    for line in ls_remote_output.splitlines():
        _, _, reference = line.partition("\t")
        reference = reference.strip()
        if not reference.startswith("refs/tags/"):
            continue
        name = reference.removeprefix("refs/tags/")
        match = TAG_RE.match(name)
        if match is None:
            continue
        version = (int(match[1]), int(match[2]), int(match[3]))
        if best is None or version > best[1]:
            best = (name, version)
    return best


def validate_repository(value: object) -> str:
    if not isinstance(value, str) or not (
        HTTPS_REPOSITORY_RE.fullmatch(value) or FILE_REPOSITORY_RE.fullmatch(value)
    ):
        raise ReleaseUpdateError(
            "repository must be an https:// URL without credentials, query or fragment "
            "(or a file:/// URL for a local mirror)"
        )
    return value


@dataclass(frozen=True)
class ReleaseConfig:
    repository: str = DEFAULT_REPOSITORY
    automatic_install: bool = True


def load_release_config(path: Path, *, require_root_owner: bool = False) -> ReleaseConfig:
    """Read the operator's ``release-update.toml``; a typo must never be ignored."""

    if not path.exists():
        return ReleaseConfig()
    if require_root_owner:
        details = path.stat()
        if details.st_uid != 0 or details.st_mode & 0o022:
            raise ReleaseUpdateError(
                f"{path} must be owned by root and not writable by group or others"
            )
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseUpdateError(f"unreadable release update configuration {path}: {error}") from error
    unknown = sorted(set(raw) - {"repository", "automatic_install"})
    if unknown:
        raise ReleaseUpdateError(f"unknown key(s) in {path}: {', '.join(unknown)}")
    automatic = raw.get("automatic_install", True)
    if not isinstance(automatic, bool):
        raise ReleaseUpdateError("automatic_install must be true or false")
    return ReleaseConfig(
        repository=validate_repository(raw.get("repository", DEFAULT_REPOSITORY)),
        automatic_install=automatic,
    )


@dataclass(frozen=True)
class Deployment:
    """Facts about the installed system that the installer must be given again."""

    task_user: str
    task_home: Path
    data_root: Path
    source_root: Path
    cpu_percent: int
    windows_managed: bool
    windows_port: int
    service_config: Path = DEFAULT_SERVICE_CONFIG

    @property
    def app_root(self) -> Path:
        return self.data_root / "app"

    @property
    def scratch_root(self) -> Path:
        # Outside the data root: the sandboxed worker may write there, but not below the
        # account's home, so it cannot alter a download between verification and install.
        return self.task_home / ".cache" / "bdencode-release-update"


def windows_port_from_nginx(path: Path) -> int:
    if not path.is_file():
        return DEFAULT_WINDOWS_PORT
    match = re.search(
        r"^\s*listen\s+127\.0\.0\.1:(\d+)\b", path.read_text(encoding="utf-8"), re.MULTILINE
    )
    if match is None:
        return DEFAULT_WINDOWS_PORT
    port = int(match[1])
    if not 1024 <= port <= 65535 or port == RESERVED_API_PORT:
        raise ReleaseUpdateError(f"unusable WSL web port in {path}: {port}")
    return port


def load_deployment(
    *,
    task_user: str,
    task_home: Path,
    data_root: Path,
    service_config: Path = DEFAULT_SERVICE_CONFIG,
    windows_marker: Path = WINDOWS_MARKER,
    wsl_nginx_config: Path = WSL_NGINX_CONFIG,
) -> Deployment:
    try:
        with service_config.open("rb") as stream:
            section = tomllib.load(stream)["bdencode"]
        configured_root = Path(section["data_root"])
        source_root = Path(section["source_roots"][0])
        cpu_percent = int(section["cpu_limit_percent"])
    except (OSError, KeyError, IndexError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise ReleaseUpdateError(
            f"cannot read the installed configuration {service_config}: {error}"
        ) from error
    if configured_root != data_root:
        # The installer refuses a rendered data root that differs from the config.
        raise ReleaseUpdateError(
            f"data root {data_root} does not match {service_config} ({configured_root})"
        )
    if not 1 <= cpu_percent <= 100:
        raise ReleaseUpdateError(f"cpu_limit_percent out of range in {service_config}")
    windows_managed = windows_marker.exists()
    return Deployment(
        task_user=task_user,
        task_home=task_home,
        data_root=data_root,
        source_root=source_root,
        cpu_percent=cpu_percent,
        windows_managed=windows_managed,
        windows_port=(
            windows_port_from_nginx(wsl_nginx_config) if windows_managed else DEFAULT_WINDOWS_PORT
        ),
        service_config=service_config,
    )


def atomic_write(path: Path, payload: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def rotate_log(path: Path, limit: int = LOG_LIMIT_BYTES) -> None:
    """Keep one previous log; an installation appends thousands of lines."""

    try:
        if path.stat().st_size > limit:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass


class Reporter:
    """Timestamped lines on stdout (the journal) and in the append-only log."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.path = log_path

    def __call__(self, message: str) -> None:
        line = f"{utc_now()} {message}"
        print(line, flush=True)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o644)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def echo_tail(self) -> None:
        """Copy the end of the log (the installer's last words) to the journal."""

        if self.path is None:
            return
        try:
            with self.path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 16384))
                tail = stream.read().decode("utf-8", "replace").splitlines()[-TAIL_LINES:]
        except OSError:
            return
        for line in tail:
            print(f"  | {line}", flush=True)


class StatusStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(document, dict) and document.get("schema") == STATUS_SCHEMA:
            return document
        return {}

    def save(self, document: dict[str, Any]) -> None:
        atomic_write(
            self.path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        )


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""


class SystemRunner:
    """Runs commands, optionally as the BDEncode account with a clean environment."""

    def run(
        self,
        argv: Sequence[object],
        *,
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        grace: float = SHORT_TERMINATE_GRACE_SECONDS,
        cwd: Path | None = None,
        log_path: Path | None = None,
    ) -> CommandResult:
        command = [str(item) for item in argv]
        if env is not None:
            command = ["env", "-i", *(f"{key}={value}" for key, value in sorted(env.items())), *command]
        if user is not None:
            command = ["runuser", "-u", user, "--", *command]
        log_handle = open(log_path, "ab") if log_path is not None else None  # noqa: SIM115
        try:
            options: dict[str, Any] = {"stdout": log_handle or subprocess.PIPE}
            if log_handle is None:
                options.update(text=True, errors="replace")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                start_new_session=True,
                **options,
            )
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                self._terminate(process, grace)
                raise CommandTimeoutError(f"{command[0]} timed out after {timeout:.0f} s") from error
            return CommandResult(process.returncode, output or "")
        finally:
            if log_handle is not None:
                log_handle.close()

    @staticmethod
    def _terminate(process: subprocess.Popen[Any], grace: float) -> None:
        # The installer's EXIT trap rolls back on SIGTERM; SIGKILL is the last resort
        # (the install watchdog unit then restores the snapshot).
        for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 30)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            try:
                process.communicate(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue


class ReleaseUpdater:
    def __init__(
        self,
        deployment: Deployment,
        config: ReleaseConfig,
        runner: SystemRunner,
        store: StatusStore,
        report: Reporter,
        *,
        check_only: bool = False,
        clock: Callable[[], str] = utc_now,
        release_id: str | None = None,
    ) -> None:
        self.deployment = deployment
        self.config = config
        self.runner = runner
        self.store = store
        self.report = report
        self.check_only = check_only
        self.clock = clock
        self.release_id = release_id or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{os.getpid()}"
        self.document = store.load()

    # -- environments of the commands run as the BDEncode account ---------------------
    def _base_env(self) -> dict[str, str]:
        return {
            "HOME": str(self.deployment.task_home),
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
        }

    def _git_env(self) -> dict[str, str]:
        protocol = "file" if self.config.repository.startswith("file:") else "https"
        return {
            **self._base_env(),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ALLOW_PROTOCOL": protocol,
            # The account's own gitconfig must not be able to rewrite the repository URL.
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }

    def _app_env(self) -> dict[str, str]:
        app_root = self.deployment.app_root
        return {
            "HOME": str(self.deployment.task_home),
            "BDENCODE_CONFIG": str(self.deployment.service_config),
            "PATH": f"{app_root}/tools/current/bin:{app_root}/current/venv/bin:/usr/local/bin:/usr/bin:/bin",
            "XDG_CACHE_HOME": str(self.deployment.data_root / "cache"),
            "XDG_CONFIG_HOME": str(app_root / "tools" / "current" / "config"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _as_account(self, argv: Sequence[object], env: Mapping[str, str], **options: Any) -> CommandResult:
        return self.runner.run(
            argv, user=self.deployment.task_user, env=env, timeout=COMMAND_TIMEOUT_SECONDS, **options
        )

    # -- facts ---------------------------------------------------------------------------
    def installed_version(self) -> Version:
        python = self.deployment.app_root / "current" / "venv" / "bin" / "python"
        result = self._as_account(
            [python, "-c", "import bdencode; print(bdencode.__version__)"], self._app_env()
        )
        if result.returncode != 0:
            raise ReleaseUpdateError("cannot determine the installed BDEncode version")
        return parse_version(last_line(result.stdout))

    def latest_release(self) -> tuple[str, Version]:
        result = self._as_account(
            ["git", "ls-remote", "--tags", "--refs", self.config.repository], self._git_env()
        )
        if result.returncode != 0:
            raise ReleaseUpdateError(f"cannot list the releases of {self.config.repository}")
        latest = select_latest_release(result.stdout)
        if latest is None:
            raise ReleaseUpdateError(f"{self.config.repository} has no vX.Y.Z release tag")
        return latest

    def passwordless_sudo(self) -> bool:
        return self._as_account(["sudo", "-n", "true"], self._base_env()).returncode == 0

    def queue_is_idle(self) -> bool:
        """Mirror the installer's own gate; the installer re-checks under its lock."""

        bdencode = self.deployment.app_root / "current" / "venv" / "bin" / "bdencode"
        arguments = ["queue-idle"]
        help_result = self._as_account([bdencode, "queue-idle", "--help"], self._app_env())
        if "--allow-install-safe-pause" in help_result.stdout:
            arguments.append("--allow-install-safe-pause")
        result = self._as_account([bdencode, *arguments], self._app_env())
        if result.returncode == 0:
            return True
        if result.returncode == 3:
            return False
        raise ReleaseUpdateError(f"queue-idle failed operationally (exit {result.returncode})")

    # -- the release checkout --------------------------------------------------------------
    def download_release(self, tag: str) -> Path:
        scratch = self.deployment.scratch_root
        self._as_account(["mkdir", "-p", "-m", "0750", scratch], self._base_env())
        checkout = scratch / f"{tag}-{self.release_id}"
        try:
            result = self.runner.run(
                [
                    "git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1",
                    "--branch", tag, self.config.repository, checkout,
                ],
                user=self.deployment.task_user,
                env=self._git_env(),
                timeout=CLONE_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise ReleaseUpdateError(f"cannot download {tag} from {self.config.repository}")
        except BaseException:
            self.remove_checkout(checkout)
            raise
        return checkout

    def verify_checkout(self, checkout: Path, version: Version) -> str:
        """Check that the tag is a coherent release and return its commit."""

        required = ["pyproject.toml", "install/install.sh", "frontend/dist/index.html"]
        if self.deployment.windows_managed:
            required.append("install/wsl-install.sh")
        for relative in required:
            # The clone is the account's data: this process (root) reads it, so a link
            # that points at some other file is as bad as a missing file.
            path = checkout / relative
            if path.is_symlink() or not path.is_file():
                raise InvalidReleaseError(f"release is missing {relative}")
        try:
            with (checkout / "pyproject.toml").open("rb") as stream:
                declared = tomllib.load(stream)["project"]["version"]
        except (OSError, KeyError, tomllib.TOMLDecodeError) as error:
            raise InvalidReleaseError(f"release has no readable pyproject.toml version: {error}") from error
        if declared != format_version(version):
            raise InvalidReleaseError(
                f"tag v{format_version(version)} declares version {declared!r} in pyproject.toml"
            )
        commit = last_line(
            self._as_account(["git", "-C", checkout, "rev-parse", "HEAD"], self._git_env()).stdout
        )
        return commit if COMMIT_RE.match(commit) else "unknown"

    def remove_checkout(self, checkout: Path) -> None:
        scratch = self.deployment.scratch_root
        if checkout.parent != scratch or not SCRATCH_NAME_RE.match(checkout.name):
            self.report(f"refusing to remove unexpected path {checkout}")
            return
        self._as_account(["rm", "-rf", "--one-file-system", "--", checkout], self._base_env())

    def clean_stale_checkouts(self) -> None:
        scratch = self.deployment.scratch_root
        try:
            names = [entry.name for entry in os.scandir(scratch) if SCRATCH_NAME_RE.match(entry.name)]
        except OSError:
            return
        for name in sorted(names):
            self.report(f"removing stale download {name}")
            self.remove_checkout(scratch / name)

    # -- the installer ---------------------------------------------------------------------------
    def run_installer(self, checkout: Path) -> int:
        deployment = self.deployment
        script = "wsl-install.sh" if deployment.windows_managed else "install.sh"
        env = {
            **self._base_env(),
            "BDENCODE_DATA_ROOT": str(deployment.data_root),
            "BDENCODE_SOURCE_ROOT": str(deployment.source_root),
            "BDENCODE_CPU_PERCENT": str(deployment.cpu_percent),
            # install.sh must not stop bdencode-update.service: that is this process.
            "BDENCODE_UNATTENDED_UPDATE": "1",
        }
        if deployment.windows_managed:
            env["BDENCODE_WINDOWS_PORT"] = str(deployment.windows_port)
        self.report(f"running install/{script} as {deployment.task_user}")
        try:
            result = self.runner.run(
                ["bash", checkout / "install" / script],
                user=deployment.task_user,
                env=env,
                timeout=INSTALL_TIMEOUT_SECONDS,
                grace=INSTALL_TERMINATE_GRACE_SECONDS,
                cwd=checkout,
                log_path=self.report.path,
            )
        except CommandTimeoutError as error:
            self.report(str(error))
            return 124
        return result.returncode

    # -- status -------------------------------------------------------------------------------------
    def record(self, state: str, message: str, **fields: Any) -> None:
        now = self.clock()
        self.document.update({"schema": STATUS_SCHEMA, "state": state, "message": message, "checked_at": now})
        if state in HEALTHY_STATES:
            self.document["last_successful_check_at"] = now
        self.document.update({key: value for key, value in fields.items() if value is not None})
        self.store.save(self.document)
        self.report(f"{state}: {message}")

    def failed_attempts(self, tag: str) -> int:
        attempts = self.document.get("failed_attempts")
        value = attempts.get(tag, 0) if isinstance(attempts, dict) else 0
        return value if isinstance(value, int) else 0

    def set_failed_attempts(self, tag: str, count: int) -> None:
        # Only the tag in question is remembered; older entries cannot matter any more.
        self.document["failed_attempts"] = {tag: count} if count else {}

    # -- the whole run ----------------------------------------------------------------------------------
    def run(self) -> int:
        self.report("release check started")
        try:
            return self._run()
        except InvalidReleaseError as error:
            tag = self.document.get("latest_tag")
            if isinstance(tag, str):
                self.set_failed_attempts(tag, MAX_FAILED_ATTEMPTS)
            self.record(STATE_INVALID, str(error))
            return 1
        except ReleaseUpdateError as error:
            self.record(STATE_CHECK_FAILED, str(error))
            return 1

    def _run(self) -> int:
        deployment = self.deployment
        installed = self.installed_version()
        tag, latest = self.latest_release()
        self.document.update(
            installed_version=format_version(installed),
            latest_version=format_version(latest),
            latest_tag=tag,
        )
        if latest <= installed:
            self.record(STATE_UP_TO_DATE, f"BDEncode {format_version(installed)} is the newest release")
            return 0
        if self.check_only or not self.config.automatic_install:
            reason = "check only" if self.check_only else "automatic_install is off"
            self.record(STATE_AVAILABLE, f"{tag} is available ({reason}); install it manually")
            return 0
        if self.failed_attempts(tag) >= MAX_FAILED_ATTEMPTS:
            self.record(
                STATE_BLOCKED,
                f"automatic installation of {tag} stopped after {MAX_FAILED_ATTEMPTS} failed attempts; "
                "fix the cause and run the installer manually, or publish a newer release",
            )
            return 1
        if not self.passwordless_sudo():
            self.record(
                STATE_MANUAL,
                f"{tag} is available, but {deployment.task_user} has no passwordless sudo; "
                "run the installer manually",
            )
            return 0
        if not deployment.source_root.is_dir():
            self.record(STATE_DEFERRED, f"source root {deployment.source_root} is not available; will retry")
            return 0
        if not self.queue_is_idle():
            self.record(STATE_DEFERRED, f"{tag} is available, but the encode queue is busy; will retry")
            return 0

        self.clean_stale_checkouts()
        checkout = self.download_release(tag)
        try:
            commit = self.verify_checkout(checkout, latest)
            code = self.run_installer(checkout)
        finally:
            self.remove_checkout(checkout)
        if code == 3:
            self.record(STATE_DEFERRED, f"{tag} is available, but the installer found the queue busy; will retry")
            return 0
        if code != 0:
            self.report.echo_tail()
            count = self.failed_attempts(tag) + 1
            self.set_failed_attempts(tag, count)
            self.record(
                STATE_INSTALL_FAILED,
                f"installing {tag} failed (installer exit {code}, attempt {count} of "
                f"{MAX_FAILED_ATTEMPTS}); the previous release stays active",
            )
            return 1
        now_installed = self.installed_version()
        if now_installed != latest:
            self.set_failed_attempts(tag, self.failed_attempts(tag) + 1)
            self.record(
                STATE_INSTALL_FAILED,
                f"the installer finished but BDEncode is {format_version(now_installed)}, not {tag}",
            )
            return 1
        self.set_failed_attempts(tag, 0)
        self.record(
            STATE_INSTALLED,
            f"BDEncode {format_version(installed)} was updated to {tag}",
            installed_version=format_version(now_installed),
            installed_commit=commit,
            installed_at=self.clock(),
        )
        return 0


def task_home_of(user: str) -> Path:
    if pwd is None:  # pragma: no cover
        raise ReleaseUpdateError("the release updater runs on Linux only")
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError as error:
        raise ReleaseUpdateError(f"unknown BDEncode account: {user}") from error


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    result.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "check"),
        help="run: install a newer release unattended; check: only report",
    )
    result.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    result.add_argument("--release-config", type=Path, default=DEFAULT_RELEASE_CONFIG)
    result.add_argument("--service-config", type=Path, default=DEFAULT_SERVICE_CONFIG)
    # The unit sets BDENCODE_USER; an operator running the check with sudo is that account.
    result.add_argument("--user", default=os.environ.get("BDENCODE_USER") or os.environ.get("SUDO_USER"))
    result.add_argument("--data-root", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    # The updater starts the installer through runuser, which needs root; the test
    # switch only relaxes the root and ownership requirements, never the logic.
    testing = os.environ.get("BDENCODE_RELEASE_UPDATE_TESTING") == "1"
    if not testing and hasattr(os, "geteuid") and os.geteuid() != 0:
        print("bdencode-release-update must run as root", file=sys.stderr)
        return 2
    if not args.user:
        print("BDEncode account is unknown: set BDENCODE_USER or pass --user", file=sys.stderr)
        return 2
    state_root: Path = args.state_root
    state_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    rotate_log(state_root / "release-update.log")
    report = Reporter(state_root / "release-update.log")
    store = StatusStore(state_root / "status.json")
    if fcntl is not None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_handle = os.fdopen(os.open(state_root / "lock", flags, 0o600), "w")
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            report("another release check is running; nothing to do")
            return 0
    try:
        task_home = task_home_of(args.user)
        data_root = args.data_root or Path(os.environ.get("BDENCODE_DATA_ROOT") or task_home / "encode")
        deployment = load_deployment(
            task_user=args.user,
            task_home=task_home,
            data_root=data_root,
            service_config=args.service_config,
            windows_marker=WINDOWS_MARKER,
            wsl_nginx_config=WSL_NGINX_CONFIG,
        )
        config = load_release_config(args.release_config, require_root_owner=not testing)
    except ReleaseUpdateError as error:
        document = store.load()
        document.update(
            {"schema": STATUS_SCHEMA, "state": STATE_CHECK_FAILED, "message": str(error), "checked_at": utc_now()}
        )
        store.save(document)
        report(f"{STATE_CHECK_FAILED}: {error}")
        return 1
    updater = ReleaseUpdater(
        deployment, config, SystemRunner(), store, report, check_only=args.command == "check"
    )
    return updater.run()


if __name__ == "__main__":
    raise SystemExit(main())
