from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

import bdencode.release.package as package_module
from bdencode.maintenance import MaintenanceSafetyError
from bdencode.release.models import (
    PackageFile,
    PackageFileRole,
    ReleaseMetadata,
    ReleasePackageManifest,
    TrackerProfile,
)
from bdencode.release.package import (
    UploadKitError,
    build_upload_kit,
    sanitize_release_text,
    verify_upload_kit,
)
from bdencode.release.torrent import build_private_torrent


_PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"


def _profile(**overrides: object) -> TrackerProfile:
    values: dict[str, object] = {
        "profile_id": "tracker",
        "display_name": "Tracker",
        "torrent_source": "TRACKER",
        "announce_urls": ("https://tracker.invalid/announce",),
        "screenshot_minimum": 1,
        "screenshot_maximum": 3,
        "credential_name": "tracker-token",
    }
    values.update(overrides)
    return TrackerProfile(**values)


def _metadata() -> ReleaseMetadata:
    return ReleaseMetadata(
        release_name="Example.2026.1080p.BluRay.x264-GROUP",
        title="Example",
        year=2026,
        category="Movie",
        source_media="BluRay",
        resolution="1080p",
        video_codec="x264",
        audio_codecs=("FLAC",),
        languages=("en",),
    )


def _inputs(tmp_path: Path) -> tuple[TrackerProfile, ReleaseMetadata, Path, Path, Path]:
    profile = _profile()
    metadata = _metadata()
    payload = tmp_path / f"{metadata.release_name}.mkv"
    payload.write_bytes((b"matroska-payload" * 4096) + b"tail")
    torrent = tmp_path / "release.torrent"
    build_private_torrent(
        payload,
        torrent,
        release_name=metadata.release_name,
        profile=profile.torrent_profile(),
    )
    screenshot_root = tmp_path / "evidence"
    screenshot_root.mkdir()
    screenshot = screenshot_root / "frame.png"
    screenshot.write_bytes(_PNG)
    return profile, metadata, payload, torrent, screenshot


