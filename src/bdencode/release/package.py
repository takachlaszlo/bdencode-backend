"""Fail-closed construction of a private tracker upload kit.

Only explicitly supplied, typed artifacts are admitted.  Pipeline reports,
worker logs and arbitrary directories are intentionally not accepted by this
API, so they cannot accidentally become part of a tracker upload.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Sequence

from ..maintenance import safe_tree_usage

from .models import (
    PackageFile,
    PackageFileRole,
    ReleaseMetadata,
    ReleasePackageManifest,
    TrackerProfile,
    UploadRequest,
)


_MAX_TEXT_BYTES = 2 * 1024 * 1024
_MAX_SCREENSHOT_BYTES = 64 * 1024 * 1024
_SENSITIVE_LINE = re.compile(
    r"(?i)(?:passkey|api[ _-]?key|authorization|bearer|access[ _-]?token|"
    r"refresh[ _-]?token|password|passwd|cookie|secret)\s*(?::|=)"
)
_PATH_LINE = re.compile(
    r"(?i)^\s*(?:complete name|folder name|file path|source path|work path)\s*(?::|=)"
)
_PRIVATE_PATH = re.compile(
    r"(?i)(?:[A-Z]:\\[^\s\]\[\"']+|\\\\[^\s\]\[\"']+|"
    r"/(?:home|srv|tmp|var/lib|mnt)/[^\s\]\[\"']+)"
)
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
)
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_SCREENSHOT_MEDIA = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_ROLE_MEDIA = {
    PackageFileRole.TORRENT: "application/x-bittorrent",
    PackageFileRole.MEDIAINFO: "text/plain; charset=utf-8",
    PackageFileRole.NFO: "text/plain; charset=utf-8",
    PackageFileRole.DESCRIPTION: "text/plain; charset=utf-8",
    PackageFileRole.CHECKSUMS: "text/plain; charset=utf-8",
    PackageFileRole.UPLOAD_REQUEST: "application/json",
}


class UploadKitError(RuntimeError):
    """An upload kit could not be built without weakening an invariant."""


@dataclass(frozen=True, slots=True)
class UploadKitResult:
    directory: Path
    manifest_path: Path
    manifest: ReleasePackageManifest
    manifest_sha256: str
    torrent_path: Path


def _is_reparse(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _assert_plain_path(path: Path, *, include_leaf: bool = True) -> None:
    """Reject symlinks and Windows reparse points in every existing component."""

    absolute = path.absolute()
    components = list(absolute.parents)
    components.reverse()
    components.append(absolute)
    if not include_leaf:
        components.pop()
    for component in components:
        try:
            current = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(current.st_mode) or _is_reparse(current):
            raise UploadKitError(
                f"linked or reparse path component is forbidden: {component}"
            )


def _within(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _secure_read(
    path: Path,
    *,
    roots: Sequence[Path],
    maximum_bytes: int,
) -> bytes:
    if not roots:
        raise UploadKitError("at least one allowlisted root is required")
    path = Path(path)
    _assert_plain_path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise UploadKitError(f"input must be a regular non-reparse file: {path}")
    resolved_roots: list[Path] = []
    for root in roots:
        _assert_plain_path(Path(root))
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise UploadKitError(f"allowlisted root is not a directory: {root}")
        resolved_roots.append(resolved)
    resolved = path.resolve(strict=True)
    if not _within(resolved, resolved_roots):
        raise UploadKitError(f"input escapes its allowlisted root: {path}")
    if before.st_size > maximum_bytes:
        raise UploadKitError(f"input exceeds its size limit: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_opened = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity_before != identity_opened or not stat.S_ISREG(opened.st_mode):
            raise UploadKitError(f"input changed while it was opened: {path}")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = path.lstat()
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    identity_after_open = (
        after_open.st_dev,
        after_open.st_ino,
        after_open.st_size,
        after_open.st_mtime_ns,
    )
    if identity_before != identity_after or identity_before != identity_after_open:
        raise UploadKitError(f"input changed while it was read: {path}")
    if len(data) > maximum_bytes:
        raise UploadKitError(f"input exceeds its size limit: {path}")
    return data


def sanitize_release_text(
    value: str,
    *,
    payload_filename: str,
    maximum_bytes: int = _MAX_TEXT_BYTES,
) -> str:
    """Normalize text and remove common credential/path disclosure fields."""

    if not isinstance(value, str):
        raise TypeError("release text must be supplied as a string")
    value = _ANSI_ESCAPE.sub("", value).replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in value or any(unicodedata.category(char) in {"Cs"} for char in value):
        raise UploadKitError("release text contains invalid characters")
    lines: list[str] = []
    for raw_line in value.split("\n"):
        line = "".join(
            char
            for char in raw_line
            if char == "\t" or unicodedata.category(char) not in {"Cc", "Cf"}
        ).rstrip()
        if _SENSITIVE_LINE.search(line):
            continue
        if _PATH_LINE.match(line):
            label = re.split(r"[:=]", line, maxsplit=1)[0].strip()
            line = f"{label} : {payload_filename}"
        else:
            line = _PRIVATE_PATH.sub("<redacted-path>", line)
            line = _UUID.sub("<redacted-id>", line)
        lines.append(line)
    normalized = unicodedata.normalize("NFC", "\n".join(lines)).strip() + "\n"
    if len(normalized.encode("utf-8")) > maximum_bytes:
        raise UploadKitError("release text exceeds its size limit")
    return normalized


def _validate_screenshot(data: bytes, suffix: str) -> None:
    valid = False
    if suffix == ".png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif suffix in {".jpg", ".jpeg"}:
        valid = data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    elif suffix == ".webp":
        valid = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if not valid:
        raise UploadKitError(f"screenshot content does not match {suffix}")


def _write_file(path: Path, data: bytes, *, mode: int = 0o640) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, mode)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _file_record(
    path: Path,
    root: Path,
    role: PackageFileRole,
    size: int,
    sha256: str,
) -> PackageFile:
    relative = path.relative_to(root).as_posix()
    media_type = (
        _SCREENSHOT_MEDIA[path.suffix.casefold()]
        if role is PackageFileRole.SCREENSHOT
        else _ROLE_MEDIA[role]
    )
    return PackageFile(
        path=relative,
        size=size,
        sha256=sha256,
        role=role,
        media_type=media_type,
    )


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_upload_kit(
    output_directory: Path,
    *,
    profile: TrackerProfile,
    metadata: ReleaseMetadata,
    torrent_path: Path,
    payload_path: Path,
    mediainfo: str,
    nfo: str,
    description_bbcode: str,
    screenshots: Sequence[Path],
    screenshot_roots: Sequence[Path],
    created_at: datetime | None = None,
) -> UploadKitResult:
    """Build and atomically publish a validated, private upload-kit directory."""

    # Deferred import keeps the models and text sanitizer usable independently.
    from .torrent import TorrentError, verify_torrent

    if metadata.release_name != payload_path.stem:
        raise UploadKitError("payload filename must match the release name")
    if payload_path.suffix.casefold() != ".mkv":
        raise UploadKitError("payload must be an MKV file")
    if not profile.screenshot_minimum <= len(screenshots) <= profile.screenshot_maximum:
        raise UploadKitError("screenshot count violates the tracker profile")
    if created_at is None:
        created_at = datetime.now(UTC)
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise UploadKitError("created_at must be timezone-aware")

    output_directory = Path(output_directory)
    parent = output_directory.parent
    _assert_plain_path(parent)
    parent = parent.resolve(strict=True)
    if not parent.is_dir() or output_directory.parent.resolve(strict=True) != parent:
        raise UploadKitError("upload-kit parent is invalid")
    if output_directory.exists() or output_directory.is_symlink():
        raise FileExistsError(output_directory)

    torrent_bytes = _secure_read(
        torrent_path,
        roots=(torrent_path.parent,),
        maximum_bytes=16 * 1024 * 1024,
    )
    torrent_profile = profile.torrent_profile()
    try:
        verification = verify_torrent(
            torrent_bytes,
            expected_release_name=metadata.release_name,
            payload_file=payload_path,
            expected_profile=torrent_profile,
        )
    except TorrentError as exc:
        raise UploadKitError("torrent and payload verification failed") from exc
    payload_size = verification.file_size
    payload_sha256 = verification.file_sha256
    if payload_size < 1 or payload_sha256 is None:
        raise UploadKitError("torrent verification did not prove the payload")

    screenshot_inputs: list[tuple[str, bytes]] = []
    seen_input_names: set[str] = set()
    for index, screenshot in enumerate(screenshots, start=1):
        screenshot = Path(screenshot)
        suffix = screenshot.suffix.casefold()
        if suffix not in _SCREENSHOT_MEDIA:
            raise UploadKitError("screenshots must be PNG, JPEG or WebP")
        folded = unicodedata.normalize("NFC", screenshot.name).casefold()
        if folded in seen_input_names:
            raise UploadKitError("screenshot names must be Unicode/casefold unique")
        seen_input_names.add(folded)
        data = _secure_read(
            screenshot,
            roots=screenshot_roots,
            maximum_bytes=_MAX_SCREENSHOT_BYTES,
        )
        _validate_screenshot(data, suffix)
        screenshot_inputs.append((f"screenshots/{index:02d}{suffix}", data))

    stage = Path(tempfile.mkdtemp(prefix=f".{output_directory.name}.tmp-", dir=parent))
    try:
        os.chmod(stage, 0o750)
        screenshots_dir = stage / "screenshots"
        screenshots_dir.mkdir(mode=0o750)
        artifacts: list[tuple[Path, PackageFileRole, int, str]] = []

        torrent_destination = stage / f"{metadata.release_name}.torrent"
        _write_file(torrent_destination, torrent_bytes)
        artifacts.append(
            (
                torrent_destination,
                PackageFileRole.TORRENT,
                len(torrent_bytes),
                hashlib.sha256(torrent_bytes).hexdigest(),
            )
        )

        text_values = (
            ("mediainfo.txt", mediainfo, PackageFileRole.MEDIAINFO),
            (f"{metadata.release_name}.nfo", nfo, PackageFileRole.NFO),
            ("description.bbcode", description_bbcode, PackageFileRole.DESCRIPTION),
        )
        for filename, value, role in text_values:
            sanitized = sanitize_release_text(
                value,
                payload_filename=f"{metadata.release_name}.mkv",
            )
            text_data = sanitized.encode("utf-8")
            destination = stage / filename
            _write_file(destination, text_data)
            artifacts.append(
                (
                    destination,
                    role,
                    len(text_data),
                    hashlib.sha256(text_data).hexdigest(),
                )
            )

        for relative, data in screenshot_inputs:
            destination = stage / Path(relative)
            _write_file(destination, data)
            artifacts.append(
                (
                    destination,
                    PackageFileRole.SCREENSHOT,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                )
            )

        upload_request = UploadRequest(
            profile_id=profile.profile_id,
            release_name=metadata.release_name,
            metadata_sha256=metadata.canonical_digest(),
            torrent_infohash=verification.infohash,
            requested_at=created_at,
        )
        request_path = stage / "upload-request.json"
        request_data = _json_bytes(upload_request.model_dump(mode="json"))
        _write_file(request_path, request_data)
        artifacts.append(
            (
                request_path,
                PackageFileRole.UPLOAD_REQUEST,
                len(request_data),
                hashlib.sha256(request_data).hexdigest(),
            )
        )

        checksum_lines: list[str] = []
        for artifact, _role, _size, digest in sorted(
            artifacts,
            key=lambda item: item[0].relative_to(stage).as_posix(),
        ):
            relative = artifact.relative_to(stage).as_posix()
            checksum_lines.append(f"{digest}  {relative}")
        checksums_path = stage / "SHA256SUMS"
        checksums_data = ("\n".join(checksum_lines) + "\n").encode("utf-8")
        _write_file(checksums_path, checksums_data)
        artifacts.append(
            (
                checksums_path,
                PackageFileRole.CHECKSUMS,
                len(checksums_data),
                hashlib.sha256(checksums_data).hexdigest(),
            )
        )

        files = tuple(
            _file_record(path, stage, role, size, digest)
            for path, role, size, digest in sorted(
                artifacts,
                key=lambda item: item[0].relative_to(stage).as_posix(),
            )
        )
        manifest = ReleasePackageManifest(
            release_name=metadata.release_name,
            profile_id=profile.profile_id,
            metadata_sha256=metadata.canonical_digest(),
            torrent_infohash=verification.infohash,
            payload_path=f"{metadata.release_name}/{metadata.release_name}.mkv",
            payload_size=payload_size,
            payload_sha256=payload_sha256,
            files=files,
            created_at=created_at,
        )
        manifest_path = stage / "package-manifest.json"
        manifest_bytes = manifest.canonical_bytes()
        manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
        _write_file(manifest_path, manifest_bytes)
        _fsync_directory(screenshots_dir)
        _fsync_directory(stage)
        staged_manifest = verify_upload_kit(
            stage,
            expected_manifest_sha256=manifest_digest,
        )
        if staged_manifest != manifest:
            raise UploadKitError(
                "staged upload kit failed model round-trip verification"
            )

        if output_directory.exists() or output_directory.is_symlink():
            raise FileExistsError(output_directory)
        os.rename(stage, output_directory)
        _fsync_directory(parent)
        final_manifest_model = verify_upload_kit(
            output_directory,
            expected_manifest_sha256=manifest_digest,
        )
        if final_manifest_model != manifest:
            raise UploadKitError("published upload kit failed model verification")
    except BaseException:
        # ``stage`` is an exact mkdtemp result beneath the validated parent.
        if (
            stage.exists()
            and stage.parent == parent
            and stage.name.startswith(f".{output_directory.name}.tmp-")
        ):
            safe_tree_usage(stage)
            shutil.rmtree(stage)
        raise

    final_manifest = output_directory / "package-manifest.json"
    return UploadKitResult(
        directory=output_directory,
        manifest_path=final_manifest,
        manifest=manifest,
        manifest_sha256=manifest_digest,
        torrent_path=output_directory / torrent_destination.name,
    )


def verify_upload_kit(
    directory: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> ReleasePackageManifest:
    """Revalidate every manifest-bound artifact immediately before a side effect."""

    manifest, _artifacts = load_verified_upload_kit(
        directory,
        expected_manifest_sha256=expected_manifest_sha256,
        retain_artifacts=False,
    )
    return manifest


def load_verified_upload_kit(
    directory: Path,
    *,
    expected_manifest_sha256: str | None = None,
    retain_artifacts: bool = True,
) -> tuple[ReleasePackageManifest, dict[str, bytes]]:
    """Verify a kit and optionally return the exact checked artifact bytes.

    Network adapters use the returned bytes, closing the verify-then-read race
    that would otherwise exist immediately before a tracker request.
    """

    directory = Path(directory)
    _assert_plain_path(directory)
    root = directory.resolve(strict=True)
    if not root.is_dir():
        raise UploadKitError("upload kit must be a directory")
    manifest_path = root / "package-manifest.json"
    manifest_bytes = _secure_read(
        manifest_path,
        roots=(root,),
        maximum_bytes=2 * 1024 * 1024,
    )
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise UploadKitError("package manifest does not match the approved digest")
    try:
        manifest = ReleasePackageManifest.model_validate_json(manifest_bytes)
    except ValueError as exc:
        raise UploadKitError("package manifest is invalid") from exc
    if manifest.canonical_bytes() != manifest_bytes:
        raise UploadKitError("package manifest is not canonically serialized")

    expected = {item.path for item in manifest.files} | {"package-manifest.json"}
    actual: set[str] = set()
    for candidate in root.rglob("*"):
        _assert_plain_path(candidate)
        if candidate.is_file():
            actual.add(candidate.relative_to(root).as_posix())
    if actual != expected:
        raise UploadKitError("upload kit contains missing or unmanifested files")
    artifacts: dict[str, bytes] = {}
    checksums_data: bytes | None = None
    torrent_data: bytes | None = None
    request_data: bytes | None = None
    total_size = 0
    for item in manifest.files:
        total_size += item.size
        if total_size > 512 * 1024 * 1024:
            raise UploadKitError("upload kit exceeds the aggregate size limit")
        if item.role is PackageFileRole.SCREENSHOT:
            maximum = _MAX_SCREENSHOT_BYTES
        elif item.role is PackageFileRole.TORRENT:
            maximum = 16 * 1024 * 1024
        else:
            maximum = _MAX_TEXT_BYTES
        if item.size > maximum:
            raise UploadKitError(
                f"upload-kit artifact exceeds its role limit: {item.path}"
            )
        data = _secure_read(
            root / Path(item.path),
            roots=(root,),
            maximum_bytes=max(maximum, 1),
        )
        if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
            raise UploadKitError(f"upload-kit artifact changed: {item.path}")
        if item.role is PackageFileRole.SCREENSHOT:
            suffix = Path(item.path).suffix
            if item.media_type != _SCREENSHOT_MEDIA[suffix]:
                raise UploadKitError(
                    "screenshot media type does not match its extension"
                )
            _validate_screenshot(data, suffix)
        elif item.media_type != _ROLE_MEDIA[item.role]:
            raise UploadKitError(
                f"artifact media type does not match its role: {item.path}"
            )

        if item.role in {
            PackageFileRole.MEDIAINFO,
            PackageFileRole.NFO,
            PackageFileRole.DESCRIPTION,
        }:
            try:
                decoded_text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise UploadKitError(f"release text is not UTF-8: {item.path}") from exc
            if (
                sanitize_release_text(
                    decoded_text,
                    payload_filename=f"{manifest.release_name}.mkv",
                ).encode("utf-8")
                != data
            ):
                raise UploadKitError(
                    f"release text is not canonically sanitized: {item.path}"
                )
        elif item.role is PackageFileRole.CHECKSUMS:
            checksums_data = data
        elif item.role is PackageFileRole.TORRENT:
            torrent_data = data
        elif item.role is PackageFileRole.UPLOAD_REQUEST:
            request_data = data
        if retain_artifacts:
            artifacts[item.path] = data

    if checksums_data is None or torrent_data is None or request_data is None:
        raise UploadKitError("upload kit is missing a required singleton artifact")
    expected_checksum_lines = [
        f"{item.sha256}  {item.path}"
        for item in sorted(manifest.files, key=lambda entry: entry.path)
        if item.role is not PackageFileRole.CHECKSUMS
    ]
    expected_checksums = ("\n".join(expected_checksum_lines) + "\n").encode("utf-8")
    if checksums_data != expected_checksums:
        raise UploadKitError("SHA256SUMS is not the canonical manifest checksum list")

    from .torrent import TorrentError, verify_torrent

    try:
        torrent = verify_torrent(
            torrent_data,
            expected_release_name=manifest.release_name,
            expected_infohash=manifest.torrent_infohash,
        )
    except TorrentError as exc:
        raise UploadKitError("upload-kit torrent failed verification") from exc
    if (
        torrent.file_size != manifest.payload_size
        or torrent.payload_path != manifest.payload_path
    ):
        raise UploadKitError("torrent payload does not match the package manifest")

    try:
        request = UploadRequest.model_validate_json(request_data)
    except ValueError as exc:
        raise UploadKitError("upload request is invalid") from exc
    if _json_bytes(request.model_dump(mode="json")) != request_data:
        raise UploadKitError("upload request is not canonically serialized")
    if (
        request.profile_id != manifest.profile_id
        or request.release_name != manifest.release_name
        or request.metadata_sha256 != manifest.metadata_sha256
        or request.torrent_infohash != manifest.torrent_infohash
        or request.requested_at != manifest.created_at
    ):
        raise UploadKitError("upload request does not match the package manifest")
    return manifest, artifacts


__all__ = [
    "UploadKitError",
    "UploadKitResult",
    "build_upload_kit",
    "load_verified_upload_kit",
    "sanitize_release_text",
    "verify_upload_kit",
]
