"""Network boundaries for seeding, duplicate checks and tracker publication.

Every adapter performs at most one state-changing request.  A timeout after
that request is represented as ``UNKNOWN`` and is never retried automatically.
Credentials are loaded immediately before use and are never included in a
receipt, exception message, URL or log record.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import Field, field_validator, model_validator

from bdencode.secrets import read_secret

from .models import ReleaseMetadata, ReleaseModel
from .package import UploadKitError, load_verified_upload_kit


_HEX_40 = "0123456789abcdef"
_HEX_64 = "0123456789abcdef"
_CREDENTIAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROFILE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_MAX_TORRENT_BYTES = 16 * 1024 * 1024
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\."
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


class AdapterConfigurationError(ValueError):
    pass


class AdapterError(RuntimeError):
    pass


class CredentialLoader(Protocol):
    def __call__(self, name: str) -> str: ...


class QBitTorrentOutcome(StrEnum):
    ADDED_AND_RECHECKING = "ADDED_AND_RECHECKING"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class QBitTorrentReceipt(ReleaseModel):
    schema_version: Literal[1] = 1
    outcome: QBitTorrentOutcome
    infohash: str
    added_paused: bool | None = None
    full_recheck_requested: bool | None
    recorded_at: datetime

    @field_validator("outcome", mode="before")
    @classmethod
    def accept_serialized_outcome(cls, value: Any) -> Any:
        return _serialized_enum(value, QBitTorrentOutcome)

    @field_validator("recorded_at", mode="before")
    @classmethod
    def accept_serialized_recorded_at(cls, value: Any) -> Any:
        return _serialized_datetime(value)

    @field_validator("infohash")
    @classmethod
    def validate_infohash(cls, value: str) -> str:
        if len(value) != 40 or any(char not in _HEX_40 for char in value):
            raise ValueError("invalid v1 infohash")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> QBitTorrentReceipt:
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        if self.outcome is QBitTorrentOutcome.ADDED_AND_RECHECKING and not self.full_recheck_requested:
            raise ValueError("successful receipt requires a recheck request")
        if self.outcome is QBitTorrentOutcome.ADDED_AND_RECHECKING and self.added_paused is not True:
            raise ValueError("successful receipt must confirm a paused add")
        if self.outcome is QBitTorrentOutcome.REJECTED and self.added_paused is not False:
            raise ValueError("rejected receipt may not claim a paused add")
        if self.outcome is QBitTorrentOutcome.REJECTED and self.full_recheck_requested is not False:
            raise ValueError("rejected receipt may not claim a recheck request")
        if self.outcome is QBitTorrentOutcome.UNKNOWN:
            if self.added_paused is False:
                raise ValueError("unknown receipt cannot claim an explicit add rejection")
            if self.added_paused is None and self.full_recheck_requested is not False:
                raise ValueError("an ambiguous add cannot have reached the recheck stage")
            if self.added_paused is True and self.full_recheck_requested is not None:
                raise ValueError("an ambiguous recheck must remain tri-state")
        return self


class DupeCheckOutcome(StrEnum):
    CLEAR = "CLEAR"
    DUPLICATE = "DUPLICATE"
    UNKNOWN = "UNKNOWN"


class DupeCheckReceipt(ReleaseModel):
    schema_version: Literal[1] = 1
    profile_id: str
    manifest_sha256: str
    metadata_sha256: str
    outcome: DupeCheckOutcome
    matches: tuple[str, ...] = Field(default=(), max_length=100)
    checked_at: datetime
    remote_request_id: str | None = Field(default=None, max_length=256)

    @field_validator("outcome", mode="before")
    @classmethod
    def accept_serialized_outcome(cls, value: Any) -> Any:
        return _serialized_enum(value, DupeCheckOutcome)

    @field_validator("checked_at", mode="before")
    @classmethod
    def accept_serialized_checked_at(cls, value: Any) -> Any:
        return _serialized_datetime(value)

    @field_validator("matches", mode="before")
    @classmethod
    def accept_json_match_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        return _validate_profile_id(value)

    @field_validator("matches", "remote_request_id")
    @classmethod
    def validate_remote_text(
        cls, value: tuple[str, ...] | str | None
    ) -> tuple[str, ...] | str | None:
        if isinstance(value, tuple):
            for item in value:
                _validate_remote_text(item)
        elif value is not None:
            _validate_remote_text(value)
        return value

    @field_validator("manifest_sha256", "metadata_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in _HEX_64 for char in value):
            raise ValueError("invalid SHA-256 digest")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> DupeCheckReceipt:
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        if self.outcome is DupeCheckOutcome.CLEAR and self.matches:
            raise ValueError("a CLEAR dupe receipt cannot contain matches")
        if self.outcome is DupeCheckOutcome.DUPLICATE and not self.matches:
            raise ValueError("a DUPLICATE receipt must contain at least one match")
        return self


class PublicationApproval(ReleaseModel):
    schema_version: Literal[1] = 1
    profile_id: str
    manifest_sha256: str
    approved_by: str = Field(min_length=1, max_length=255)
    approved_at: datetime
    expires_at: datetime

    @field_validator("approved_at", "expires_at", mode="before")
    @classmethod
    def accept_serialized_timestamps(cls, value: Any) -> Any:
        return _serialized_datetime(value)

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        return _validate_profile_id(value)

    @field_validator("approved_by")
    @classmethod
    def validate_approver(cls, value: str) -> str:
        return _validate_remote_text(value)

    @field_validator("manifest_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in _HEX_64 for char in value):
            raise ValueError("invalid manifest digest")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> PublicationApproval:
        if (
            self.approved_at.tzinfo is None
            or self.approved_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("approval timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")
        return self

    def assert_current(self, *, profile_id: str, manifest_sha256: str, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if self.profile_id != profile_id or self.manifest_sha256 != manifest_sha256:
            raise AdapterError("publication approval does not bind this profile and manifest")
        if now < self.approved_at or now >= self.expires_at:
            raise AdapterError("publication approval is not currently valid")


class PublicationOutcome(StrEnum):
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class PublicationReceipt(ReleaseModel):
    schema_version: Literal[1] = 1
    profile_id: str
    manifest_sha256: str
    outcome: PublicationOutcome
    published_at: datetime
    remote_id: str | None = Field(default=None, max_length=256)
    remote_url: str | None = Field(default=None, max_length=2048)

    @field_validator("outcome", mode="before")
    @classmethod
    def accept_serialized_outcome(cls, value: Any) -> Any:
        return _serialized_enum(value, PublicationOutcome)

    @field_validator("published_at", mode="before")
    @classmethod
    def accept_serialized_published_at(cls, value: Any) -> Any:
        return _serialized_datetime(value)

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        return _validate_profile_id(value)

    @field_validator("remote_id")
    @classmethod
    def validate_remote_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_remote_text(value)

    @field_validator("remote_url")
    @classmethod
    def validate_remote_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("remote_url is malformed") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("remote_url must be a credential-free HTTPS URL")
        return value

    @field_validator("manifest_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in _HEX_64 for char in value):
            raise ValueError("invalid manifest digest")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> PublicationReceipt:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("published_at must be timezone-aware")
        if self.outcome is PublicationOutcome.PUBLISHED and not self.remote_id:
            raise ValueError("published receipt requires a remote ID")
        if self.outcome is not PublicationOutcome.PUBLISHED and (
            self.remote_id is not None or self.remote_url is not None
        ):
            raise ValueError("non-published receipts may not claim a remote release")
        return self


class DupeChecker(Protocol):
    def check(
        self,
        metadata: ReleaseMetadata,
        *,
        profile_id: str,
        manifest_sha256: str,
    ) -> DupeCheckReceipt: ...


class TrackerPublisher(Protocol):
    def publish(
        self,
        kit_directory: Path,
        *,
        approval: PublicationApproval,
        dupe_receipt: DupeCheckReceipt,
    ) -> PublicationReceipt: ...


def _serialized_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value


def _serialized_enum(value: Any, enum_type: type[StrEnum]) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return enum_type(value)
    except ValueError:
        return value


def _normalized_endpoint(
    value: str,
    *,
    allowed_hosts: Sequence[str],
    allow_loopback_http: bool,
) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AdapterConfigurationError("endpoint URL is malformed") from exc
    hostname = _normalize_host(parsed.hostname or "")
    allowlist = {_normalize_host(item) for item in allowed_hosts}
    if not hostname or hostname not in allowlist or not allowlist:
        raise AdapterConfigurationError("endpoint host is not explicitly allowlisted")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AdapterConfigurationError("endpoint may not contain userinfo, query or fragment")
    loopback = hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (
        allow_loopback_http and parsed.scheme == "http" and loopback
    ):
        raise AdapterConfigurationError("endpoint must use HTTPS (HTTP only for loopback)")
    if port is not None and not 1 <= port <= 65535:
        raise AdapterConfigurationError("endpoint port is invalid")
    if parsed.path and not parsed.path.startswith("/"):
        raise AdapterConfigurationError("endpoint path is invalid")
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _join_endpoint(base: str, suffix: str) -> str:
    if not suffix.startswith("/") or "?" in suffix or "#" in suffix or "\\" in suffix:
        raise AdapterConfigurationError("API path must be an absolute URL path")
    return base.rstrip("/") + suffix


def _validate_credential_name(value: str) -> str:
    if not _CREDENTIAL_NAME.fullmatch(value):
        raise AdapterConfigurationError("credential name is invalid")
    return value


def _validate_profile_id(value: str) -> str:
    if not _PROFILE_ID.fullmatch(value):
        raise ValueError("profile_id must be a lowercase stable identifier")
    return value


def _validate_remote_text(value: str) -> str:
    if not value or value != unicodedata.normalize("NFC", value):
        raise ValueError("remote text must be non-empty canonical Unicode")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("remote text contains unsafe control or format characters")
    return value


def _is_safe_remote_text(value: object, *, maximum: int) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        return False
    try:
        _validate_remote_text(value)
    except ValueError:
        return False
    return True


def _normalize_host(value: str) -> str:
    normalized = value.rstrip(".").casefold()
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        pass
    if not normalized.isascii() or not _HOSTNAME.fullmatch(normalized):
        raise AdapterConfigurationError("host allowlist contains an invalid host")
    return normalized


def _load_credential(loader: CredentialLoader, name: str, *, label: str) -> str:
    try:
        value = loader(name)
    except Exception:
        # A third-party loader exception may contain the credential value.
        raise AdapterError(f"{label} credential loading failed") from None
    if not value:
        raise AdapterError(f"{label} credential is unavailable")
    return value


def _read_stable_torrent(path: Path) -> bytes:
    path = Path(path)
    before = path.lstat()
    reparse = getattr(before, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    )
    if path.is_symlink() or reparse or not stat.S_ISREG(before.st_mode):
        raise AdapterError("torrent input must be a regular non-linked file")
    if before.st_size > _MAX_TORRENT_BYTES:
        raise AdapterError("torrent metadata exceeds the safe size limit")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        remaining = _MAX_TORRENT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = path.lstat()
    identities = {
        (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        for item in (before, opened, after_open, after)
    }
    data = b"".join(chunks)
    if len(identities) != 1 or len(data) != before.st_size:
        raise AdapterError("torrent input changed while it was read")
    if len(data) > _MAX_TORRENT_BYTES:
        raise AdapterError("torrent metadata exceeds the safe size limit")
    return data


def _torrent_upload_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 255
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or not value.casefold().endswith(".torrent")
    ):
        raise AdapterError("torrent upload name is invalid")
    try:
        return _validate_remote_text(value)
    except ValueError as exc:
        raise AdapterError("torrent upload name is invalid") from exc


def _close_owned_client(client: httpx.Client, *, owned: bool) -> None:
    if not owned:
        return
    try:
        client.close()
    except Exception:
        # Transport cleanup cannot change an already observed remote outcome.
        pass


def _unknown_publication(profile_id: str, manifest_sha256: str) -> PublicationReceipt:
    return PublicationReceipt(
        profile_id=profile_id,
        manifest_sha256=manifest_sha256,
        outcome=PublicationOutcome.UNKNOWN,
        published_at=datetime.now(UTC),
    )


class QBitTorrentClient:
    """Minimal qBittorrent adapter: add stopped, then request a full recheck."""

    def __init__(
        self,
        base_url: str,
        *,
        allowed_hosts: Sequence[str],
        username_credential: str,
        password_credential: str,
        credential_loader: CredentialLoader = read_secret,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = _normalized_endpoint(
            base_url,
            allowed_hosts=allowed_hosts,
            allow_loopback_http=True,
        )
        self._username_credential = _validate_credential_name(username_credential)
        self._password_credential = _validate_credential_name(password_credential)
        self._credential_loader = credential_loader
        self._client = client
        self._timeout = timeout

    def add_paused_and_recheck(
        self,
        torrent: bytes | Path,
        *,
        expected_infohash: str,
        torrent_name: str | None = None,
        save_path: Path | None = None,
        category: str | None = None,
    ) -> QBitTorrentReceipt:
        from .torrent import TorrentError, verify_torrent

        if isinstance(torrent, Path):
            inferred_name = torrent.name
            if torrent_name is not None and torrent_name != inferred_name:
                raise AdapterError("torrent upload name does not match its path")
            upload_name = _torrent_upload_name(inferred_name)
            data = _read_stable_torrent(torrent)
        elif isinstance(torrent, bytes):
            if not torrent or len(torrent) > _MAX_TORRENT_BYTES:
                raise AdapterError("torrent metadata exceeds the safe size limit")
            if torrent_name is None:
                raise AdapterError("torrent upload name is required for byte input")
            upload_name = _torrent_upload_name(torrent_name)
            data = torrent
        else:
            raise TypeError("torrent must be bytes or pathlib.Path")
        try:
            verification = verify_torrent(
                data,
                expected_infohash=expected_infohash,
            )
        except TorrentError as exc:
            raise AdapterError("torrent input does not match the expected identity") from exc
        add_fields = {"paused": "true", "stopped": "true"}
        if save_path is not None:
            destination = Path(save_path)
            if not destination.is_absolute() or "\x00" in os.fspath(destination):
                raise AdapterError("qBittorrent save path must be an absolute local path")
            details = destination.lstat()
            reparse = getattr(details, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            )
            if destination.is_symlink() or reparse or not stat.S_ISDIR(details.st_mode):
                raise AdapterError("qBittorrent save path must be a regular non-linked directory")
            add_fields["savepath"] = os.fspath(destination)
        if category is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}", category):
                raise AdapterError("qBittorrent category is invalid")
            add_fields["category"] = category
        username = _load_credential(
            self._credential_loader, self._username_credential, label="qBittorrent username"
        )
        password = _load_credential(
            self._credential_loader, self._password_credential, label="qBittorrent password"
        )
        owned = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout, follow_redirects=False)
        try:
            try:
                login = client.post(
                    _join_endpoint(self._base_url, "/api/v2/auth/login"),
                    data={"username": username, "password": password},
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                raise AdapterError("qBittorrent authentication transport failed") from exc
            if login.is_redirect or login.status_code != 200 or login.text.strip() != "Ok.":
                raise AdapterError("qBittorrent authentication was rejected")

            try:
                added = client.post(
                    _join_endpoint(self._base_url, "/api/v2/torrents/add"),
                    data=add_fields,
                    files={"torrents": (upload_name, data, "application/x-bittorrent")},
                    follow_redirects=False,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                return QBitTorrentReceipt(
                    outcome=QBitTorrentOutcome.UNKNOWN,
                    infohash=verification.infohash,
                    added_paused=None,
                    full_recheck_requested=False,
                    recorded_at=datetime.now(UTC),
                )
            if added.is_redirect:
                return QBitTorrentReceipt(
                    outcome=QBitTorrentOutcome.UNKNOWN,
                    infohash=verification.infohash,
                    added_paused=None,
                    full_recheck_requested=False,
                    recorded_at=datetime.now(UTC),
                )
            if added.status_code != 200 or added.text.strip() != "Ok.":
                return QBitTorrentReceipt(
                    outcome=QBitTorrentOutcome.REJECTED,
                    infohash=verification.infohash,
                    added_paused=False,
                    full_recheck_requested=False,
                    recorded_at=datetime.now(UTC),
                )
            try:
                rechecked = client.post(
                    _join_endpoint(self._base_url, "/api/v2/torrents/recheck"),
                    data={"hashes": verification.infohash},
                    follow_redirects=False,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                return QBitTorrentReceipt(
                    outcome=QBitTorrentOutcome.UNKNOWN,
                    infohash=verification.infohash,
                    added_paused=True,
                    full_recheck_requested=None,
                    recorded_at=datetime.now(UTC),
                )
            if rechecked.is_redirect or rechecked.status_code != 200:
                return QBitTorrentReceipt(
                    outcome=QBitTorrentOutcome.UNKNOWN,
                    infohash=verification.infohash,
                    added_paused=True,
                    full_recheck_requested=None,
                    recorded_at=datetime.now(UTC),
                )
            return QBitTorrentReceipt(
                outcome=QBitTorrentOutcome.ADDED_AND_RECHECKING,
                infohash=verification.infohash,
                added_paused=True,
                full_recheck_requested=True,
                recorded_at=datetime.now(UTC),
            )
        finally:
            username = ""
            password = ""
            _close_owned_client(client, owned=owned)


class HttpDupeChecker:
    """Strict one-shot JSON duplicate check against an operator-set endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        allowed_hosts: Sequence[str],
        credential_name: str,
        credential_loader: CredentialLoader = read_secret,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._endpoint = _normalized_endpoint(
            endpoint,
            allowed_hosts=allowed_hosts,
            allow_loopback_http=False,
        )
        self._credential_name = _validate_credential_name(credential_name)
        self._credential_loader = credential_loader
        self._client = client
        self._timeout = timeout

    def check(
        self,
        metadata: ReleaseMetadata,
        *,
        profile_id: str,
        manifest_sha256: str,
    ) -> DupeCheckReceipt:
        profile_id = _validate_profile_id(profile_id)
        if len(manifest_sha256) != 64 or any(char not in _HEX_64 for char in manifest_sha256):
            raise ValueError("manifest_sha256 is invalid")
        token = _load_credential(
            self._credential_loader, self._credential_name, label="tracker"
        )
        now = datetime.now(UTC)
        owned = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout, follow_redirects=False)
        try:
            try:
                response = client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "schema_version": 1,
                        "profile_id": profile_id,
                        "manifest_sha256": manifest_sha256,
                        "metadata": metadata.model_dump(mode="json"),
                    },
                    follow_redirects=False,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                return DupeCheckReceipt(
                    profile_id=profile_id,
                    manifest_sha256=manifest_sha256,
                    metadata_sha256=metadata.canonical_digest(),
                    outcome=DupeCheckOutcome.UNKNOWN,
                    checked_at=now,
                )
            if response.is_redirect or response.status_code != 200:
                return DupeCheckReceipt(
                    profile_id=profile_id,
                    manifest_sha256=manifest_sha256,
                    metadata_sha256=metadata.canonical_digest(),
                    outcome=DupeCheckOutcome.UNKNOWN,
                    checked_at=now,
                )
            try:
                document = response.json()
            except ValueError:
                document = None
            if not isinstance(document, dict) or set(document) - {
                "status",
                "matches",
                "request_id",
            }:
                document = None
            status = document.get("status") if document else None
            matches = document.get("matches", []) if document else []
            request_id = document.get("request_id") if document else None
            valid_matches = (
                isinstance(matches, list)
                and len(matches) <= 100
                and all(
                    _is_safe_remote_text(item, maximum=512) and token not in item
                    for item in matches
                )
            )
            valid_request = request_id is None or (
                _is_safe_remote_text(request_id, maximum=256)
                and token not in request_id
            )
            if status == "clear" and valid_matches and not matches and valid_request:
                outcome = DupeCheckOutcome.CLEAR
            elif status == "duplicate" and valid_matches and bool(matches) and valid_request:
                outcome = DupeCheckOutcome.DUPLICATE
            else:
                outcome = DupeCheckOutcome.UNKNOWN
                matches = []
                request_id = None
            return DupeCheckReceipt(
                profile_id=profile_id,
                manifest_sha256=manifest_sha256,
                metadata_sha256=metadata.canonical_digest(),
                outcome=outcome,
                matches=tuple(matches),
                checked_at=now,
                remote_request_id=request_id,
            )
        finally:
            token = ""
            _close_owned_client(client, owned=owned)


