from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from bdencode.release.adapters import (
    AdapterConfigurationError,
    AdapterError,
    DupeCheckOutcome,
    DupeCheckReceipt,
    HttpDupeChecker,
    HttpTrackerPublisher,
    PublicationApproval,
    PublicationOutcome,
    QBitTorrentClient,
    QBitTorrentOutcome,
)
from bdencode.release.models import (
    ReleaseMetadata,
    ReleasePackageManifest,
    TrackerProfile,
)
from bdencode.release.package import build_upload_kit
from bdencode.release.torrent import TorrentProfile, build_private_torrent


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


def _kit(tmp_path: Path) -> tuple[Path, ReleasePackageManifest, str]:
    metadata = _metadata()
    profile = TrackerProfile(
        profile_id="tracker",
        display_name="Tracker",
        torrent_source="TRACKER",
        announce_urls=("https://tracker.invalid/announce",),
        screenshot_minimum=1,
        screenshot_maximum=1,
        credential_name="tracker-token",
    )
    payload = tmp_path / f"{metadata.release_name}.mkv"
    payload.write_bytes(b"payload" * 100)
    torrent = tmp_path / "release.torrent"
    build_private_torrent(
        payload,
        torrent,
        release_name=metadata.release_name,
        profile=profile.torrent_profile(),
    )
    screenshot_root = tmp_path / "screenshots"
    screenshot_root.mkdir()
    screenshot = screenshot_root / "frame.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    result = build_upload_kit(
        tmp_path / "kit",
        profile=profile,
        metadata=metadata,
        torrent_path=torrent,
        payload_path=payload,
        mediainfo="Format : Matroska",
        nfo="Title : Example",
        description_bbcode="[b]Example[/b]",
        screenshots=(screenshot,),
        screenshot_roots=(screenshot_root,),
    )
    return result.directory, result.manifest, result.manifest_sha256


@pytest.mark.parametrize(
    ("url", "hosts"),
    [
        ("http://tracker.invalid/api", ("tracker.invalid",)),
        ("https://attacker.invalid/api", ("tracker.invalid",)),
        ("https://user:secret@tracker.invalid/api", ("tracker.invalid",)),
        ("https://tracker.invalid/api?next=https://attacker.invalid", ("tracker.invalid",)),
    ],
)
def test_network_adapters_reject_unsafe_endpoints(
    url: str, hosts: tuple[str, ...]
) -> None:
    with pytest.raises(AdapterConfigurationError):
        HttpDupeChecker(
            url,
            allowed_hosts=hosts,
            credential_name="tracker-token",
        )


def test_qbittorrent_allows_explicit_loopback_http_only(tmp_path: Path) -> None:
    client = QBitTorrentClient(
        "http://127.0.0.1:8080",
        allowed_hosts=("127.0.0.1",),
        username_credential="qbit-user",
        password_credential="qbit-password",
    )
    assert "127.0.0.1" in client._base_url

    with pytest.raises(AdapterConfigurationError):
        QBitTorrentClient(
            "http://192.0.2.1:8080",
            allowed_hosts=("192.0.2.1",),
            username_credential="qbit-user",
            password_credential="qbit-password",
        )


def test_dupe_checker_returns_strict_clear_receipt() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer top-secret"
        return httpx.Response(
            200,
            json={"status": "clear", "matches": [], "request_id": "req-1"},
        )

    checker = HttpDupeChecker(
        "https://tracker.invalid/api/dupe",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=lambda _name: "top-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = checker.check(
        _metadata(),
        profile_id="tracker",
        manifest_sha256="a" * 64,
    )

    assert receipt.outcome is DupeCheckOutcome.CLEAR
    assert receipt.remote_request_id == "req-1"
    assert DupeCheckReceipt.model_validate(receipt.model_dump(mode="json")) == receipt
    assert len(requests) == 1
    assert "top-secret" not in repr(checker.__dict__)


def test_dupe_timeout_is_unknown_and_never_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("ambiguous", request=request)

    checker = HttpDupeChecker(
        "https://tracker.invalid/api/dupe",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = checker.check(
        _metadata(),
        profile_id="tracker",
        manifest_sha256="a" * 64,
    )

    assert receipt.outcome is DupeCheckOutcome.UNKNOWN
    assert attempts == 1


def test_dupe_checker_treats_unrecognized_success_as_unknown() -> None:
    checker = HttpDupeChecker(
        "https://tracker.invalid/api/dupe",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"status": "maybe"})
            )
        ),
    )

    receipt = checker.check(
        _metadata(), profile_id="tracker", manifest_sha256="a" * 64
    )

    assert receipt.outcome is DupeCheckOutcome.UNKNOWN


