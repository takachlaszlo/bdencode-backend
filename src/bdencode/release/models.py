"""Strict, versioned domain models for release preparation.

The encoder pipeline does not persist tracker API credentials.  A private
announce URL may itself contain a tracker passkey, so tracker profiles and
generated torrents are protected artifacts and never public API metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from .torrent import TorrentProfile


_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_CREDENTIAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMDB_ID = re.compile(r"^tt[0-9]{7,10}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_TORRENT_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _nfc_text(value: str, *, label: str, max_length: int) -> str:
    if not value or len(value) > max_length:
        raise ValueError(f"{label} must contain 1..{max_length} characters")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{label} must use Unicode NFC normalization")
    if value != value.strip():
        raise ValueError(f"{label} may not have leading or trailing whitespace")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError(f"{label} may not contain control or surrogate characters")
    return value


def _serialized_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value


class ReleaseModel(BaseModel):
    """Base model that rejects silent forward/backward-schema coercion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ReleasePreparationState(StrEnum):
    NOT_PREPARED = "NOT_PREPARED"
    PREPARING = "PREPARING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY = "READY"
    SEEDING_CHECK = "SEEDING_CHECK"
    SEEDING = "SEEDING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class TrackerProfile(ReleaseModel):
    """Protected torrent policy, including private announce URLs.

    Dupe/publish API endpoints live in a separate host-allowlisted network
    configuration and their credentials are injected from systemd.
    """

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    torrent_source: str = Field(min_length=1, max_length=64)
    announce_urls: tuple[str, ...] = Field(min_length=1, max_length=8, repr=False)
    piece_size_min: int = Field(default=256 * 1024, ge=16 * 1024, le=16 * 1024 * 1024)
    piece_size_max: int = Field(default=16 * 1024 * 1024, ge=16 * 1024, le=32 * 1024 * 1024)
    piece_size_default: int = Field(default=1024 * 1024, ge=16 * 1024, le=32 * 1024 * 1024)
    target_piece_count_min: int = Field(default=1000, ge=1, le=65_536)
    target_piece_count_max: int = Field(default=2000, ge=1, le=65_536)
    screenshot_minimum: int = Field(default=3, ge=0, le=24)
    screenshot_maximum: int = Field(default=8, ge=1, le=24)
    credential_name: str = Field(min_length=1, max_length=128)

    @field_validator("announce_urls", mode="before")
    @classmethod
    def accept_json_announce_array(cls, value: Any) -> Any:
        # FastAPI presents JSON arrays to nested models as Python lists even
        # under strict validation.  Normalize the container explicitly while
        # retaining strict validation for every element.
        return tuple(value) if isinstance(value, list) else value

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise ValueError("profile_id must be a lowercase stable identifier")
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_text(cls, value: str, info: Any) -> str:
        return _nfc_text(value, label=info.field_name, max_length=128)

    @field_validator("torrent_source")
    @classmethod
    def validate_torrent_source(cls, value: str) -> str:
        if not _TORRENT_SOURCE.fullmatch(value):
            raise ValueError("torrent_source must be a safe ASCII tracker source token")
        return value

    @field_validator("credential_name")
    @classmethod
    def validate_credential_name(cls, value: str) -> str:
        if not _CREDENTIAL_NAME.fullmatch(value):
            raise ValueError("credential_name is not a safe system credential name")
        return value

    @field_validator("announce_urls")
    @classmethod
    def validate_announces(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        from urllib.parse import urlsplit

        seen: set[str] = set()
        for url in value:
            if not url.isascii() or any(char.isspace() for char in url):
                raise ValueError("announce URLs must be ASCII without whitespace")
            try:
                parsed = urlsplit(url)
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("announce URL is malformed") from exc
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("announce URLs must use HTTPS and include a host")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("announce URLs may not contain userinfo or fragments")
            normalized = url.casefold()
            if normalized in seen:
                raise ValueError("announce URLs must be unique")
            seen.add(normalized)
        return value

    @model_validator(mode="after")
    def validate_piece_policy(self) -> TrackerProfile:
        for value in (self.piece_size_min, self.piece_size_default, self.piece_size_max):
            if value & (value - 1):
                raise ValueError("piece lengths must be powers of two")
        if not self.piece_size_min <= self.piece_size_default <= self.piece_size_max:
            raise ValueError("default piece size must be within the piece-size bounds")
        if self.target_piece_count_min > self.target_piece_count_max:
            raise ValueError("target piece-count bounds are reversed")
        if self.screenshot_minimum > self.screenshot_maximum:
            raise ValueError("screenshot_minimum may not exceed screenshot_maximum")
        return self

    def torrent_profile(self) -> TorrentProfile:
        """Return the torrent builder's deliberately smaller policy view."""

        from .torrent import TorrentProfile

        return TorrentProfile(
            version=1,
            source=self.torrent_source,
            announce_url=self.announce_urls[0],
            piece_size_min=self.piece_size_min,
            piece_size_max=self.piece_size_max,
            piece_size_default=self.piece_size_default,
            target_piece_count_min=self.target_piece_count_min,
            target_piece_count_max=self.target_piece_count_max,
        )


class ReleaseMetadata(ReleaseModel):
    schema_version: Literal[1] = 1
    release_name: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=300)
    year: int = Field(ge=1878, le=2200)
    edition: str | None = Field(default=None, max_length=160)
    imdb_id: str | None = None
    tmdb_id: int | None = Field(default=None, ge=1)
    category: str = Field(min_length=1, max_length=64)
    source_media: str = Field(min_length=1, max_length=64)
    resolution: str = Field(min_length=1, max_length=32)
    video_codec: str = Field(min_length=1, max_length=32)
    audio_codecs: tuple[str, ...] = Field(min_length=1, max_length=32)
    languages: tuple[str, ...] = Field(min_length=1, max_length=32)

    @field_validator("audio_codecs", "languages", mode="before")
    @classmethod
    def accept_json_string_arrays(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("release_name")
    @classmethod
    def validate_release_name(cls, value: str) -> str:
        _nfc_text(value, label="release_name", max_length=240)
        if value in {".", ".."} or value != value.strip(" ."):
            raise ValueError("release_name may not be dot-like or have edge spaces/dots")
        if any(char in value for char in '/\\:*?"<>|'):
            raise ValueError("release_name contains a path-unsafe character")
        if value.casefold() in {"con", "prn", "aux", "nul"} or re.fullmatch(
            r"(?:com|lpt)[1-9]", value, flags=re.IGNORECASE
        ):
            raise ValueError("release_name is reserved on Windows")
        if len(f"{value}.torrent".encode("utf-8")) > 255:
            raise ValueError("release_name is too long for upload-kit filenames")
        # Keep metadata admission identical to the torrent payload policy so a
        # release cannot be accepted by the API only to fail later in build.
        from .torrent import TorrentSecurityError, validate_release_name

        try:
            validate_release_name(value)
        except TorrentSecurityError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator(
        "title", "edition", "category", "source_media", "resolution", "video_codec"
    )
    @classmethod
    def validate_text(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return None
        return _nfc_text(value, label=info.field_name, max_length=300)

    @field_validator("imdb_id")
    @classmethod
    def validate_imdb(cls, value: str | None) -> str | None:
        if value is not None and not _IMDB_ID.fullmatch(value):
            raise ValueError("imdb_id must have the form tt followed by 7-10 digits")
        return value

    @field_validator("languages")
    @classmethod
    def validate_languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _LANGUAGE.fullmatch(item) for item in value):
            raise ValueError("languages must be normalized BCP-47 language tags")
        if len({item.casefold() for item in value}) != len(value):
            raise ValueError("languages must be unique")
        return value

    @field_validator("audio_codecs")
    @classmethod
    def validate_audio_codecs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for item in value:
            normalized.append(_nfc_text(item, label="audio codec", max_length=32))
        if len({item.casefold() for item in normalized}) != len(normalized):
            raise ValueError("audio_codecs must be unique")
        return tuple(normalized)

    def canonical_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.model_dump(mode="json"))).hexdigest()


