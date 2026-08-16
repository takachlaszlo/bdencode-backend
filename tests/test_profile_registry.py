from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdencode.config import ConfigurationError
from bdencode.release_profiles import (
    RELEASE_PROFILE_VALIDATION_ERROR,
    load_release_profiles,
)


def _profile() -> dict[str, object]:
    return {
        "tracker": {
            "schema_version": 1,
            "profile_id": "example",
            "display_name": "Example",
            "torrent_source": "EXAMPLE",
            "announce_urls": ["https://tracker.example/announce"],
            "credential_name": "tracker-example-api-token",
        },
        "network": {
            "allowed_hosts": ["tracker.example"],
            "dupe_check_endpoint": "https://tracker.example/api/dupe",
            "publish_endpoint": "https://tracker.example/api/upload",
        },
        "qbittorrent": {
            "base_url": "http://127.0.0.1:8080",
            "allowed_hosts": ["127.0.0.1"],
            "username_credential": "qbittorrent-username",
            "password_credential": "qbittorrent-password",
        },
    }


def test_missing_profile_document_is_an_empty_registry(tmp_path: Path) -> None:
    assert load_release_profiles(tmp_path / "missing.json").profiles == ()


def test_profile_public_view_never_exposes_endpoints_or_credentials(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps({"schema_version": 1, "profiles": [_profile()]}),
        encoding="utf-8",
    )

    profile = load_release_profiles(path).get("example")
    public = profile.public_dict()

    assert public["supports_publish"] is True
    assert public["supports_qbittorrent"] is True
    serialized = json.dumps(public)
    assert "api/upload" not in serialized
    assert "credential" not in serialized
    assert "announce" not in serialized


def test_endpoint_host_must_be_explicitly_allowlisted(tmp_path: Path) -> None:
    document = _profile()
    document["network"]["publish_endpoint"] = "https://evil.example/api/upload"  # type: ignore[index]
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps({"schema_version": 1, "profiles": [document]}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_release_profiles(path)

    assert captured.value.code == RELEASE_PROFILE_VALIDATION_ERROR
    assert "evil.example" not in str(captured.value)


def test_duplicate_profile_ids_are_rejected(tmp_path: Path) -> None:
    profile = _profile()
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps({"schema_version": 1, "profiles": [profile, profile]}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_release_profiles(path)

    assert captured.value.code == RELEASE_PROFILE_VALIDATION_ERROR


def test_invalid_announce_value_stays_only_in_private_validation_diagnostic(
    tmp_path: Path,
) -> None:
    profile = _profile()
    rejected_url = "http://tracker.example/sekrit"
    profile["tracker"]["announce_urls"] = [rejected_url]  # type: ignore[index]
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps({"schema_version": 1, "profiles": [profile]}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as captured:
        load_release_profiles(path)

    assert captured.value.code == RELEASE_PROFILE_VALIDATION_ERROR
    assert "sekrit" not in str(captured.value)
    assert captured.value.__cause__ is not None
    assert "sekrit" in str(captured.value.__cause__)