class HttpTrackerPublisher:
    """One-shot tracker publisher guarded by manifest and dupe receipts."""

    def __init__(
        self,
        endpoint: str,
        *,
        profile_id: str,
        allowed_hosts: Sequence[str],
        credential_name: str,
        credential_loader: CredentialLoader = read_secret,
        client: httpx.Client | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._endpoint = _normalized_endpoint(
            endpoint,
            allowed_hosts=allowed_hosts,
            allow_loopback_http=False,
        )
        self._profile_id = _validate_profile_id(profile_id)
        self._credential_name = _validate_credential_name(credential_name)
        self._credential_loader = credential_loader
        self._client = client
        self._timeout = timeout

    def publish(
        self,
        kit_directory: Path,
        *,
        approval: PublicationApproval,
        dupe_receipt: DupeCheckReceipt,
    ) -> PublicationReceipt:
        now = datetime.now(UTC)
        approval.assert_current(
            profile_id=self._profile_id,
            manifest_sha256=approval.manifest_sha256,
            now=now,
        )
        try:
            manifest, verified_artifacts = load_verified_upload_kit(
                kit_directory,
                expected_manifest_sha256=approval.manifest_sha256,
            )
        except (OSError, ValueError, UploadKitError) as exc:
            raise AdapterError("approved upload kit failed revalidation") from exc
        if manifest.profile_id != self._profile_id:
            raise AdapterError("upload kit belongs to a different tracker profile")
        if (
            dupe_receipt.profile_id != self._profile_id
            or dupe_receipt.manifest_sha256 != approval.manifest_sha256
            or dupe_receipt.metadata_sha256 != manifest.metadata_sha256
            or dupe_receipt.outcome is not DupeCheckOutcome.CLEAR
        ):
            raise AdapterError("a current CLEAR dupe receipt for this manifest is required")
        token = _load_credential(
            self._credential_loader, self._credential_name, label="tracker"
        )
        file_parts: list[tuple[str, tuple[str, bytes, str]]] = []
        for item in manifest.files:
            data = verified_artifacts[item.path]
            file_parts.append(("files", (item.path, data, item.media_type)))
        manifest_data = manifest.canonical_bytes()
        file_parts.append(
            ("files", ("package-manifest.json", manifest_data, "application/json"))
        )
        owned = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout, follow_redirects=False)
        try:
            try:
                response = client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {token}"},
                    data={
                        "schema_version": "1",
                        "profile_id": self._profile_id,
                        "manifest_sha256": approval.manifest_sha256,
                    },
                    files=file_parts,
                    follow_redirects=False,
                )
            except (httpx.TimeoutException, httpx.TransportError):
                return _unknown_publication(self._profile_id, approval.manifest_sha256)
            if response.is_redirect:
                return _unknown_publication(self._profile_id, approval.manifest_sha256)
            if response.status_code in {400, 401, 403, 409, 422}:
                return PublicationReceipt(
                    profile_id=self._profile_id,
                    manifest_sha256=approval.manifest_sha256,
                    outcome=PublicationOutcome.REJECTED,
                    published_at=datetime.now(UTC),
                )
            if response.status_code != 200:
                return _unknown_publication(self._profile_id, approval.manifest_sha256)
            try:
                document = response.json()
            except ValueError:
                document = None
            if not isinstance(document, dict) or set(document) - {"status", "id", "url"}:
                return _unknown_publication(self._profile_id, approval.manifest_sha256)
            status = document.get("status")
            remote_id = document.get("id")
            remote_url = document.get("url")
            if status == "rejected" and remote_id is None and remote_url is None:
                return PublicationReceipt(
                    profile_id=self._profile_id,
                    manifest_sha256=approval.manifest_sha256,
                    outcome=PublicationOutcome.REJECTED,
                    published_at=datetime.now(UTC),
                )
            if not _is_safe_remote_text(remote_id, maximum=256) or token in remote_id:
                return _unknown_publication(self._profile_id, approval.manifest_sha256)
            if remote_url is not None:
                if (
                    not isinstance(remote_url, str)
                    or len(remote_url) > 2048
                    or token in remote_url
                ):
                    return _unknown_publication(self._profile_id, approval.manifest_sha256)
                try:
                    _normalized_endpoint(
                        remote_url,
                        allowed_hosts=((urlsplit(self._endpoint).hostname or ""),),
                        allow_loopback_http=False,
                    )
                except AdapterConfigurationError:
                    return _unknown_publication(self._profile_id, approval.manifest_sha256)
            if status != "published":
                return _unknown_publication(self._profile_id, approval.manifest_sha256)
            return PublicationReceipt(
                profile_id=self._profile_id,
                manifest_sha256=approval.manifest_sha256,
                outcome=PublicationOutcome.PUBLISHED,
                published_at=datetime.now(UTC),
                remote_id=remote_id,
                remote_url=remote_url,
            )
        finally:
            token = ""
            file_parts.clear()
            _close_owned_client(client, owned=owned)


__all__ = [
    "AdapterConfigurationError",
    "AdapterError",
    "CredentialLoader",
    "DupeCheckOutcome",
    "DupeCheckReceipt",
    "DupeChecker",
    "HttpDupeChecker",
    "HttpTrackerPublisher",
    "PublicationApproval",
    "PublicationOutcome",
    "PublicationReceipt",
    "QBitTorrentClient",
    "QBitTorrentOutcome",
    "QBitTorrentReceipt",
    "TrackerPublisher",
]
