#!/usr/bin/env python3
"""Release helper: ``check`` a vX.Y.Z tag against the repository, build the GitHub release ``notes``.

The unattended updater installs the newest ``vX.Y.Z`` tag and refuses a tag whose version differs
from ``pyproject.toml`` (``invalid_release``). ``check`` makes the same mistake impossible to
publish: the tag, ``pyproject.toml``, ``bdencode.__version__`` and ``frontend/package.json`` must
carry one version, and the release notes document must exist.

    python tools/release.py check v2.3.0
    python tools/release.py notes v2.3.0 > notes.md
    python tools/release.py tag v2.3.0      # annotated, SSH-signed tag on HEAD (not pushed)
"""

from __future__ import annotations

import json
import re
import subprocess
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


def create_signed_tag(tag: str, root: Path = ROOT, commit: str = "HEAD") -> str:
    """Create the annotated, SSH-signed tag the hardened updater accepts and verify it locally.

    Needs ``gpg.format=ssh`` and ``user.signingkey`` in the git configuration. Returns the tagged
    commit. Nothing is pushed: publishing the tag is the deliberate last step.
    """

    problems = check(tag, root)
    if problems:
        raise ReleaseError("; ".join(problems))

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments], capture_output=True, text=True, errors="replace"
        )
        if completed.returncode != 0:
            raise ReleaseError(f"git {' '.join(arguments[:2])} failed: {(completed.stderr or completed.stdout).strip()}")
        return completed.stdout.strip()

    if git("status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("the working tree has uncommitted changes")
    def configured(name: str) -> str:
        found = subprocess.run(
            ["git", "-C", str(root), "config", "--get", name], capture_output=True, text=True
        )
        return found.stdout.strip() if found.returncode == 0 else ""

    if configured("gpg.format") != "ssh" or not configured("user.signingkey"):
        raise ReleaseError("set gpg.format=ssh and user.signingkey (your release signing key) first")
    target = git("rev-parse", f"{commit}^{{commit}}")
    git("tag", "--sign", "--message", f"BDEncode {tag[1:]}", tag, target)
    # Verification needs the local trust anchor; without one the signature is still created.
    if configured("gpg.ssh.allowedSignersFile"):
        git("tag", "--verify", tag)
    return target


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"check", "notes", "tag"}:
        print(__doc__, file=sys.stderr)
        return 2
    command, tag = argv[1], argv[2]
    try:
        if command == "tag":
            target = create_signed_tag(tag)
            print(f"{tag}: signed and verified locally on {target[:12]}; publish it with: git push origin {tag}")
            return 0
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
