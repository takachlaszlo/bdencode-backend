from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import bdencode.release.torrent as torrent_module
from bdencode.release.torrent import (
    BencodeError,
    TorrentProfile,
    TorrentSecurityError,
    TorrentVerificationError,
    bdecode,
    bencode,
    build_private_torrent,
    payload_path_for,
    select_piece_size,
    verify_torrent,
)


RELEASE_NAME = "Example.Movie.2024.1080p.BluRay.x264-GROUP"


def _profile(**updates: object) -> TorrentProfile:
    values: dict[str, object] = {
        "source": "AITHER",
        "announce_url": "https://tracker.invalid/announce/very-secret-passkey",
        "piece_size_min": 16 * 1024,
        "piece_size_max": 16 * 1024,
        "piece_size_default": 16 * 1024,
        "target_piece_count_min": 1,
        "target_piece_count_max": 100,
    }
    values.update(updates)
    return TorrentProfile.model_validate(values)


def _sha1(value: bytes) -> bytes:
    try:
        return hashlib.sha1(value, usedforsecurity=False).digest()
    except TypeError:  # pragma: no cover - compatibility provider
        return hashlib.sha1(value).digest()


def test_canonical_bencode_sorts_raw_dictionary_keys_and_round_trips() -> None:
    encoded = bencode({b"z": 2, "a": [b"x", -3], b"m": {b"n": b""}})

    assert encoded == b"d1:al1:xi-3ee1:md1:n0:e1:zi2ee"
    assert bdecode(encoded) == {
        b"a": [b"x", -3],
        b"m": {b"n": b""},
        b"z": 2,
    }


@pytest.mark.parametrize(
    "value",
    [
        b"i03e",
        b"i-0e",
        b"i+1e",
        b"03:abc",
        b"d1:bi1e1:ai2ee",
        b"d1:ai1e1:ai2ee",
        b"li1e",
        b"i1ejunk",
        b"i99999999999999999999e",
        b"99999999999:x",
    ],
)
def test_bdecode_rejects_noncanonical_or_truncated_values(value: bytes) -> None:
    with pytest.raises(BencodeError):
        bdecode(value)


def test_bencode_rejects_ambiguous_and_recursive_values() -> None:
    with pytest.raises(BencodeError, match="booleans"):
        bencode(True)
    with pytest.raises(BencodeError, match="duplicate"):
        bencode({b"same": 1, "same": 2})
    recursive: list[object] = []
    recursive.append(recursive)
    with pytest.raises(BencodeError, match="circular"):
        bencode(recursive)
    with pytest.raises(BencodeError, match="64-bit"):
        bencode(2**63)


