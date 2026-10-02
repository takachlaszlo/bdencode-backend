from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("bdencode_release_tool", ROOT / "tools" / "release.py")
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


def make_repo(tmp_path: Path, version: str = "2.3.1", notes: str | None = "RELEASE_2_3_1.md") -> Path:
    (tmp_path / "src" / "bdencode").mkdir(parents=True)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8")
    (tmp_path / "src" / "bdencode" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (tmp_path / "frontend" / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    if notes:
        (tmp_path / "docs" / notes).write_text(
            "# Notes\n\nSee [guide](../README.md#x), [api](API.md), [old](RELEASE_2_2.md#a), "
            "[abs](https://example.org/a) and [here](#local).\n",
            encoding="utf-8",
        )
    return tmp_path


def test_tags_must_be_stable_semver() -> None:
    assert release.parse_tag("v2.10.0") == (2, 10, 0)
    for bad in ("2.3.0", "v2.3", "v2.3.0-rc1", "v2.3.0.1", "main", ""):
        with pytest.raises(release.ReleaseError):
            release.parse_tag(bad)


def test_notes_file_name_follows_the_existing_convention() -> None:
    assert release.notes_path("v2.2.0", Path("r")).as_posix() == "r/docs/RELEASE_2_2.md"
    assert release.notes_path("v2.2.1", Path("r")).as_posix() == "r/docs/RELEASE_2_2_1.md"


def test_a_consistent_release_passes(tmp_path: Path) -> None:
    assert release.check("v2.3.1", make_repo(tmp_path)) == []


def test_every_disagreement_is_reported(tmp_path: Path) -> None:
    root = make_repo(tmp_path, version="2.3.0", notes=None)
    problems = release.check("v2.3.1", root)
    assert len(problems) == 4
    assert any("pyproject.toml declares '2.3.0'" in item for item in problems)
    assert any("__init__.py" in item for item in problems) and any("package.json" in item for item in problems)
    assert any("docs/RELEASE_2_3_1.md are missing" in item for item in problems)


def test_notes_use_absolute_links_for_the_tag(tmp_path: Path) -> None:
    text = release.notes("v2.3.1", make_repo(tmp_path))
    base = "https://github.com/takachlaszlo/bdencode-backend/blob/v2.3.1/"
    assert f"[guide]({base}README.md#x)" in text
    assert f"[api]({base}docs/API.md)" in text and f"[old]({base}docs/RELEASE_2_2.md#a)" in text
    assert "[abs](https://example.org/a)" in text and "[here](#local)" in text


def test_the_repository_itself_is_consistent_for_its_current_version() -> None:
    version = release.declared_versions()["pyproject.toml"]
    major, minor, patch = (int(part) for part in version.split("."))
    tag = f"v{version}"
    assert release.check(tag) == [], "bump the three version files together and add the release notes"
    assert release.notes(tag)


def test_command_line_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main(["release.py"]) == 2
    assert release.main(["release.py", "check", "v9.9.9"]) == 1
    assert "error:" in capsys.readouterr().err
    assert release.main(["release.py", "check", "latest"]) == 1


# -- signed tags -----------------------------------------------------------------------------------------


def git_in(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *arguments],
        capture_output=True, text=True, check=True,
    )
    return completed.stdout.strip()


def committed_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.invalid")
    root = make_repo(tmp_path / "repo")
    git_in(root, "init", "--quiet", "--initial-branch=main")
    git_in(root, "add", "-A")
    git_in(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "release")
    return root


needs_signing_tools = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("ssh-keygen") is None, reason="needs git and ssh-keygen"
)


@needs_signing_tools
def test_tagging_refuses_an_inconsistent_release_or_a_dirty_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = committed_repo(tmp_path, monkeypatch)
    with pytest.raises(release.ReleaseError, match="declares"):
        release.create_signed_tag("v2.4.0", root)
    (root / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text(encoding="utf-8") + "# edit\n", encoding="utf-8"
    )
    with pytest.raises(release.ReleaseError, match="uncommitted"):
        release.create_signed_tag("v2.3.1", root)


@needs_signing_tools
def test_tagging_needs_an_ssh_signing_key_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = committed_repo(tmp_path, monkeypatch)
    with pytest.raises(release.ReleaseError, match="signing key"):
        release.create_signed_tag("v2.3.1", root)


@needs_signing_tools
def test_a_signed_annotated_tag_is_created_and_verifiable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = committed_repo(tmp_path, monkeypatch)
    key = tmp_path / "release-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "release", "-f", str(key)], check=True)
    signers = tmp_path / "signers"
    signers.write_text(f'release namespaces="git" {(tmp_path / "release-key.pub").read_text(encoding="utf-8")}', encoding="utf-8")
    git_in(root, "config", "gpg.format", "ssh")
    git_in(root, "config", "user.signingkey", str(key))
    git_in(root, "config", "gpg.ssh.allowedSignersFile", str(signers))

    commit = release.create_signed_tag("v2.3.1", root)

    assert commit == git_in(root, "rev-parse", "HEAD")
    assert git_in(root, "cat-file", "-t", "v2.3.1") == "tag"
    git_in(root, "tag", "--verify", "v2.3.1")
