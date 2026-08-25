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
    r"^[ \t]*\[[ \t]*(?:bdencode|\"bdencode\"|'bdencode')[ \t]*\]"
    r"[ \t]*(?:#.*)?(?:\r?\n)?$"
)
TOML_KEY_PART = r'(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|\'[^\']*\')'
TOML_TABLE_HEADER = re.compile(
    rf"^[ \t]*\[{{1,2}}[ \t]*{TOML_KEY_PART}"
    rf"(?:[ \t]*\.[ \t]*{TOML_KEY_PART})*[ \t]*\]{{1,2}}"
    r"[ \t]*(?:#.*)?(?:\r?\n)?$"
)
TOML_INTEGER = (
    r"[+-]?(?:0|[1-9](?:_?[0-9])*)"
    r"|0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*"
    r"|0o[0-7](?:_?[0-7])*"
    r"|0b[01](?:_?[01])*"
)
COMPARISON_PAIR_COUNT_ASSIGNMENT = re.compile(
    rf"^(?P<prefix>[ \t]*(?:comparison_pair_count|\"comparison_pair_count\"|"
    rf"'comparison_pair_count')[ \t]*=[ \t]*)(?P<value>{TOML_INTEGER})"
    r"(?P<suffix>[ \t]*(?:#[^\r\n]*)?)(?P<newline>\r?\n)?$"
)

COMPARISON_PAIR_COUNT_MIN = 20
COMPARISON_PAIR_COUNT_MAX = 50
COMPARISON_PAIR_COUNT_DEFAULT = 24


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


def replace_legacy_comparison_pair_count(lines: list[str], header_index: int) -> None:
    table_end = next(
        (
            index
            for index in range(header_index + 1, len(lines))
            if TOML_TABLE_HEADER.fullmatch(lines[index])
        ),
        len(lines),
    )
    assignments = [
        (index, match)
        for index in range(header_index + 1, table_end)
        if (match := COMPARISON_PAIR_COUNT_ASSIGNMENT.fullmatch(lines[index]))
        is not None
    ]
    if len(assignments) != 1:
        raise ConfigUpgradeError(
            "installed configuration has an ambiguous comparison_pair_count"
        )
    index, match = assignments[0]
    lines[index] = (
        f"{match.group('prefix')}{COMPARISON_PAIR_COUNT_DEFAULT}"
        f"{match.group('suffix')}{match.group('newline') or ''}"
    )


def insert_release_profiles_path(
    lines: list[str], header_index: int, release_profiles_path: str
) -> None:
    header = lines[header_index]
    if header.endswith("\r\n"):
        newline = "\r\n"
    elif header.endswith("\n"):
        newline = "\n"
    else:
        newline = next(
            (
                "\r\n" if line.endswith("\r\n") else "\n"
                for line in lines
                if line.endswith(("\r\n", "\n"))
            ),
            "\n",
        )
        lines[header_index] = f"{header}{newline}"
        newline = ""
    assignment = f"release_profiles_path = {json.dumps(release_profiles_path)}{newline}"
    lines.insert(header_index + 1, assignment)


def upgraded_payload(payload: bytes, release_profiles_path: str) -> bytes:
    try:
        text = payload.decode("utf-8")
        document = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigUpgradeError("installed configuration is invalid") from error
    section = document.get("bdencode")
    if not isinstance(section, dict):
        raise ConfigUpgradeError("installed configuration has no [bdencode] table")
    needs_release_profiles_path = "release_profiles_path" not in section
    comparison_pair_count = section.get("comparison_pair_count")
    needs_comparison_pair_count = type(comparison_pair_count) is int and not (
        COMPARISON_PAIR_COUNT_MIN <= comparison_pair_count <= COMPARISON_PAIR_COUNT_MAX
    )
    if not needs_release_profiles_path and not needs_comparison_pair_count:
        return payload

    lines = text.splitlines(keepends=True)
    headers = [
        index for index, line in enumerate(lines) if BDENCODE_HEADER.fullmatch(line)
    ]
    if len(headers) != 1:
        raise ConfigUpgradeError(
            "installed configuration has an ambiguous [bdencode] table"
        )
    if needs_comparison_pair_count:
        replace_legacy_comparison_pair_count(lines, headers[0])
    if needs_release_profiles_path:
        insert_release_profiles_path(lines, headers[0], release_profiles_path)
    candidate = "".join(lines).encode("utf-8")
    try:
        parsed = tomllib.loads(candidate.decode("utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigUpgradeError("configuration upgrade verification failed") from error
    upgraded_section = parsed.get("bdencode")
    if not isinstance(upgraded_section, dict):
        raise ConfigUpgradeError("configuration upgrade verification failed")
    if (
        needs_release_profiles_path
        and upgraded_section.get("release_profiles_path") != release_profiles_path
    ):
        raise ConfigUpgradeError("configuration upgrade verification failed")
    if (
        needs_comparison_pair_count
        and upgraded_section.get("comparison_pair_count")
        != COMPARISON_PAIR_COUNT_DEFAULT
    ):
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