def _rebind_artifact(
    directory: Path,
    manifest: ReleasePackageManifest,
    relative_path: str,
    data: bytes,
) -> str:
    (directory / Path(relative_path)).write_bytes(data)
    files = tuple(
        item.model_copy(
            update={
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        if item.path == relative_path
        else item
        for item in manifest.files
    )
    updated = manifest.model_copy(update={"files": files})
    manifest_bytes = updated.canonical_bytes()
    (directory / "package-manifest.json").write_bytes(manifest_bytes)
    return hashlib.sha256(manifest_bytes).hexdigest()


def test_sanitizer_removes_secret_fields_and_private_paths() -> None:
    text = (
        "Complete name : C:\\private\\work\\source.mkv\r\n"
        "Source path = /srv/encoder/work/source.mkv\r\n"
        "API key = do-not-leak\r\n"
        "Authorization: Bearer do-not-leak\r\n"
        "Diagnostic 123e4567-e89b-12d3-a456-426614174000 at /home/operator/job\r\n"
        "Title : Safe title\x1b[31m\r\n"
    )

    sanitized = sanitize_release_text(text, payload_filename="Public.Release.mkv")

    assert "do-not-leak" not in sanitized
    assert "C:\\private" not in sanitized
    assert "Complete name : Public.Release.mkv" in sanitized
    assert "Source path : Public.Release.mkv" in sanitized
    assert "/home/operator" not in sanitized
    assert "123e4567" not in sanitized
    assert "Title : Safe title" in sanitized
    assert "\x1b" not in sanitized
    assert sanitized.endswith("\n")


def test_build_upload_kit_is_atomic_allowlisted_and_self_verifying(
    tmp_path: Path,
) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    output = tmp_path / "upload-kit"
    created_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    result = build_upload_kit(
        output,
        profile=profile,
        metadata=metadata,
        torrent_path=torrent,
        payload_path=payload,
        mediainfo="Complete name : C:\\private\\movie.mkv\nFormat : Matroska",
        nfo="Title: Example\nPassword: secret",
        description_bbcode="[b]Example[/b]\nToken = secret",
        screenshots=(screenshot,),
        screenshot_roots=(screenshot.parent,),
        created_at=created_at,
    )

    assert result.directory == output
    assert (
        result.manifest_sha256
        == hashlib.sha256(result.manifest_path.read_bytes()).hexdigest()
    )
    manifest = verify_upload_kit(
        output,
        expected_manifest_sha256=result.manifest_sha256,
    )
    assert manifest == result.manifest
    assert (
        manifest.payload_path == f"{metadata.release_name}/{metadata.release_name}.mkv"
    )
    assert manifest.payload_size == payload.stat().st_size
    assert manifest.payload_sha256 == hashlib.sha256(payload.read_bytes()).hexdigest()
    assert {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    } == {
        f"{metadata.release_name}.nfo",
        f"{metadata.release_name}.torrent",
        "mediainfo.txt",
        "description.bbcode",
        "screenshots/01.png",
        "SHA256SUMS",
        "upload-request.json",
        "package-manifest.json",
    }
    assert "private" not in (output / "mediainfo.txt").read_text(encoding="utf-8")
    assert "secret" not in (output / f"{metadata.release_name}.nfo").read_text(
        encoding="utf-8"
    )
    checksums = (output / "SHA256SUMS").read_text(encoding="utf-8")
    assert "screenshots/01.png" in checksums
    assert "package-manifest.json" not in checksums


def test_build_upload_kit_refuses_existing_destination(tmp_path: Path) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    output = tmp_path / "upload-kit"
    output.mkdir()

    with pytest.raises(FileExistsError):
        build_upload_kit(
            output,
            profile=profile,
            metadata=metadata,
            torrent_path=torrent,
            payload_path=payload,
            mediainfo="MediaInfo",
            nfo="NFO",
            description_bbcode="Description",
            screenshots=(screenshot,),
            screenshot_roots=(screenshot.parent,),
        )


def test_failed_build_leaves_no_visible_partial_kit(tmp_path: Path) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    output = tmp_path / "upload-kit"

    with pytest.raises(UploadKitError, match="count"):
        build_upload_kit(
            output,
            profile=_profile(screenshot_minimum=2),
            metadata=metadata,
            torrent_path=torrent,
            payload_path=payload,
            mediainfo="MediaInfo",
            nfo="NFO",
            description_bbcode="Description",
            screenshots=(screenshot,),
            screenshot_roots=(screenshot.parent,),
        )

    assert not output.exists()
    assert not list(tmp_path.glob(".upload-kit.tmp-*"))


def test_stage_mutation_cannot_be_legitimized_by_manifest_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    output = tmp_path / "upload-kit"
    original_write = package_module._write_file

    def mutate_after_write(path: Path, data: bytes, *, mode: int = 0o640) -> None:
        original_write(path, data, mode=mode)
        if path.suffix == ".nfo":
            path.write_bytes(b"attacker replacement\n")

    monkeypatch.setattr(package_module, "_write_file", mutate_after_write)

    with pytest.raises(UploadKitError, match="changed"):
        build_upload_kit(
            output,
            profile=profile,
            metadata=metadata,
            torrent_path=torrent,
            payload_path=payload,
            mediainfo="MediaInfo",
            nfo="NFO",
            description_bbcode="Description",
            screenshots=(screenshot,),
            screenshot_roots=(screenshot.parent,),
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".upload-kit.tmp-*"))


def test_unsafe_upload_stage_is_retained_without_recursive_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    output = tmp_path / "upload-kit"
    original_write = package_module._write_file
    rmtree_calls: list[Path] = []

    def mutate_after_write(path: Path, data: bytes, *, mode: int = 0o640) -> None:
        original_write(path, data, mode=mode)
        if path.suffix == ".nfo":
            path.write_bytes(b"attacker replacement\n")

    def reject_unsafe_tree(path: Path) -> tuple[int, int]:
        raise MaintenanceSafetyError(f"unsafe mounted stage: {path.name}")

    def record_rmtree(path: Path) -> None:
        rmtree_calls.append(Path(path))

    monkeypatch.setattr(package_module, "_write_file", mutate_after_write)
    monkeypatch.setattr(package_module, "safe_tree_usage", reject_unsafe_tree)
    monkeypatch.setattr(package_module.shutil, "rmtree", record_rmtree)

    with pytest.raises(MaintenanceSafetyError, match="unsafe mounted stage"):
        build_upload_kit(
            output,
            profile=profile,
            metadata=metadata,
            torrent_path=torrent,
            payload_path=payload,
            mediainfo="MediaInfo",
            nfo="NFO",
            description_bbcode="Description",
            screenshots=(screenshot,),
            screenshot_roots=(screenshot.parent,),
        )

    assert not output.exists()
    assert len(list(tmp_path.glob(".upload-kit.tmp-*"))) == 1
    assert rmtree_calls == []


def test_upload_kit_verifier_detects_tamper_and_unmanifested_file(
    tmp_path: Path,
) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    output = tmp_path / "upload-kit"
    result = build_upload_kit(
        output,
        profile=profile,
        metadata=metadata,
        torrent_path=torrent,
        payload_path=payload,
        mediainfo="MediaInfo",
        nfo="NFO",
        description_bbcode="Description",
        screenshots=(screenshot,),
        screenshot_roots=(screenshot.parent,),
    )
    (output / "description.bbcode").write_text("changed", encoding="utf-8")

    with pytest.raises(UploadKitError, match="changed"):
        verify_upload_kit(output, expected_manifest_sha256=result.manifest_sha256)

    (output / "description.bbcode").write_text("Description\n", encoding="utf-8")
    (output / "private-report.json").write_text("{}", encoding="utf-8")
    with pytest.raises(UploadKitError, match="unmanifested"):
        verify_upload_kit(output, expected_manifest_sha256=result.manifest_sha256)


def test_screenshot_must_be_beneath_allowlisted_root(tmp_path: Path) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    other = tmp_path / "different-root"
    other.mkdir()

    with pytest.raises(UploadKitError, match="escapes"):
        build_upload_kit(
            tmp_path / "upload-kit",
            profile=profile,
            metadata=metadata,
            torrent_path=torrent,
            payload_path=payload,
            mediainfo="MediaInfo",
            nfo="NFO",
            description_bbcode="Description",
            screenshots=(screenshot,),
            screenshot_roots=(other,),
        )


def test_screenshot_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are unavailable")
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    linked = screenshot.parent / "linked.png"
    try:
        linked.symlink_to(screenshot)
    except OSError:
        pytest.skip("creating symlinks requires an unavailable privilege")

    with pytest.raises(UploadKitError, match="linked|reparse"):
        build_upload_kit(
            tmp_path / "upload-kit",
            profile=profile,
            metadata=metadata,
            torrent_path=torrent,
            payload_path=payload,
            mediainfo="MediaInfo",
            nfo="NFO",
            description_bbcode="Description",
            screenshots=(linked,),
            screenshot_roots=(screenshot.parent,),
        )


def test_casefold_colliding_screenshot_names_are_rejected(tmp_path: Path) -> None:
    profile, metadata, payload, torrent, _screenshot = _inputs(tmp_path)
    first_root = tmp_path / "one"
    second_root = tmp_path / "two"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / "Frame.PNG"
    second = second_root / "frame.png"
    first.write_bytes(_PNG)
    second.write_bytes(_PNG)

    with pytest.raises(UploadKitError, match="casefold"):
        build_upload_kit(
            tmp_path / "upload-kit",
            profile=_profile(screenshot_maximum=3),
            metadata=metadata,
            torrent_path=torrent,
            payload_path=payload,
            mediainfo="MediaInfo",
            nfo="NFO",
            description_bbcode="Description",
            screenshots=(first, second),
            screenshot_roots=(first_root, second_root),
        )


def test_verifier_rejects_rebound_but_noncanonical_checksums(tmp_path: Path) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    result = build_upload_kit(
        tmp_path / "upload-kit",
        profile=profile,
        metadata=metadata,
        torrent_path=torrent,
        payload_path=payload,
        mediainfo="MediaInfo",
        nfo="NFO",
        description_bbcode="Description",
        screenshots=(screenshot,),
        screenshot_roots=(screenshot.parent,),
    )
    digest = _rebind_artifact(
        result.directory,
        result.manifest,
        "SHA256SUMS",
        b"0" * 64 + b"  made-up.txt\n",
    )

    with pytest.raises(UploadKitError, match="canonical manifest checksum"):
        verify_upload_kit(result.directory, expected_manifest_sha256=digest)


def test_verifier_rejects_rebound_unsanitized_release_text(tmp_path: Path) -> None:
    profile, metadata, payload, torrent, screenshot = _inputs(tmp_path)
    result = build_upload_kit(
        tmp_path / "upload-kit",
        profile=profile,
        metadata=metadata,
        torrent_path=torrent,
        payload_path=payload,
        mediainfo="MediaInfo",
        nfo="NFO",
        description_bbcode="Description",
        screenshots=(screenshot,),
        screenshot_roots=(screenshot.parent,),
    )
    digest = _rebind_artifact(
        result.directory,
        result.manifest,
        f"{metadata.release_name}.nfo",
        b"NFO\nAPI key: should-not-be-publishable\n",
    )

    with pytest.raises(UploadKitError, match="canonically sanitized"):
        verify_upload_kit(result.directory, expected_manifest_sha256=digest)


def test_manifest_requires_all_singletons_and_binds_payload_to_release() -> None:
    metadata = _metadata()
    with pytest.raises(ValueError, match="requires exactly one"):
        ReleasePackageManifest(
            release_name=metadata.release_name,
            profile_id="tracker",
            metadata_sha256=metadata.canonical_digest(),
            torrent_infohash="a" * 40,
            payload_path=f"{metadata.release_name}/{metadata.release_name}.mkv",
            payload_size=1,
            payload_sha256="b" * 64,
            files=(
                PackageFile(
                    path="mediainfo.txt",
                    size=1,
                    sha256="c" * 64,
                    role=PackageFileRole.MEDIAINFO,
                    media_type="text/plain; charset=utf-8",
                ),
            ),
            created_at=datetime.now(UTC),
        )
