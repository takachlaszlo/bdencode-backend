#!/usr/bin/env python3
"""Release helper: ``check`` a vX.Y.Z tag against the repository, build the GitHub release ``notes``.

The unattended updater installs the newest ``vX.Y.Z`` tag and refuses a tag whose version differs
from ``pyproject.toml`` (``invalid_release``). ``check`` makes the same mistake impossible to
publish: the tag, ``pyproject.toml``, ``bdencode.__version__`` and ``frontend/package.json`` must
carry one version, and the release notes document must exist.

    python tools/release.py check v2.3.0
    python tools/release.py notes v2.3.0 > notes.md
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
REPOSITORY_URL = "https://github.com/takachlaszlo/bdencode-backend"
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


class ReleaseError(ValueError):
    pass


def parse_tag(tag: str) -> tuple[int, int, int]:
    match = TAG_RE.match(tag)
    if match is None:
        raise ReleaseError(f"{tag!r} is not a stable vMAJOR.MINOR.PATCH tag")
    return int(match[1]), int(match[2]), int(match[3])


def notes_path(tag: str, root: Path = ROOT) -> Path:
    major, minor, patch = parse_tag(tag)
    name = f"RELEASE_{major}_{minor}.md" if patch == 0 else f"RELEASE_{major}_{minor}_{patch}.md"
    return root / "docs" / name


def declared_versions(root: Path = ROOT) -> dict[str, str]:
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)["project"]["version"]
    init = (root / "src" / "bdencode" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
    package = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))["version"]
    return {
        "pyproject.toml": pyproject,
        "src/bdencode/__init__.py": match[1] if match else "<missing>",
        "frontend/package.json": package,
    }


def check(tag: str, root: Path = ROOT) -> list[str]:
    """Return the problems that would make the tag an invalid release (empty = fine)."""

    parse_tag(tag)
    version = tag[1:]
    problems = [
        f"{name} declares {value!r}, the tag says {version!r}"
        for name, value in declared_versions(root).items()
        if value != version
    ]
    notes = notes_path(tag, root)
    if not notes.is_file():
        problems.append(f"release notes {notes.relative_to(root).as_posix()} are missing")
    return problems


def notes(tag: str, root: Path = ROOT) -> str:
    """The release notes document with every relative link made absolute for the tag."""

    text = notes_path(tag, root).read_text(encoding="utf-8")

    def absolute(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if target.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)
        path = target[3:] if target.startswith("../") else f"docs/{target}"
        return f"[{label}]({REPOSITORY_URL}/blob/{tag}/{path})"

    return LINK_RE.sub(absolute, text)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"check", "notes"}:
        print(__doc__, file=sys.stderr)
        return 2
    command, tag = argv[1], argv[2]
    try:
        if command == "check":
            problems = check(tag)
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            if not problems:
                print(f"{tag}: version files and release notes agree")
            return 1 if problems else 0
        sys.stdout.buffer.write(notes(tag).encode("utf-8"))
        return 0
    except (ReleaseError, OSError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
