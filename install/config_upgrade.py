#!/usr/bin/env python3
"""Atomic, formatting-preserving upgrades for the host TOML configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tomllib


BDENCODE_HEADER = re.compile(
    r"^[ \t]*\[[ \t]*bdencode[ \t]*\][ \t]*(?:#.*)?(?:\r?\n)?$"
)


class ConfigUpgradeError(RuntimeError):
    """The installed configuration cannot be upgraded without ambiguity."""


def fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_regular_file(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ConfigUpgradeError("configuration must be a regular file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            payload = stream.read()
    finally:
        os.close(descriptor)
    return payload, details


def upgraded_payload(payload: bytes, release_profiles_path: str) -> bytes:
    try:
        text = payload.decode("utf-8")
        document = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigUpgradeError("installed configuration is invalid") from error
    section = document.get("bdencode")
    if not isinstance(section, dict):
        raise ConfigUpgradeError("installed configuration has no [bdencode] table")
    if "release_profiles_path" in section:
        return payload

    lines = text.splitlines(keepends=True)
    headers = [
        index for index, line in enumerate(lines) if BDENCODE_HEADER.fullmatch(line)
    ]
    if len(headers) != 1:
        raise ConfigUpgradeError(
            "installed configuration has an ambiguous [bdencode] table"
        )
    newline = "\r\n" if lines[headers[0]].endswith("\r\n") else "\n"
    assignment = f"release_profiles_path = {json.dumps(release_profiles_path)}{newline}"
    lines.insert(headers[0] + 1, assignment)
    candidate = "".join(lines).encode("utf-8")
    parsed = tomllib.loads(candidate.decode("utf-8"))
    if parsed["bdencode"].get("release_profiles_path") != release_profiles_path:
        raise ConfigUpgradeError("configuration upgrade verification failed")
    return candidate


def upgrade(path: Path, release_profiles_path: str) -> bool:
    payload, details = read_regular_file(path)
    candidate = upgraded_payload(payload, release_profiles_path)
    if candidate == payload:
        return False

    temporary = (
        path.parent / f".{path.name}.upgrade-{os.getpid()}-{secrets.token_hex(8)}"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, stat.S_IMODE(details.st_mode))
    try:
        if hasattr(os, "fchown"):
            os.fchown(descriptor, details.st_uid, details.st_gid)
        os.fchmod(descriptor, stat.S_IMODE(details.st_mode))
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--release-profiles-path",
        default="/etc/bdencode/release-profiles.json",
    )
    args = parser.parse_args()
    try:
        upgrade(args.config, args.release_profiles_path)
    except (OSError, ConfigUpgradeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
