#!/usr/bin/env python3
"""Crash-safe, allowlisted APT transactions for the BDEncode media toolchain.

The updater cannot rely on filesystem snapshots on the target host.  This
helper therefore builds a verified vault containing both sides of every
package upgrade before dpkg is allowed to mutate the host.  Its root-owned
journal is also consumed by the boot recovery unit.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

try:  # pragma: no cover - the production target is Linux; pure helpers are tested on Windows too.
    import fcntl
except ModuleNotFoundError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


DEFAULT_STATE_ROOT = Path("/var/lib/bdencode/apt-transactions")
DEFAULT_SOURCES = Path("/etc/bdencode/media-apt.sources.list")
DEFAULT_GUARD = Path("/usr/local/libexec/bdencode-apt-guard")
REQUESTED_PACKAGES = (
    "ffmpeg",
    "libbluray-bin",
    "libbluray-dev",
    "libbluray2",
    "mediainfo",
    "mkvtoolnix",
    "x264",
    "x265",
)
ALLOWED_SOURCE_PACKAGES = {
    "ffmpeg",
    "libbluray",
    "libmediainfo",
    "libzen",
    "mediainfo",
    "mkvtoolnix",
    "x264",
    "x265",
}
RECOVERABLE_STATES = {
    "APPLYING",
    "APPLIED",
    "VALIDATING",
    "ROLLING_BACK",
    "RECOVERY_REQUIRED",
}
FINAL_STATES = {"ABORTED", "COMMITTED", "ROLLED_BACK"}
TXN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9]+$")
INSTALL_RE = re.compile(
    r"^Inst\s+(?P<package>\S+)\s+\[(?P<old>[^]]+)]\s+\((?P<new>\S+)"
)


class TransactionError(RuntimeError):
    """A fail-closed transaction or recovery error."""


@dataclass(frozen=True)
class PlannedUpgrade:
    query_name: str
    old_version: str
    new_version: str


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"{utc_now()} {message}", flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def parse_simulation(output: str) -> list[PlannedUpgrade]:
    upgrades: list[PlannedUpgrade] = []
    seen: set[str] = set()
    for line in output.splitlines():
        if line.startswith("Remv "):
            raise TransactionError(f"APT removal is forbidden: {line}")
        if not line.startswith("Inst "):
            continue
        match = INSTALL_RE.match(line)
        if not match:
            raise TransactionError(
                f"APT attempted a new install or emitted an unknown plan line: {line}"
            )
        query_name = match.group("package")
        if query_name in seen:
            raise TransactionError(f"Duplicate package in APT plan: {query_name}")
        seen.add(query_name)
        upgrades.append(
            PlannedUpgrade(
                query_name=query_name,
                old_version=match.group("old"),
                new_version=match.group("new"),
            )
        )
    return upgrades


def parse_control(text: str) -> dict[str, str]:
    message = email.parser.Parser().parsestr(text)
    return {key: str(value).strip() for key, value in message.items()}


def source_name(control: dict[str, str]) -> str:
    return control.get("Source", control["Package"]).split(maxsplit=1)[0]


class AptTransaction:
    def __init__(self, state_root: Path, sources: Path, guard: Path) -> None:
        self.state_root = state_root
        self.sources = sources
        self.guard = guard
        self.active_file = state_root / "active"
        self.lists_dir = state_root / "lists"
        self.lock_path = Path("/run/lock/bdencode-apt.lock")
        self._lock_handle: Any | None = None

    def require_root(self) -> None:
        if os.geteuid() != 0 and os.environ.get("BDENCODE_APT_TESTING") != "1":
            raise TransactionError("This command must run as root")

    def initialize(self, *, acquire_lock: bool = True) -> None:
        self.require_root()
        # World execute lets APT's unprivileged _apt downloader traverse to the
        # public index/cache directories. Transaction directories and the
        # active marker remain root-only (0700/0600).
        if self.state_root.exists() or self.state_root.is_symlink():
            details = os.lstat(self.state_root)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise TransactionError(f"Unsafe transaction root: {self.state_root}")
            if details.st_uid != os.geteuid() or details.st_mode & 0o022:
                raise TransactionError(
                    f"Transaction root must be root-owned: {self.state_root}"
                )
        else:
            self.state_root.mkdir(parents=True, mode=0o711)
        os.chmod(self.state_root, 0o711)
        details = os.lstat(self.state_root)
        if details.st_uid != os.geteuid() or details.st_mode & 0o022:
            raise TransactionError(
                f"Transaction root must be root-owned: {self.state_root}"
            )
        self.lists_dir.mkdir(mode=0o755, exist_ok=True)
        os.chmod(self.lists_dir, 0o755)
        lists_partial = self.lists_dir / "partial"
        lists_partial.mkdir(mode=0o700, exist_ok=True)
        try:
            import pwd

            apt_user = pwd.getpwnam("_apt")
        except (ImportError, KeyError) as error:
            raise TransactionError(
                "The Debian _apt sandbox account is missing"
            ) from error
        os.chown(lists_partial, apt_user.pw_uid, apt_user.pw_gid)
        os.chmod(lists_partial, 0o700)
        if acquire_lock:
            if fcntl is None:
                raise TransactionError(
                    "APT transaction locking requires Linux fcntl support"
                )
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._lock_handle = self.lock_path.open("a+")
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX)

    def apt_scope(self, archives: Path | None = None) -> list[str]:
        options = [
            "-o",
            f"Dir::Etc::sourcelist={self.sources}",
            "-o",
            "Dir::Etc::sourceparts=/dev/null",
            "-o",
            "Dir::Etc::preferences=/dev/null",
            "-o",
            "Dir::Etc::preferencesparts=/dev/null",
            "-o",
            f"Dir::State::lists={self.lists_dir}",
            "-o",
            "Dir::Cache::pkgcache=",
            "-o",
            "Dir::Cache::srcpkgcache=",
            "-o",
            "Acquire::Languages=none",
        ]
        if archives is not None:
            options += ["-o", f"Dir::Cache::archives={archives}"]
        return options

    @staticmethod
    def run(
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        capture: bool = True,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        printable = " ".join(command[:3])
        log(f"running: {printable}{' ...' if len(command) > 3 else ''}")
        effective_environment = os.environ.copy()
        effective_environment.update(
            {"LANG": "C", "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"}
        )
        if env:
            effective_environment.update(env)
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            env=effective_environment,
        )
        if check and completed.returncode:
            details = (completed.stderr or completed.stdout or "").strip()
            raise TransactionError(
                f"Command failed ({completed.returncode}): {printable}: {details}"
            )
        return completed

    def transaction_dir(self, transaction_id: str) -> Path:
        if not TXN_ID_RE.fullmatch(transaction_id):
            raise TransactionError(f"Invalid transaction id: {transaction_id!r}")
        path = self.state_root / transaction_id
        if path.parent != self.state_root:
            raise TransactionError("Unsafe transaction path")
        return path

    def active_id(self) -> str | None:
        if not self.active_file.exists():
            return None
        if self.active_file.is_symlink():
            raise TransactionError(
                "The active transaction marker must not be a symlink"
            )
        transaction_id = self.active_file.read_text(encoding="ascii").strip()
        self.transaction_dir(transaction_id)
        return transaction_id

    def active_dir(self) -> Path | None:
        transaction_id = self.active_id()
        if transaction_id is None:
            return None
        path = self.transaction_dir(transaction_id)
        if not path.is_dir() or path.is_symlink():
            raise TransactionError(
                f"Active transaction directory is missing or unsafe: {path}"
            )
        return path

    @staticmethod
    def state(path: Path) -> str:
        state_path = path / "state"
        if not state_path.is_file() or state_path.is_symlink():
            raise TransactionError(f"Missing or unsafe transaction state: {state_path}")
        return state_path.read_text(encoding="ascii").strip()

    @staticmethod
    def write_state(path: Path, state: str) -> None:
        atomic_write(path / "state", f"{state}\n".encode("ascii"))
        log(f"transaction {path.name}: {state}")

    @staticmethod
    def load_manifest(path: Path) -> dict[str, Any]:
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise TransactionError(f"Missing or unsafe manifest: {manifest_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TransactionError(f"Invalid transaction manifest: {error}") from error
        if manifest.get("schema") != 1 or manifest.get("transaction_id") != path.name:
            raise TransactionError("Transaction manifest identity mismatch")
        return manifest

    def clear_active(self, expected: str) -> None:
        if self.active_id() != expected:
            raise TransactionError("Active transaction changed unexpectedly")
        self.active_file.unlink()
        directory_fd = os.open(self.state_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def package_status(self, query_name: str) -> dict[str, Any]:
        fmt = (
            "${db:Status-Abbrev}\\n${Package}\\n${Version}\\n${Architecture}\\n"
            "${Essential}\\n${Protected}\\n${Installed-Size}\\n"
        )
        result = self.run(["dpkg-query", "-W", f"-f={fmt}", query_name])
        lines = result.stdout.splitlines()
        if len(lines) < 7 or not lines[0].startswith("ii"):
            raise TransactionError(f"Package is not fully installed: {query_name}")
        return {
            "query_name": query_name,
            "package": lines[1],
            "version": lines[2],
            "architecture": lines[3],
            "essential": lines[4].lower() == "yes",
            "protected": lines[5].lower() == "yes",
            "installed_size_kib": int(lines[6] or "0"),
        }

    def deb_control(self, path: Path) -> dict[str, str]:
        result = self.run(["dpkg-deb", "-f", str(path)])
        control = parse_control(result.stdout)
        for required in ("Package", "Version", "Architecture"):
            if not control.get(required):
                raise TransactionError(f"{path} has no {required} field")
        return control

    @staticmethod
    def mark_sets(run: Any) -> tuple[set[str], set[str]]:
        auto = set(run(["apt-mark", "showauto"]).stdout.split())
        held = set(run(["apt-mark", "showhold"]).stdout.split())
        return auto, held

    def preflight_integrity(self) -> None:
        audit = self.run(["dpkg", "--audit"])
        if audit.stdout.strip():
            raise TransactionError(
                f"dpkg reports an inconsistent pre-update state: {audit.stdout.strip()}"
            )
        self.run(["apt-get", "check"])

    def cleanup_unpublished(self) -> None:
        """Remove preparation debris which can never have mutated dpkg."""
        active = self.active_id()
        downloads_root = self.state_root / "downloads"
        if downloads_root.is_dir() and not downloads_root.is_symlink():
            for child in downloads_root.iterdir():
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and TXN_ID_RE.fullmatch(child.name)
                ):
                    if child.name != active:
                        shutil.rmtree(child)
        for child in self.state_root.iterdir():
            if (
                not child.is_dir()
                or child.is_symlink()
                or not TXN_ID_RE.fullmatch(child.name)
                or child.name == active
            ):
                continue
            state_path = child / "state"
            if not state_path.exists() or (
                state_path.is_file()
                and not state_path.is_symlink()
                and state_path.read_text(encoding="ascii").strip() == "PREPARED"
            ):
                shutil.rmtree(child)

    def prepare(self, transaction_id: str) -> bool:
        self.cleanup_unpublished()
        if self.active_id() is not None:
            raise TransactionError(
                "An unfinished APT transaction already exists; run recover first"
            )
        if not self.sources.is_file() or self.sources.is_symlink():
            raise TransactionError(
                f"Missing or unsafe media APT source list: {self.sources}"
            )
        source_details = os.lstat(self.sources)
        if source_details.st_uid != 0 or source_details.st_mode & 0o022:
            raise TransactionError(
                "The media APT source list must be root-owned and not writable"
            )
        self.preflight_integrity()
        transaction = self.transaction_dir(transaction_id)
        transaction.mkdir(mode=0o700)
        old_dir = transaction / "old"
        new_dir = transaction / "new"
        downloads_root = self.state_root / "downloads"
        downloads_root.mkdir(mode=0o755, exist_ok=True)
        os.chmod(downloads_root, 0o755)
        download_dir = downloads_root / transaction_id
        for directory in (old_dir, new_dir):
            directory.mkdir(mode=0o700)
        download_dir.mkdir(mode=0o755)
        download_partial = download_dir / "partial"
        download_partial.mkdir(mode=0o700)
        import pwd

        apt_user = pwd.getpwnam("_apt")
        os.chown(download_partial, apt_user.pw_uid, apt_user.pw_gid)

        self.run(["apt-get", *self.apt_scope(), "update"], capture=False)
        simulation = self.run(
            [
                "apt-get",
                *self.apt_scope(),
                "--simulate",
                "--only-upgrade",
                "--no-remove",
                "--no-install-recommends",
                "install",
                *REQUESTED_PACKAGES,
            ]
        )
        upgrades = parse_simulation(simulation.stdout)
        atomic_write(
            transaction / "apt-plan.txt", simulation.stdout.encode("utf-8"), 0o600
        )
        if not upgrades:
            shutil.rmtree(transaction)
            shutil.rmtree(download_dir)
            log("media APT transaction: no package changes")
            return False

        auto, held = self.mark_sets(self.run)
        installed: dict[str, dict[str, Any]] = {}
        required_kib = 64 * 1024
        for upgrade in upgrades:
            status = self.package_status(upgrade.query_name)
            if status["version"] != upgrade.old_version:
                raise TransactionError(
                    f"APT plan/install state mismatch for {upgrade.query_name}: "
                    f"{upgrade.old_version} != {status['version']}"
                )
            if status["essential"] or status["protected"]:
                raise TransactionError(
                    f"Refusing to upgrade Essential/Protected package: {upgrade.query_name}"
                )
            base_name = status["package"]
            if upgrade.query_name in held or base_name in held:
                raise TransactionError(
                    f"Refusing to upgrade held package: {upgrade.query_name}"
                )
            installed[upgrade.query_name] = status
            required_kib += status["installed_size_kib"]
        if shutil.disk_usage(self.state_root).free < required_kib * 1024:
            raise TransactionError(
                f"Not enough space for verified rollback vault ({required_kib} KiB required)"
            )

        self.run(
            [
                "apt-get",
                *self.apt_scope(download_dir),
                "--download-only",
                "-y",
                "--only-upgrade",
                "--no-remove",
                "--no-install-recommends",
                "-o",
                "APT::Keep-Downloaded-Packages=true",
                "install",
                *REQUESTED_PACKAGES,
            ],
            capture=False,
        )
        downloaded: dict[tuple[str, str, str], tuple[Path, dict[str, str]]] = {}
        for archive in download_dir.glob("*.deb"):
            control = self.deb_control(archive)
            key = (control["Package"], control["Architecture"], control["Version"])
            if key in downloaded:
                raise TransactionError(f"Duplicate candidate archive: {key}")
            downloaded[key] = (archive, control)

        packages: list[dict[str, Any]] = []
        for index, upgrade in enumerate(upgrades):
            status = installed[upgrade.query_name]
            candidate_key = (
                status["package"],
                status["architecture"],
                upgrade.new_version,
            )
            if candidate_key not in downloaded:
                raise TransactionError(
                    f"Downloaded candidate is missing: {candidate_key}"
                )
            candidate_source, candidate_control = downloaded[candidate_key]
            candidate_source_name = source_name(candidate_control)
            if candidate_source_name not in ALLOWED_SOURCE_PACKAGES:
                raise TransactionError(
                    f"Candidate source package is not allowlisted: {candidate_source_name} ({upgrade.query_name})"
                )
            if (
                candidate_control.get("Essential", "no").lower() == "yes"
                or candidate_control.get("Protected", "no").lower() == "yes"
            ):
                raise TransactionError(
                    f"Candidate became Essential/Protected: {upgrade.query_name}"
                )

            candidate_path = new_dir / f"{index:04d}.deb"
            shutil.copy2(candidate_source, candidate_path)
            os.chmod(candidate_path, 0o600)
            fsync_path(candidate_path)

            repack_dir = transaction / f".repack-{index:04d}"
            repack_dir.mkdir(mode=0o700)
            try:
                self.run(
                    ["dpkg-repack", "--tag=none", upgrade.query_name],
                    cwd=repack_dir,
                    capture=False,
                )
                repacked = list(repack_dir.glob("*.deb"))
                if len(repacked) != 1:
                    raise TransactionError(
                        f"dpkg-repack produced {len(repacked)} archives for {upgrade.query_name}"
                    )
                old_path = old_dir / f"{index:04d}.deb"
                shutil.move(repacked[0], old_path)
                os.chmod(old_path, 0o600)
                fsync_path(old_path)
            finally:
                shutil.rmtree(repack_dir, ignore_errors=True)

            old_control = self.deb_control(old_path)
            expected_old = (
                status["package"],
                status["architecture"],
                status["version"],
            )
            actual_old = (
                old_control["Package"],
                old_control["Architecture"],
                old_control["Version"],
            )
            if actual_old != expected_old:
                raise TransactionError(
                    f"Rollback archive identity mismatch: {actual_old} != {expected_old}"
                )

            packages.append(
                {
                    "query_name": upgrade.query_name,
                    "package": status["package"],
                    "architecture": status["architecture"],
                    "old_version": status["version"],
                    "new_version": upgrade.new_version,
                    "source_package": candidate_source_name,
                    "was_auto": upgrade.query_name in auto or status["package"] in auto,
                    "was_held": False,
                    "old_deb": str(old_path.relative_to(transaction)),
                    "old_sha256": sha256_file(old_path),
                    "new_deb": str(candidate_path.relative_to(transaction)),
                    "new_sha256": sha256_file(candidate_path),
                }
            )

        expected_keys = {
            (entry["package"], entry["architecture"], entry["new_version"])
            for entry in packages
        }
        if set(downloaded) != expected_keys:
            extras = sorted(set(downloaded) - expected_keys)
            raise TransactionError(
                f"APT downloaded archives outside the verified plan: {extras}"
            )
        shutil.rmtree(download_dir)

        selections = self.run(["dpkg", "--get-selections"]).stdout
        atomic_write(transaction / "dpkg-selections.before", selections.encode("utf-8"))
        shutil.copy2("/var/lib/dpkg/status", transaction / "dpkg-status.before")
        os.chmod(transaction / "dpkg-status.before", 0o600)
        fsync_path(transaction / "dpkg-status.before")
        fsync_directory(old_dir)
        fsync_directory(new_dir)
        fsync_directory(transaction)
        manifest = {
            "schema": 1,
            "transaction_id": transaction_id,
            "created_at": utc_now(),
            "requested_packages": list(REQUESTED_PACKAGES),
            "sources_sha256": sha256_file(self.sources),
            "packages": packages,
        }
        atomic_write(
            transaction / "manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        self.write_state(transaction, "PREPARED")
        atomic_write(self.active_file, f"{transaction_id}\n".encode("ascii"))
        log(f"prepared verified rollback vault for {len(packages)} package(s)")
        return True

    def validate_archives(
        self, transaction: Path, manifest: dict[str, Any], side: str
    ) -> list[Path]:
        paths: list[Path] = []
        for package in manifest["packages"]:
            relative = package[f"{side}_deb"]
            unresolved = transaction / relative
            if unresolved.is_symlink():
                raise TransactionError(f"Unsafe {side} archive symlink: {unresolved}")
            path = unresolved.resolve(strict=True)
            expected_parent = (transaction / side).resolve(strict=True)
            if path.parent != expected_parent:
                raise TransactionError(f"Unsafe {side} archive path: {path}")
            if sha256_file(path) != package[f"{side}_sha256"]:
                raise TransactionError(f"Hash mismatch for {path}")
            control = self.deb_control(path)
            expected = (
                package["package"],
                package["architecture"],
                package[f"{side}_version"],
            )
            actual = (control["Package"], control["Architecture"], control["Version"])
            if actual != expected:
                raise TransactionError(
                    f"Archive identity mismatch: {actual} != {expected}"
                )
            paths.append(path)
        return paths

    def verify_versions(self, manifest: dict[str, Any], side: str) -> None:
        mismatches: list[str] = []
        for package in manifest["packages"]:
            actual = self.package_status(package["query_name"])
            expected = package[f"{side}_version"]
            if (
                actual["version"] != expected
                or actual["architecture"] != package["architecture"]
            ):
                mismatches.append(
                    f"{package['query_name']}={actual['version']} (expected {expected})"
                )
        if mismatches:
            raise TransactionError(
                "Package version verification failed: " + ", ".join(mismatches)
            )

    def verify_integrity(self) -> None:
        audit = self.run(["dpkg", "--audit"])
        if audit.stdout.strip():
            raise TransactionError(f"dpkg audit failed: {audit.stdout.strip()}")
        self.run(["apt-get", "check"])

    def verify_marks(self, manifest: dict[str, Any]) -> None:
        auto, held = self.mark_sets(self.run)
        mismatches: list[str] = []
        for package in manifest["packages"]:
            names = {package["query_name"], package["package"]}
            is_auto = bool(names & auto)
            is_held = bool(names & held)
            if is_auto != package["was_auto"] or is_held != package["was_held"]:
                mismatches.append(package["query_name"])
        if mismatches:
            raise TransactionError(
                "APT mark restoration failed for: " + ", ".join(mismatches)
            )

    @staticmethod
    def prepare_private_cache(path: Path) -> None:
        """Create or validate an idempotently reusable root-only APT cache."""
        for directory in (path, path / "partial"):
            if directory.exists() or directory.is_symlink():
                details = os.lstat(directory)
                expected_uid = (
                    os.geteuid() if hasattr(os, "geteuid") else details.st_uid
                )
                if (
                    stat.S_ISLNK(details.st_mode)
                    or not stat.S_ISDIR(details.st_mode)
                    or (os.name == "posix" and details.st_uid != expected_uid)
                    or (os.name == "posix" and details.st_mode & 0o077)
                ):
                    raise TransactionError(
                        f"Unsafe rollback cache directory: {directory}"
                    )
            else:
                directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
        if hasattr(os, "O_DIRECTORY"):
            fsync_directory(path / "partial")
            fsync_directory(path)

    def apply(self) -> bool:
        transaction = self.active_dir()
        if transaction is None:
            log("media APT transaction: nothing to apply")
            return False
        if self.state(transaction) != "PREPARED":
            raise TransactionError(
                f"Cannot apply transaction in state {self.state(transaction)}"
            )
        manifest = self.load_manifest(transaction)
        new_archives = self.validate_archives(transaction, manifest, "new")
        self.write_state(transaction, "APPLYING")
        environment = os.environ.copy()
        environment["BDENCODE_APT_STATE_ROOT"] = str(self.state_root)
        command = [
            "apt-get",
            *self.apt_scope(),
            "-y",
            "--no-download",
            "--only-upgrade",
            "--no-remove",
            "--no-install-recommends",
            "-o",
            "Dpkg::Options::=--force-confold",
            "-o",
            f"DPkg::Pre-Install-Pkgs::={self.guard}",
            "-o",
            f"DPkg::Tools::Options::{self.guard}::Version=3",
            "install",
            *[str(path) for path in new_archives],
        ]
        self.run(command, capture=False, env=environment)
        self.restore_marks(manifest)
        self.verify_versions(manifest, "new")
        self.verify_marks(manifest)
        self.verify_integrity()
        self.run(["ldconfig"], capture=False)
        self.write_state(transaction, "APPLIED")
        return True

    def begin_validation(self) -> None:
        transaction = self.active_dir()
        if transaction is None:
            return
        if self.state(transaction) != "APPLIED":
            raise TransactionError(
                f"Cannot validate transaction in state {self.state(transaction)}"
            )
        self.write_state(transaction, "VALIDATING")

    def restore_marks(self, manifest: dict[str, Any]) -> None:
        for package in manifest["packages"]:
            mark = "auto" if package["was_auto"] else "manual"
            self.run(["apt-mark", mark, package["query_name"]], capture=False)
            hold = "hold" if package["was_held"] else "unhold"
            self.run(["apt-mark", hold, package["query_name"]], capture=False)

    def rollback(self, transaction: Path) -> None:
        try:
            manifest = self.load_manifest(transaction)
            old_archives = self.validate_archives(transaction, manifest, "old")
            self.write_state(transaction, "ROLLING_BACK")
            rollback_cache = transaction / "rollback-cache"
            self.prepare_private_cache(rollback_cache)
            command = [
                "apt-get",
                *self.apt_scope(rollback_cache),
                "-y",
                "--allow-downgrades",
                "--no-download",
                "--only-upgrade",
                "--no-remove",
                "--no-install-recommends",
                "-o",
                "Dpkg::Options::=--force-confold",
                "install",
                *[str(path) for path in old_archives],
            ]
            normal = self.run(command, capture=False, check=False)
            if normal.returncode:
                log(
                    "normal APT downgrade failed; attempting the offline dpkg recovery path"
                )
                self.run(
                    [
                        "dpkg",
                        "--force-downgrade",
                        "--force-confdef",
                        "--force-confold",
                        "--unpack",
                        *[str(path) for path in old_archives],
                    ],
                    capture=False,
                )
                self.run(
                    [
                        "dpkg",
                        "--force-confdef",
                        "--force-confold",
                        "--configure",
                        "--pending",
                    ],
                    capture=False,
                )
            self.restore_marks(manifest)
            self.run(["ldconfig"], capture=False)
            self.verify_versions(manifest, "old")
            self.verify_marks(manifest)
            self.verify_integrity()
        except Exception:
            try:
                self.write_state(transaction, "RECOVERY_REQUIRED")
            except Exception as state_error:
                log(f"could not persist RECOVERY_REQUIRED: {state_error}")
            raise
        self.write_state(transaction, "ROLLED_BACK")
        self.clear_active(transaction.name)
        self.prune_nonfatal(keep=3)
        log("APT media stack restored to the exact pre-update versions")

    def recover(self) -> bool:
        transaction = self.active_dir()
        if transaction is None:
            log("APT recovery: no unfinished transaction")
            return False
        state = self.state(transaction)
        if state == "PREPARED":
            self.write_state(transaction, "ABORTED")
            self.clear_active(transaction.name)
            self.prune_nonfatal(keep=3)
            log("discarded a prepared transaction which had not mutated the host")
            return False
        if state in RECOVERABLE_STATES:
            self.rollback(transaction)
            return True
        if state in FINAL_STATES:
            self.clear_active(transaction.name)
            self.prune_nonfatal(keep=3)
            return False
        raise TransactionError(f"Unknown transaction state: {state}")

    def commit(self) -> None:
        transaction = self.active_dir()
        if transaction is None:
            log("media APT transaction: no package changes to commit")
            return
        state = self.state(transaction)
        if state not in {"APPLIED", "VALIDATING"}:
            raise TransactionError(f"Cannot commit transaction in state {state}")
        manifest = self.load_manifest(transaction)
        self.verify_versions(manifest, "new")
        self.verify_integrity()
        self.write_state(transaction, "COMMITTED")
        self.clear_active(transaction.name)
        self.prune_nonfatal(keep=3)
        log("APT media transaction committed after runtime health checks")

    def prune_nonfatal(self, keep: int) -> None:
        try:
            self.prune(keep=keep)
        except Exception as error:
            log(f"non-fatal transaction retention warning: {error}")

    def prune(self, keep: int) -> None:
        finished: list[Path] = []
        for child in self.state_root.iterdir():
            if (
                not child.is_dir()
                or child.is_symlink()
                or not TXN_ID_RE.fullmatch(child.name)
            ):
                continue
            try:
                if self.state(child) in FINAL_STATES:
                    finished.append(child)
            except TransactionError:
                continue
        finished.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for obsolete in finished[keep:]:
            if obsolete.parent != self.state_root:
                raise TransactionError(f"Refusing to prune unsafe path: {obsolete}")
            shutil.rmtree(obsolete)

    def guard_apt(self) -> None:
        transaction = self.active_dir()
        if transaction is None or self.state(transaction) != "APPLYING":
            raise TransactionError("APT guard invoked without an APPLYING transaction")
        manifest = self.load_manifest(transaction)
        expected = {
            (entry["package"], entry["architecture"]): entry
            for entry in manifest["packages"]
        }
        descriptor_text = os.environ.get("APT_HOOK_INFO_FD")
        if descriptor_text is None:
            raise TransactionError("APT did not provide APT_HOOK_INFO_FD")
        try:
            descriptor = int(descriptor_text)
        except ValueError as error:
            raise TransactionError("Invalid APT_HOOK_INFO_FD") from error
        with os.fdopen(
            os.dup(descriptor), "r", encoding="utf-8", errors="strict"
        ) as stream:
            lines = stream.read().splitlines()
        if not lines or lines[0] != "VERSION 3":
            raise TransactionError("APT hook protocol v3 is required")
        try:
            actions_start = lines.index("") + 1
        except ValueError as error:
            raise TransactionError(
                "APT hook configuration terminator is missing"
            ) from error
        seen: set[tuple[str, str]] = set()
        for line in lines[actions_start:]:
            if not line:
                continue
            fields = line.split(maxsplit=8)
            if len(fields) != 9:
                raise TransactionError(f"Malformed APT hook action: {line}")
            (
                name,
                old_version,
                old_arch,
                _old_multi,
                direction,
                new_version,
                new_arch,
                _new_multi,
                action,
            ) = fields
            key = (name, new_arch)
            entry = expected.get(key)
            if entry is None:
                raise TransactionError(
                    f"APT attempted an unplanned package action: {line}"
                )
            if old_version == "-" or old_arch == "-" or direction != "<":
                raise TransactionError(f"APT action is not a verified upgrade: {line}")
            if old_version != entry["old_version"] or old_arch != entry["architecture"]:
                raise TransactionError(
                    f"APT old package identity changed after preparation: {line}"
                )
            if new_version != entry["new_version"] or new_arch != entry["architecture"]:
                raise TransactionError(
                    f"APT candidate identity changed after preparation: {line}"
                )
            if action in {"**REMOVE**", "**CONFIGURE**"}:
                if action == "**REMOVE**":
                    raise TransactionError(f"APT attempted package removal: {line}")
                continue
            archive = Path(action).resolve(strict=True)
            expected_archive = (transaction / entry["new_deb"]).resolve(strict=True)
            if (
                archive != expected_archive
                or sha256_file(archive) != entry["new_sha256"]
            ):
                raise TransactionError(
                    f"APT attempted an unverified archive: {archive}"
                )
            seen.add(key)
        if seen != set(expected):
            missing = sorted(set(expected) - seen)
            raise TransactionError(
                f"APT hook did not receive every planned package archive: {missing}"
            )
        log(f"APT guard approved {len(seen)} exact local package archive(s)")

    def show_status(self) -> None:
        transaction = self.active_dir()
        if transaction is None:
            print(json.dumps({"active": False}, sort_keys=True))
            return
        manifest = self.load_manifest(transaction)
        print(
            json.dumps(
                {
                    "active": True,
                    "transaction_id": transaction.name,
                    "state": self.state(transaction),
                    "packages": len(manifest["packages"]),
                },
                sort_keys=True,
            )
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command",
        choices=(
            "prepare",
            "apply",
            "validate",
            "commit",
            "recover",
            "status",
            "guard",
        ),
    )
    result.add_argument("--transaction-id")
    result.add_argument(
        "--state-root",
        type=Path,
        default=Path(os.environ.get("BDENCODE_APT_STATE_ROOT", DEFAULT_STATE_ROOT)),
    )
    result.add_argument(
        "--sources",
        type=Path,
        default=Path(os.environ.get("BDENCODE_APT_SOURCES", DEFAULT_SOURCES)),
    )
    result.add_argument(
        "--guard",
        type=Path,
        default=Path(os.environ.get("BDENCODE_APT_GUARD", DEFAULT_GUARD)),
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    if Path(sys.argv[0]).name == "bdencode-apt-guard":
        actual_argv.insert(0, "guard")
    args = parser().parse_args(actual_argv)
    transaction = AptTransaction(args.state_root, args.sources, args.guard)
    try:
        # The APT guard is a child of the apply process.  The parent deliberately
        # holds the transaction lock, so acquiring it again here would deadlock.
        transaction.initialize(acquire_lock=args.command != "guard")
        if args.command == "prepare":
            if not args.transaction_id:
                raise TransactionError("prepare requires --transaction-id")
            transaction.prepare(args.transaction_id)
        elif args.command == "apply":
            transaction.apply()
        elif args.command == "validate":
            transaction.begin_validation()
        elif args.command == "commit":
            transaction.commit()
        elif args.command == "recover":
            transaction.recover()
        elif args.command == "status":
            transaction.show_status()
        elif args.command == "guard":
            transaction.guard_apt()
        return 0
    except (OSError, subprocess.SubprocessError, TransactionError) as error:
        print(f"bdencode APT transaction error: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