def test_dupe_checker_does_not_surface_credential_loader_detail() -> None:
    def credential_loader(_name: str) -> str:
        raise RuntimeError("do-not-leak-this-secret")

    checker = HttpDupeChecker(
        "https://tracker.invalid/api/dupe",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=credential_loader,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("network must not be reached")
            )
        ),
    )

    with pytest.raises(AdapterError) as captured:
        checker.check(
            _metadata(), profile_id="tracker", manifest_sha256="a" * 64
        )
    assert "do-not-leak" not in str(captured.value)
    assert captured.value.__suppress_context__ is True


def test_dupe_checker_rejects_control_characters_in_remote_evidence() -> None:
    checker = HttpDupeChecker(
        "https://tracker.invalid/api/dupe",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"status": "duplicate", "matches": ["bad\nlog-entry"]},
                )
            )
        ),
    )

    receipt = checker.check(
        _metadata(), profile_id="tracker", manifest_sha256="a" * 64
    )

    assert receipt.outcome is DupeCheckOutcome.UNKNOWN
    assert receipt.matches == ()


def _publication_guards(
    manifest: ReleasePackageManifest, manifest_digest: str
) -> tuple[PublicationApproval, DupeCheckReceipt]:
    now = datetime.now(UTC)
    approval = PublicationApproval(
        profile_id="tracker",
        manifest_sha256=manifest_digest,
        approved_by="operator",
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
    )
    dupe = DupeCheckReceipt(
        profile_id="tracker",
        manifest_sha256=manifest_digest,
        metadata_sha256=manifest.metadata_sha256,
        outcome=DupeCheckOutcome.CLEAR,
        checked_at=now,
    )
    return approval, dupe


def test_publisher_revalidates_approval_manifest_and_dupe_receipt(tmp_path: Path) -> None:
    kit, manifest, digest = _kit(tmp_path)
    approval, dupe = _publication_guards(manifest, digest)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer top-secret"
        assert digest.encode() in request.content
        return httpx.Response(
            200,
            json={
                "status": "published",
                "id": "123",
                "url": "https://tracker.invalid/torrents/123",
            },
        )

    publisher = HttpTrackerPublisher(
        "https://tracker.invalid/api/upload",
        profile_id="tracker",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=lambda _name: "top-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = publisher.publish(kit, approval=approval, dupe_receipt=dupe)

    assert receipt.outcome is PublicationOutcome.PUBLISHED
    assert receipt.remote_id == "123"
    assert len(requests) == 1
    assert "top-secret" not in repr(publisher.__dict__)


def test_publisher_refuses_tamper_before_network(tmp_path: Path) -> None:
    kit, manifest, digest = _kit(tmp_path)
    approval, dupe = _publication_guards(manifest, digest)
    (kit / "upload-request.json").write_text("tampered", encoding="utf-8")
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200)

    publisher = HttpTrackerPublisher(
        "https://tracker.invalid/api/upload",
        profile_id="tracker",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(AdapterError, match="revalidation"):
        publisher.publish(kit, approval=approval, dupe_receipt=dupe)
    assert attempts == 0


def test_publisher_timeout_is_unknown_and_never_retried(tmp_path: Path) -> None:
    kit, manifest, digest = _kit(tmp_path)
    approval, dupe = _publication_guards(manifest, digest)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("ambiguous", request=request)

    publisher = HttpTrackerPublisher(
        "https://tracker.invalid/api/upload",
        profile_id="tracker",
        allowed_hosts=("tracker.invalid",),
        credential_name="tracker-token",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = publisher.publish(kit, approval=approval, dupe_receipt=dupe)

    assert receipt.outcome is PublicationOutcome.UNKNOWN
    assert attempts == 1


def test_qbittorrent_adds_stopped_then_requests_full_recheck(tmp_path: Path) -> None:
    release_name = "Example.2026.1080p.BluRay.x264-GROUP"
    payload = tmp_path / f"{release_name}.mkv"
    payload.write_bytes(b"mkv payload" * 100)
    torrent_path = tmp_path / "release.torrent"
    built = build_private_torrent(
        payload,
        torrent_path,
        release_name=release_name,
        profile=TorrentProfile(
            source="TEST",
            announce_url="https://tracker.invalid/announce",
        ),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/add"):
            assert b'name="paused"' in request.content
            assert b"true" in request.content
            assert b'name="savepath"' in request.content
            assert b'name="category"' in request.content
            assert b"bdencode" in request.content
            return httpx.Response(200, text="Ok.")
        assert request.url.path.endswith("/torrents/recheck")
        assert built.infohash.encode() in request.content
        return httpx.Response(200, text="")

    adapter = QBitTorrentClient(
        "http://127.0.0.1:8080",
        allowed_hosts=("127.0.0.1",),
        username_credential="qbit-user",
        password_credential="qbit-password",
        credential_loader=lambda name: "user" if name == "qbit-user" else "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = adapter.add_paused_and_recheck(
        torrent_path,
        expected_infohash=built.infohash,
        save_path=tmp_path,
        category="bdencode",
    )

    assert receipt.outcome is QBitTorrentOutcome.ADDED_AND_RECHECKING
    assert receipt.full_recheck_requested is True
    assert calls == [
        "/api/v2/auth/login",
        "/api/v2/torrents/add",
        "/api/v2/torrents/recheck",
    ]


def test_qbittorrent_rejects_unbound_torrent_bytes_before_network(
    tmp_path: Path,
) -> None:
    release_name = "Example.2026.1080p.BluRay.x264-GROUP"
    payload = tmp_path / f"{release_name}.mkv"
    payload.write_bytes(b"mkv payload" * 100)
    torrent_path = tmp_path / "release.torrent"
    built = build_private_torrent(
        payload,
        torrent_path,
        release_name=release_name,
        profile=TorrentProfile(
            source="TEST",
            announce_url="https://tracker.invalid/announce",
        ),
    )
    mismatched_infohash = (
        ("f" if built.infohash[0] != "f" else "e") + built.infohash[1:]
    )
    adapter = QBitTorrentClient(
        "http://127.0.0.1:8080",
        allowed_hosts=("127.0.0.1",),
        username_credential="qbit-user",
        password_credential="qbit-password",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("network must not be reached")
            )
        ),
    )

    with pytest.raises(AdapterError, match="expected identity"):
        adapter.add_paused_and_recheck(
            torrent_path.read_bytes(),
            torrent_name=torrent_path.name,
            expected_infohash=mismatched_infohash,
        )


def test_qbittorrent_rejects_relative_save_path_before_network(tmp_path: Path) -> None:
    payload = tmp_path / "Release.mkv"
    payload.write_bytes(b"payload")
    torrent_path = tmp_path / "release.torrent"
    built = build_private_torrent(
        payload,
        torrent_path,
        release_name="Release",
        profile=TorrentProfile(
            source="TEST",
            announce_url="https://tracker.invalid/announce",
        ),
    )
    adapter = QBitTorrentClient(
        "http://localhost:8080",
        allowed_hosts=("localhost",),
        username_credential="qbit-user",
        password_credential="qbit-password",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("network must not be reached")
            )
        ),
    )

    with pytest.raises(AdapterError, match="absolute"):
        adapter.add_paused_and_recheck(
            torrent_path,
            expected_infohash=built.infohash,
            save_path=Path("relative"),
        )