@pytest.mark.parametrize(
    "updates",
    [
        {"version": 2},
        {"source": "AITHER/unsafe"},
        {"announce_url": "ftp://tracker.invalid/announce"},
        {"announce_url": "https://user:secret@tracker.invalid/announce"},
        {"piece_size_min": 24 * 1024},
        {"piece_size_min": 32 * 1024, "piece_size_max": 16 * 1024},
        {"target_piece_count_min": 3, "target_piece_count_max": 2},
        {"unknown": True},
    ],
)
def test_torrent_profile_is_strict_and_rejects_unsafe_policy(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _profile(**updates)


def test_torrent_profile_does_not_repr_tracker_credential() -> None:
    profile = _profile()

    assert "very-secret-passkey" not in repr(profile)


def test_piece_size_selection_stays_power_of_two_and_targets_piece_range() -> None:
    profile = _profile(
        piece_size_min=16 * 1024,
        piece_size_max=1024 * 1024,
        piece_size_default=64 * 1024,
        target_piece_count_min=2,
        target_piece_count_max=4,
    )

    assert select_piece_size(10 * 64 * 1024, profile) == 256 * 1024
    assert select_piece_size(16 * 1024, profile) == 16 * 1024
    with pytest.raises(ValueError, match="positive"):
        select_piece_size(0, profile)


def test_piece_size_selection_fails_if_profile_maximum_cannot_fit_payload() -> None:
    profile = _profile(
        piece_size_min=16 * 1024,
        piece_size_max=16 * 1024,
        piece_size_default=16 * 1024,
        target_piece_count_min=1,
        target_piece_count_max=2,
    )

    with pytest.raises(ValueError, match="maximum piece count"):
        select_piece_size(3 * 16 * 1024, profile)


def test_builder_is_deterministic_private_and_has_one_virtual_payload(
    tmp_path: Path,
) -> None:
    source = tmp_path / "local-secret-input.mkv"
    source_bytes = bytes(range(256)) * 200
    source.write_bytes(source_bytes)
    first_path = tmp_path / "first.torrent"
    second_path = tmp_path / "second.torrent"
    profile = _profile()

    first = build_private_torrent(
        source,
        first_path,
        release_name=RELEASE_NAME,
        profile=profile,
    )
    second = build_private_torrent(
        source,
        second_path,
        release_name=RELEASE_NAME,
        profile=profile.model_dump(),
    )

    assert first.torrent_bytes == second.torrent_bytes
    assert first.infohash == second.infohash
    assert first.sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert first.file_sha256 == first.sha256
    assert first.piece_length == 16 * 1024
    assert first.piece_count == 4
    assert first.payload_path == f"{RELEASE_NAME}/{RELEASE_NAME}.mkv"
    assert first_path.read_bytes() == first.torrent_bytes
    assert b"local-secret-input" not in first.torrent_bytes
    assert b"created by" not in first.torrent_bytes
    assert b"creation date" not in first.torrent_bytes

    root = bdecode(first.torrent_bytes)
    assert isinstance(root, dict)
    info = root[b"info"]
    assert isinstance(info, dict)
    assert info[b"private"] == 1
    assert info[b"source"] == b"AITHER"
    assert info[b"files"] == [
        {
            b"length": len(source_bytes),
            b"path": [f"{RELEASE_NAME}.mkv".encode()],
        }
    ]
    expected_pieces = b"".join(
        _sha1(source_bytes[offset : offset + 16 * 1024])
        for offset in range(0, len(source_bytes), 16 * 1024)
    )
    assert info[b"pieces"] == expected_pieces
    assert first.infohash == hashlib.sha1(  # noqa: S324 - BitTorrent v1 protocol
        bencode(info)
    ).hexdigest()


def test_verify_rehashes_payload_and_proves_sha256(tmp_path: Path) -> None:
    source = tmp_path / "encode.mkv"
    source.write_bytes(os.urandom(40_000))
    profile = _profile()
    result = build_private_torrent(
        source,
        tmp_path / "release.torrent",
        release_name=RELEASE_NAME,
        profile=profile,
    )

    proof = verify_torrent(
        result.torrent_path,
        expected_release_name=RELEASE_NAME,
        expected_file_sha256=result.sha256.upper(),
        payload_file=source,
        expected_profile=profile,
        expected_infohash=result.infohash.upper(),
    )

    assert proof.infohash == result.infohash
    assert proof.sha256 == result.sha256
    assert proof.torrent_sha256 == result.torrent_sha256
    assert proof.payload_path == result.payload_path


def test_verify_rejects_tampered_piece_hash(tmp_path: Path) -> None:
    source = tmp_path / "encode.mkv"
    source.write_bytes(b"content" * 7000)
    result = build_private_torrent(
        source,
        tmp_path / "release.torrent",
        release_name=RELEASE_NAME,
        profile=_profile(),
    )
    root = bdecode(result.torrent_bytes)
    assert isinstance(root, dict)
    info = root[b"info"]
    assert isinstance(info, dict)
    pieces = info[b"pieces"]
    assert isinstance(pieces, bytes)
    info[b"pieces"] = bytes([pieces[0] ^ 1]) + pieces[1:]

    with pytest.raises(TorrentVerificationError, match="piece hashes"):
        verify_torrent(bencode(root), payload_file=source)


def test_verify_never_accepts_unproven_expected_sha256(tmp_path: Path) -> None:
    source = tmp_path / "encode.mkv"
    source.write_bytes(b"payload")
    result = build_private_torrent(
        source,
        tmp_path / "release.torrent",
        release_name=RELEASE_NAME,
        profile=_profile(),
    )

    with pytest.raises(TorrentVerificationError, match="payload_file is required"):
        verify_torrent(
            result.torrent_bytes,
            expected_file_sha256=result.sha256,
        )


def test_verify_rejects_extra_metadata_and_wrong_payload_shape(tmp_path: Path) -> None:
    source = tmp_path / "encode.mkv"
    source.write_bytes(b"payload")
    result = build_private_torrent(
        source,
        tmp_path / "release.torrent",
        release_name=RELEASE_NAME,
        profile=_profile(),
    )
    root = bdecode(result.torrent_bytes)
    assert isinstance(root, dict)
    root[b"comment"] = b"leak"
    with pytest.raises(TorrentVerificationError, match="keys are not canonical"):
        verify_torrent(bencode(root))

    root = bdecode(result.torrent_bytes)
    info = root[b"info"]  # type: ignore[index]
    assert isinstance(info, dict)
    files = info[b"files"]
    assert isinstance(files, list)
    files.append(dict(files[0]))
    with pytest.raises(TorrentVerificationError, match="exactly one"):
        verify_torrent(bencode(root))


@pytest.mark.parametrize(
    "release_name",
    [
        "../escape",
        "Folder/Release",
        "Folder\\Release",
        "Cafe\u0301.2024",
        "Release.mkv",
        "CON.Title.2024",
        "Trailing.",
        "Unsafe:Name",
        "emoji.\U0001f4bf",
    ],
)
def test_release_payload_path_rejects_traversal_unicode_and_aliases(
    release_name: str,
) -> None:
    with pytest.raises(TorrentSecurityError):
        payload_path_for(release_name)


def test_release_payload_path_accepts_canonical_unicode_letters() -> None:
    name = "Árvíztűrő.Tükörfúrógép.2024"

    assert payload_path_for(name) == f"{name}/{name}.mkv"


def test_builder_requires_absolute_mkv_and_torrent_paths(tmp_path: Path) -> None:
    source = tmp_path / "encode.mkv"
    source.write_bytes(b"payload")
    with pytest.raises(TorrentSecurityError, match="absolute"):
        build_private_torrent(
            Path("relative.mkv"),
            tmp_path / "out.torrent",
            release_name=RELEASE_NAME,
            profile=_profile(),
        )
    with pytest.raises(TorrentSecurityError, match=".torrent suffix"):
        build_private_torrent(
            source,
            tmp_path / "out.bin",
            release_name=RELEASE_NAME,
            profile=_profile(),
        )


def test_builder_rejects_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"payload")
    linked = tmp_path / "linked.mkv"
    try:
        linked.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(TorrentSecurityError, match="symbolic link"):
        build_private_torrent(
            linked,
            tmp_path / "out.torrent",
            release_name=RELEASE_NAME,
            profile=_profile(),
        )