class PackageFileRole(StrEnum):
    TORRENT = "TORRENT"
    MEDIAINFO = "MEDIAINFO"
    NFO = "NFO"
    DESCRIPTION = "DESCRIPTION"
    SCREENSHOT = "SCREENSHOT"
    CHECKSUMS = "CHECKSUMS"
    UPLOAD_REQUEST = "UPLOAD_REQUEST"


class PackageFile(ReleaseModel):
    path: str = Field(min_length=1, max_length=512)
    size: int = Field(ge=0)
    sha256: str
    role: PackageFileRole
    media_type: str = Field(min_length=1, max_length=128)

    @field_validator("role", mode="before")
    @classmethod
    def accept_serialized_role(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return PackageFileRole(value)
            except ValueError:
                return value
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("package paths must be normalized relative POSIX paths")
        if value != unicodedata.normalize("NFC", value):
            raise ValueError("package paths must use Unicode NFC normalization")
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise ValueError("package paths may not contain control or format characters")
        return value

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        return value


class ReleasePackageManifest(ReleaseModel):
    schema_version: Literal[1] = 1
    release_name: str
    profile_id: str
    metadata_sha256: str
    torrent_infohash: str
    payload_path: str
    payload_size: int = Field(ge=1)
    payload_sha256: str
    files: tuple[PackageFile, ...] = Field(min_length=1, max_length=64)
    created_at: datetime

    @field_validator("files", mode="before")
    @classmethod
    def accept_json_file_array(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("created_at", mode="before")
    @classmethod
    def accept_serialized_created_at(cls, value: Any) -> Any:
        return _serialized_datetime(value)

    @field_validator("release_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        # Reuse all path-safety and Unicode rules without accepting partial data.
        return ReleaseMetadata.validate_release_name(value)

    @field_validator("profile_id")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise ValueError("profile_id must be a lowercase stable identifier")
        return value

    @field_validator("metadata_sha256", "payload_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("expected a lowercase SHA-256 digest")
        return value

    @field_validator("torrent_infohash")
    @classmethod
    def validate_infohash(cls, value: str) -> str:
        if not _HEX_40.fullmatch(value):
            raise ValueError("torrent_infohash must be a lowercase SHA-1 digest")
        return value

    @field_validator("payload_path")
    @classmethod
    def validate_payload_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or len(path.parts) != 2 or path.suffix.casefold() != ".mkv":
            raise ValueError("payload_path must be <release>/<release>.mkv")
        if path.parts[0] != path.parts[1][:-4]:
            raise ValueError("payload directory and MKV stem must match exactly")
        return value

    @model_validator(mode="after")
    def validate_files(self) -> ReleasePackageManifest:
        folded: set[str] = set()
        for item in self.files:
            key = unicodedata.normalize("NFC", item.path).casefold()
            if key in folded:
                raise ValueError("manifest paths must be Unicode/casefold unique")
            folded.add(key)
        expected_payload = f"{self.release_name}/{self.release_name}.mkv"
        if self.payload_path != expected_payload:
            raise ValueError("payload_path must be derived from release_name")
        singleton_paths = {
            PackageFileRole.TORRENT: f"{self.release_name}.torrent",
            PackageFileRole.MEDIAINFO: "mediainfo.txt",
            PackageFileRole.NFO: f"{self.release_name}.nfo",
            PackageFileRole.DESCRIPTION: "description.bbcode",
            PackageFileRole.CHECKSUMS: "SHA256SUMS",
            PackageFileRole.UPLOAD_REQUEST: "upload-request.json",
        }
        for role, expected_path in singleton_paths.items():
            matching = [item for item in self.files if item.role is role]
            if len(matching) != 1 or matching[0].path != expected_path:
                raise ValueError(f"manifest requires exactly one canonical {role.value} file")
        for item in self.files:
            if item.role is PackageFileRole.SCREENSHOT:
                if not re.fullmatch(
                    r"screenshots/[0-9]{2}\.(?:png|jpe?g|webp)",
                    item.path,
                ):
                    raise ValueError("screenshot manifest path is not canonical")
            elif item.path != singleton_paths[item.role]:
                raise ValueError("manifest role and path do not match")
        screenshot_paths = sorted(
            item.path for item in self.files if item.role is PackageFileRole.SCREENSHOT
        )
        if screenshot_paths:
            indices = [int(PurePosixPath(path).stem) for path in screenshot_paths]
            if indices != list(range(1, len(indices) + 1)):
                raise ValueError("screenshot manifest indices must be contiguous")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.model_dump(mode="json")) + b"\n"

    def canonical_digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class UploadRequest(ReleaseModel):
    schema_version: Literal[1] = 1
    profile_id: str
    release_name: str
    metadata_sha256: str
    torrent_infohash: str
    requested_at: datetime

    @field_validator("requested_at", mode="before")
    @classmethod
    def accept_serialized_requested_at(cls, value: Any) -> Any:
        return _serialized_datetime(value)

    @field_validator("profile_id")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if not _PROFILE_ID.fullmatch(value):
            raise ValueError("invalid profile_id")
        return value

    @field_validator("release_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return ReleaseMetadata.validate_release_name(value)

    @field_validator("metadata_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _HEX_64.fullmatch(value):
            raise ValueError("invalid metadata SHA-256")
        return value

    @field_validator("torrent_infohash")
    @classmethod
    def validate_infohash(cls, value: str) -> str:
        if not _HEX_40.fullmatch(value):
            raise ValueError("invalid torrent infohash")
        return value

    @model_validator(mode="after")
    def validate_timestamp(self) -> UploadRequest:
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("requested_at must be timezone-aware")
        return self


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "PackageFile",
    "PackageFileRole",
    "ReleaseMetadata",
    "ReleasePackageManifest",
    "ReleasePreparationState",
    "TrackerProfile",
    "UploadRequest",
    "utc_now",
]