def test_qbittorrent_does_not_recheck_or_retry_after_ambiguous_add(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "Release.mkv"
    payload.write_bytes(b"payload")
    torrent_path = tmp_path / "release.torrent"
    built = build_private_torrent(
        payload,
        torrent_path,
        release_name="Release",
        profile=TorrentProfile(
            source="TEST",
            announce_url="https://tracker.invalid/announce",
        ),
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        raise httpx.ReadTimeout("ambiguous", request=request)

    adapter = QBitTorrentClient(
        "http://localhost:8080",
        allowed_hosts=("localhost",),
        username_credential="qbit-user",
        password_credential="qbit-password",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = adapter.add_paused_and_recheck(
        torrent_path,
        expected_infohash=built.infohash,
    )

    assert receipt.outcome is QBitTorrentOutcome.UNKNOWN
    assert receipt.added_paused is None
    assert receipt.full_recheck_requested is False
    assert calls == 2


def test_qbittorrent_preserves_known_add_after_ambiguous_recheck(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "Release.mkv"
    payload.write_bytes(b"payload")
    torrent_path = tmp_path / "release.torrent"
    built = build_private_torrent(
        payload,
        torrent_path,
        release_name="Release",
        profile=TorrentProfile(
            source="TEST",
            announce_url="https://tracker.invalid/announce",
        ),
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/add"):
            return httpx.Response(200, text="Ok.")
        raise httpx.ReadTimeout("ambiguous", request=request)

    adapter = QBitTorrentClient(
        "http://localhost:8080",
        allowed_hosts=("localhost",),
        username_credential="qbit-user",
        password_credential="qbit-password",
        credential_loader=lambda _name: "secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    receipt = adapter.add_paused_and_recheck(
        torrent_path,
        expected_infohash=built.infohash,
    )

    assert receipt.outcome is QBitTorrentOutcome.UNKNOWN
    assert receipt.added_paused is True
    assert receipt.full_recheck_requested is None
    assert calls == 3