def test_builder_rejects_destination_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"payload")
    external = tmp_path / "external.txt"
    external.write_text("keep", encoding="utf-8")
    destination = tmp_path / "release.torrent"
    try:
        destination.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(TorrentSecurityError, match="symbolic link"):
        build_private_torrent(
            source,
            destination,
            release_name=RELEASE_NAME,
            profile=_profile(),
        )
    assert external.read_text(encoding="utf-8") == "keep"


def test_builder_rejects_casefold_colliding_destination_name(tmp_path: Path) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"payload")
    alias = tmp_path / "Release.TORRENT"
    alias.write_bytes(b"keep")

    with pytest.raises(TorrentSecurityError, match="casefold"):
        build_private_torrent(
            source,
            tmp_path / "release.torrent",
            release_name=RELEASE_NAME,
            profile=_profile(),
        )
    assert alias.read_bytes() == b"keep"


def test_builder_detects_payload_change_after_streaming_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mkv"
    source.write_bytes(b"a" * 40_000)
    original = torrent_module._hash_payload

    def mutate_after_hash(path: Path, piece_length: int) -> Any:
        result = original(path, piece_length)
        source.write_bytes(b"b" * 40_000)
        current = source.stat()
        os.utime(
            source,
            ns=(current.st_atime_ns, current.st_mtime_ns + 2_000_000_000),
        )
        return result

    monkeypatch.setattr(torrent_module, "_hash_payload", mutate_after_hash)
    with pytest.raises(TorrentSecurityError, match="changed"):
        build_private_torrent(
            source,
            tmp_path / "release.torrent",
            release_name=RELEASE_NAME,
            profile=_profile(),
        )
    assert not (tmp_path / "release.torrent").exists()


def test_directory_guard_allows_own_temporary_child_creation(tmp_path: Path) -> None:
    guard = torrent_module._snapshot_chain(
        tmp_path,
        description="torrent output directory",
        leaf_kind="directory",
    )

    (tmp_path / ".release.torrent.partial").write_bytes(b"temporary")

    torrent_module._assert_chain_unchanged(
        guard,
        description="torrent output directory",
    )
