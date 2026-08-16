"""Strict, root-protected configuration for tracker release integrations.

Announce URLs may contain a private tracker passkey and are never returned by
the public profile API.  API credentials remain separate systemd credentials.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import ConfigurationError
from .release.models import TrackerProfile


_CREDENTIAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HOST = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
RELEASE_PROFILE_VALIDATION_ERROR = "release_profile_configuration_invalid"


def _normalized_host(value: str) -> str:
    candidate = value.rstrip(".").casefold()
    try:
        return ipaddress.ip_address(candidate).compressed.casefold()
    except ValueError:
        if not _HOST.fullmatch(candidate):
            raise ValueError("invalid host") from None
        return candidate


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TrackerNetworkConfig(_StrictModel):
    """Operator-owned endpoints; never returned through the public API."""

    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=8)
    dupe_check_endpoint: str | None = Field(default=None, max_length=2048)
    publish_endpoint: str | None = Field(default=None, max_length=2048)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            normalized = tuple(_normalized_host(item) for item in value)
        except ValueError as exc:
            raise ValueError("allowed_hosts contains an invalid host") from exc
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_hosts must be unique")
        return normalized

    @field_validator("dupe_check_endpoint", "publish_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("tracker endpoints must be fixed credential-free HTTPS URLs")
        return value

    @model_validator(mode="after")
    def require_allowed_endpoint_hosts(self) -> "TrackerNetworkConfig":
        for endpoint in (self.dupe_check_endpoint, self.publish_endpoint):
            if endpoint is None:
                continue
            host = _normalized_host(urlsplit(endpoint).hostname or "")
            if host not in self.allowed_hosts:
                raise ValueError("tracker endpoint host is not allowlisted")
        return self


class QBitTorrentConfig(_StrictModel):
    base_url: str = Field(max_length=2048)
    allowed_hosts: tuple[str, ...] = Field(min_length=1, max_length=8)
    username_credential: str = Field(default="qbittorrent-username", max_length=128)
    password_credential: str = Field(default="qbittorrent-password", max_length=128)

    @field_validator("username_credential", "password_credential")
    @classmethod
    def validate_credential(cls, value: str) -> str:
        if not _CREDENTIAL_NAME.fullmatch(value):
            raise ValueError("qBittorrent credential name is invalid")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        try:
            normalized = tuple(_normalized_host(item) for item in value)
        except ValueError as exc:
            raise ValueError(
                "qBittorrent allowed_hosts contains an invalid host"
            ) from exc
        if len(set(normalized)) != len(normalized):
            raise ValueError("qBittorrent allowed_hosts must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_base_url(self) -> "QBitTorrentConfig":
        parsed = urlsplit(self.base_url)
        host = _normalized_host(parsed.hostname or "")
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or host not in self.allowed_hosts
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("qBittorrent base_url violates its host/URL policy")
        if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("plain HTTP qBittorrent is allowed only on loopback")
        return self


class ConfiguredReleaseProfile(_StrictModel):
    tracker: TrackerProfile
    network: TrackerNetworkConfig | None = None
    qbittorrent: QBitTorrentConfig | None = None

    def canonical_digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def public_dict(self) -> dict[str, object]:
        tracker = self.tracker
        return {
            "profile_id": tracker.profile_id,
            "display_name": tracker.display_name,
            "torrent_source": tracker.torrent_source,
            "piece_size_min": tracker.piece_size_min,
            "piece_size_max": tracker.piece_size_max,
            "piece_size_default": tracker.piece_size_default,
            "target_piece_count_min": tracker.target_piece_count_min,
            "target_piece_count_max": tracker.target_piece_count_max,
            "screenshot_minimum": tracker.screenshot_minimum,
            "screenshot_maximum": tracker.screenshot_maximum,
            "supports_dupe_check": bool(
                self.network and self.network.dupe_check_endpoint
            ),
            "supports_publish": bool(self.network and self.network.publish_endpoint),
            "supports_qbittorrent": self.qbittorrent is not None,
            "profile_digest": self.canonical_digest(),
        }


class ReleaseProfilesDocument(_StrictModel):
    schema_version: Literal[1] = 1
    profiles: tuple[ConfiguredReleaseProfile, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def unique_profiles(self) -> "ReleaseProfilesDocument":
        identifiers = [item.tracker.profile_id for item in self.profiles]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("release profile identifiers must be unique")
        return self

    def get(self, profile_id: str) -> ConfiguredReleaseProfile:
        for profile in self.profiles:
            if profile.tracker.profile_id == profile_id:
                return profile
        raise ConfigurationError(f"release profile not found: {profile_id}")


def _is_reparse(path: Path) -> bool:
    try:
        information = path.lstat()
    except FileNotFoundError:
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(
        getattr(information, "st_file_attributes", 0) & flag
    )


def load_release_profiles(path: Path) -> ReleaseProfilesDocument:
    """Load an exact, bounded profile document without following links."""

    target = Path(path)
    if not os.path.lexists(target):
        return ReleaseProfilesDocument(profiles=())
    if _is_reparse(target) or not target.is_file():
        raise ConfigurationError("release profiles must be a regular non-link file")
    before = target.stat()
    if before.st_size > 1024 * 1024:
        raise ConfigurationError("release profiles file exceeds 1 MiB")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ConfigurationError("release profiles could not be read") from exc
    after = target.stat()
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ConfigurationError("release profiles changed while being read")
    try:
        return ReleaseProfilesDocument.model_validate_json(raw)
    except ValueError as exc:
        # Keep Pydantic's full diagnostic in the private exception chain for
        # operators, but tag it so the API can return a bounded classification.
        # Validation errors include rejected input values (potential tracker
        # passkeys, credential names, and URL paths) and must never be echoed.
        raise ConfigurationError(
            "release profile configuration is invalid",
            code=RELEASE_PROFILE_VALIDATION_ERROR,
        ) from exc


__all__ = [
    "ConfiguredReleaseProfile",
    "QBitTorrentConfig",
    "RELEASE_PROFILE_VALIDATION_ERROR",
    "ReleaseProfilesDocument",
    "TrackerNetworkConfig",
    "load_release_profiles",
]
