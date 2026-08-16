"""Deterministic, fail-closed BitTorrent v1 release payloads.

The public torrent contains exactly one virtual file while deliberately using
the v1 multi-file representation.  That is the only v1 representation which
can preserve the required ``Release.Name/Release.Name.mkv`` payload root.

No creation timestamp, client identifier, comment, or local filesystem path is
written to the metainfo.  Rebuilding an unchanged file with the same profile is
therefore byte-for-byte deterministic.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MIN_PROTOCOL_PIECE_SIZE = 16 * 1024
_MAX_PROTOCOL_PIECE_SIZE = 64 * 1024 * 1024
_MAX_TORRENT_SIZE = 16 * 1024 * 1024
_MAX_BENCODE_DEPTH = 64
_MAX_BENCODE_ITEMS = 100_000
_WINDOWS_REPARSE_POINT = 0x400
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_SAFE_RELEASE_PUNCTUATION = frozenset("._ ()'&+-")
_SAFE_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class TorrentError(ValueError):
    """Base class for rejected torrent input or output."""


class BencodeError(TorrentError):
    """Bencoded input is invalid or not canonical."""


class TorrentSecurityError(TorrentError):
    """A filesystem path failed the no-links/no-races security policy."""


class TorrentVerificationError(TorrentError):
    """Torrent metainfo or payload verification failed."""


class TorrentProfile(BaseModel):
    """Tracker-specific v1 torrent policy.

    ``announce_url`` may contain a tracker passkey, so it is excluded from the
    model representation.  Callers must apply the same rule to application
    logs and serialized job state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: int = 1
    source: str = Field(min_length=1, max_length=64)
    announce_url: str = Field(min_length=1, max_length=2048, repr=False)
    piece_size_min: int = 256 * 1024
    piece_size_max: int = 16 * 1024 * 1024
    piece_size_default: int = 1024 * 1024
    target_piece_count_min: int = Field(default=1000, ge=1, le=1_000_000)
    target_piece_count_max: int = Field(default=2000, ge=1, le=1_000_000)

    @field_validator("version")
    @classmethod
    def _only_v1(cls, value: int) -> int:
        if value != 1:
            raise ValueError("only BitTorrent metainfo version 1 is supported")
        return value

    @field_validator("source")
    @classmethod
    def _valid_source(cls, value: str) -> str:
        if _SAFE_SOURCE.fullmatch(value) is None:
            raise ValueError(
                "source must be a 1-64 character ASCII tracker source token"
            )
        return value

    @field_validator("announce_url")
    @classmethod
    def _valid_announce_url(cls, value: str) -> str:
        _validate_announce_url(value)
        return value

    @field_validator("piece_size_min", "piece_size_max", "piece_size_default")
    @classmethod
    def _valid_piece_size(cls, value: int) -> int:
        if not _is_power_of_two(value):
            raise ValueError("piece sizes must be powers of two")
        if not _MIN_PROTOCOL_PIECE_SIZE <= value <= _MAX_PROTOCOL_PIECE_SIZE:
            raise ValueError(
                "piece sizes must be between 16 KiB and 64 MiB inclusive"
            )
        return value

    @model_validator(mode="after")
    def _ordered_bounds(self) -> "TorrentProfile":
        if self.piece_size_min > self.piece_size_max:
            raise ValueError("piece_size_min cannot exceed piece_size_max")
        if not self.piece_size_min <= self.piece_size_default <= self.piece_size_max:
            raise ValueError("piece_size_default must be inside the configured bounds")
        if self.target_piece_count_min > self.target_piece_count_max:
            raise ValueError(
                "target_piece_count_min cannot exceed target_piece_count_max"
            )
        return self


@dataclass(frozen=True, slots=True)
class TorrentBuildResult:
    """Immutable evidence returned after a torrent was written and re-read."""

    torrent_path: Path
    infohash: str
    sha256: str
    torrent_sha256: str
    piece_length: int
    piece_count: int
    file_size: int
    payload_path: str
    torrent_bytes: bytes = field(repr=False)

    @property
    def file_sha256(self) -> str:
        """Explicit alias for the payload SHA-256 stored in ``sha256``."""

        return self.sha256


@dataclass(frozen=True, slots=True)
class TorrentVerification:
    """Evidence extracted from canonical metainfo and, optionally, its file."""

    infohash: str
    sha256: str | None
    torrent_sha256: str
    piece_length: int
    piece_count: int
    file_size: int
    payload_path: str
    release_name: str
    source: str

    @property
    def file_sha256(self) -> str | None:
        return self.sha256


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    path: Path
    device: int
    inode: int
    file_type: int
    is_reparse: bool


@dataclass(frozen=True, slots=True)
class _FileFingerprint:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    file_attributes: int


@dataclass(frozen=True, slots=True)
class _FileGuard:
    path: Path
    chain: tuple[_PathIdentity, ...]
    fingerprint: _FileFingerprint


@dataclass(frozen=True, slots=True)
class _PayloadHashes:
    size: int
    sha256: str
    pieces: bytes
    guard: _FileGuard


def _is_power_of_two(value: int) -> bool:
    return value > 0 and value & (value - 1) == 0


def _validate_announce_url(value: str) -> None:
    if not value.isascii() or any(character.isspace() for character in value):
        raise ValueError("announce_url must be an ASCII URL without whitespace")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("announce_url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("announce_url must not contain URL user information")
    if parsed.fragment:
        raise ValueError("announce_url must not contain a fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("announce_url contains an invalid port") from exc


def _coerce_profile(profile: TorrentProfile | Mapping[str, object]) -> TorrentProfile:
    if isinstance(profile, TorrentProfile):
        return profile
    return TorrentProfile.model_validate(profile)


def validate_release_name(release_name: str) -> str:
    """Return a safe canonical release root or raise ``TorrentSecurityError``."""

    if not isinstance(release_name, str):
        raise TorrentSecurityError("release_name must be a string")
    if unicodedata.normalize("NFC", release_name) != release_name:
        raise TorrentSecurityError("release_name must use canonical NFC text")
    if not release_name or len(release_name) > 240:
        raise TorrentSecurityError("release_name must contain 1-240 characters")
    if not unicodedata.category(release_name[0]).startswith(("L", "N")):
        raise TorrentSecurityError("release_name must start with a letter or number")
    if any(
        not unicodedata.category(character).startswith(("L", "N"))
        and character not in _SAFE_RELEASE_PUNCTUATION
        for character in release_name
    ):
        raise TorrentSecurityError(
            "release_name contains a path-unsafe or ambiguous Unicode character"
        )
    if release_name[-1] in {".", " "}:
        raise TorrentSecurityError("release_name cannot end in a dot or space")
    if release_name.casefold().endswith(".mkv"):
        raise TorrentSecurityError("release_name must not include the .mkv suffix")
    first_component = release_name.split(".", 1)[0].casefold()
    if first_component in {item.casefold() for item in _WINDOWS_RESERVED_NAMES}:
        raise TorrentSecurityError("release_name uses a reserved filesystem name")
    filename = f"{release_name}.mkv"
    if len(filename.encode("utf-8")) > 255:
        raise TorrentSecurityError("release payload filename exceeds 255 bytes")
    return release_name


def payload_path_for(release_name: str) -> str:
    """Return the only public payload path accepted by this module."""

    safe_name = validate_release_name(release_name)
    return f"{safe_name}/{safe_name}.mkv"


def select_piece_size(
    file_size: int, profile: TorrentProfile | Mapping[str, object]
) -> int:
    """Select a bounded power-of-two piece size deterministically."""

    policy = _coerce_profile(profile)
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size <= 0:
        raise TorrentError("file_size must be a positive integer")
    piece_size = policy.piece_size_default
    while (
        _piece_count(file_size, piece_size) > policy.target_piece_count_max
        and piece_size < policy.piece_size_max
    ):
        piece_size *= 2
    while (
        _piece_count(file_size, piece_size) < policy.target_piece_count_min
        and piece_size > policy.piece_size_min
    ):
        piece_size //= 2
    piece_size = min(policy.piece_size_max, max(policy.piece_size_min, piece_size))
    if _piece_count(file_size, piece_size) > policy.target_piece_count_max:
        raise TorrentError(
            "payload exceeds the profile's maximum piece count at the largest piece size"
        )
    return piece_size


def select_piece_length(
    file_size: int, profile: TorrentProfile | Mapping[str, object]
) -> int:
    """BitTorrent terminology alias for :func:`select_piece_size`."""

    return select_piece_size(file_size, profile)


def _piece_count(file_size: int, piece_length: int) -> int:
    return (file_size + piece_length - 1) // piece_length


def _sha1(data: bytes = b""):
    # SHA-1 is mandated by the BitTorrent v1 wire format, not used here as a
    # general-purpose collision-resistant security digest.
    try:
        return hashlib.sha1(data, usedforsecurity=False)
    except TypeError:  # pragma: no cover - compatibility with older providers
        return hashlib.sha1(data)


def bencode(value: object) -> bytes:
    """Encode the supported bencode types with byte-sorted dictionary keys."""

    return _bencode(value, depth=0, active=set())


def _bencode(value: object, *, depth: int, active: set[int]) -> bytes:
    if depth > _MAX_BENCODE_DEPTH:
        raise BencodeError("bencode nesting exceeds the safety limit")
    if isinstance(value, bool):
        raise BencodeError("booleans are not bencode integers")
    if isinstance(value, int):
        if not -(2**63) <= value <= 2**63 - 1:
            raise BencodeError("bencode integer exceeds the signed 64-bit range")
        return b"i" + str(value).encode("ascii") + b"e"
    if isinstance(value, str):
        return _encode_bytes(value.encode("utf-8"))
    if isinstance(value, bytes):
        return _encode_bytes(value)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise BencodeError("circular bencode container")
        active.add(identity)
        try:
            encoded = b"".join(
                _bencode(item, depth=depth + 1, active=active) for item in value
            )
        finally:
            active.remove(identity)
        return b"l" + encoded + b"e"
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise BencodeError("circular bencode container")
        active.add(identity)
        try:
            entries: list[tuple[bytes, object]] = []
            seen: set[bytes] = set()
            for key, item in value.items():
                if isinstance(key, str):
                    encoded_key = key.encode("utf-8")
                elif isinstance(key, bytes):
                    encoded_key = key
                else:
                    raise BencodeError("bencode dictionary keys must be bytes or str")
                if encoded_key in seen:
                    raise BencodeError("duplicate encoded dictionary key")
                seen.add(encoded_key)
                entries.append((encoded_key, item))
            entries.sort(key=lambda entry: entry[0])
            encoded = b"".join(
                _encode_bytes(key)
                + _bencode(item, depth=depth + 1, active=active)
                for key, item in entries
            )
        finally:
            active.remove(identity)
        return b"d" + encoded + b"e"
    raise BencodeError(f"unsupported bencode type: {type(value).__name__}")


def _encode_bytes(value: bytes) -> bytes:
    return str(len(value)).encode("ascii") + b":" + value


class _BencodeDecoder:
    def __init__(self, data: bytes) -> None:
        if len(data) > _MAX_TORRENT_SIZE:
            raise BencodeError("bencoded input exceeds the 16 MiB safety limit")
        self.data = data
        self.offset = 0
        self.items = 0

    def decode(self) -> object:
        value = self._value(depth=0)
        if self.offset != len(self.data):
            raise BencodeError("trailing data after bencoded value")
        return value

    def _value(self, *, depth: int) -> object:
        if depth > _MAX_BENCODE_DEPTH:
            raise BencodeError("bencode nesting exceeds the safety limit")
        self.items += 1
        if self.items > _MAX_BENCODE_ITEMS:
            raise BencodeError("bencode item count exceeds the safety limit")
        if self.offset >= len(self.data):
            raise BencodeError("truncated bencoded value")
        marker = self.data[self.offset]
        if 48 <= marker <= 57:
            return self._bytes()
        if marker == ord("i"):
            return self._integer()
        if marker == ord("l"):
            self.offset += 1
            values: list[object] = []
            while not self._at_end_marker():
                values.append(self._value(depth=depth + 1))
            self.offset += 1
            return values
        if marker == ord("d"):
            self.offset += 1
            values: dict[bytes, object] = {}
            previous: bytes | None = None
            while not self._at_end_marker():
                if self.offset >= len(self.data) or not 48 <= self.data[self.offset] <= 57:
                    raise BencodeError("bencode dictionary key must be a byte string")
                key = self._bytes()
                if previous is not None and key <= previous:
                    raise BencodeError(
                        "bencode dictionary keys are duplicate or not canonical"
                    )
                previous = key
                values[key] = self._value(depth=depth + 1)
            self.offset += 1
            return values
        raise BencodeError("invalid bencode type marker")

    def _at_end_marker(self) -> bool:
        if self.offset >= len(self.data):
            raise BencodeError("unterminated bencode container")
        return self.data[self.offset] == ord("e")

    def _bytes(self) -> bytes:
        colon = self.data.find(b":", self.offset)
        if colon < 0:
            raise BencodeError("unterminated bencode byte-string length")
        token = self.data[self.offset : colon]
        if not token or not token.isdigit():
            raise BencodeError("invalid bencode byte-string length")
        if len(token) > 10:
            raise BencodeError("bencode byte-string length is excessive")
        if len(token) > 1 and token.startswith(b"0"):
            raise BencodeError("non-canonical bencode byte-string length")
        length = int(token)
        start = colon + 1
        end = start + length
        if end > len(self.data):
            raise BencodeError("truncated bencode byte string")
        self.offset = end
        return self.data[start:end]

    def _integer(self) -> int:
        end = self.data.find(b"e", self.offset + 1)
        if end < 0:
            raise BencodeError("unterminated bencode integer")
        token = self.data[self.offset + 1 : end]
        if not token:
            raise BencodeError("empty bencode integer")
        if token == b"-0" or token.startswith(b"+"):
            raise BencodeError("non-canonical bencode integer")
        digits = token[1:] if token.startswith(b"-") else token
        if not digits or not digits.isdigit():
            raise BencodeError("invalid bencode integer")
        if len(digits) > 19:
            raise BencodeError("bencode integer exceeds the signed 64-bit range")
        if len(digits) > 1 and digits.startswith(b"0"):
            raise BencodeError("non-canonical bencode integer")
        self.offset = end + 1
        value = int(token)
        if not -(2**63) <= value <= 2**63 - 1:
            raise BencodeError("bencode integer exceeds the signed 64-bit range")
        return value


def bdecode(data: bytes | bytearray | memoryview) -> object:
    """Decode canonical bencode and reject ambiguous or excessive input."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("bdecode expects a bytes-like value")
    return _BencodeDecoder(bytes(data)).decode()


def _absolute_path(path: Path, *, description: str) -> Path:
    candidate = Path(path)
    raw = os.fspath(candidate)
    if "\x00" in raw:
        raise TorrentSecurityError(f"{description} contains a null byte")
    if not candidate.is_absolute():
        raise TorrentSecurityError(f"{description} must be absolute")
    if os.name == "nt" and raw.startswith(("\\\\", "//")):
        raise TorrentSecurityError(f"{description} cannot use a network/UNC path")
    for component in candidate.parts:
        if component in {".", ".."}:
            raise TorrentSecurityError(f"{description} contains path traversal")
        if unicodedata.normalize("NFC", component) != component:
            raise TorrentSecurityError(f"{description} must use canonical NFC text")
        if any(unicodedata.category(character).startswith("C") for character in component):
            raise TorrentSecurityError(f"{description} contains unsafe Unicode")
    return Path(os.path.abspath(candidate))


def _file_attributes(details: os.stat_result) -> int:
    return int(getattr(details, "st_file_attributes", 0))


def _reject_link(path: Path, details: os.stat_result, *, description: str) -> None:
    if stat.S_ISLNK(details.st_mode) or _file_attributes(details) & _WINDOWS_REPARSE_POINT:
        raise TorrentSecurityError(
            f"{description} cannot contain a symbolic link, junction, or reparse point"
        )
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        raise TorrentSecurityError(
            f"{description} cannot contain a symbolic link, junction, or reparse point"
        )


def _identity(path: Path, details: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        path=path,
        device=details.st_dev,
        inode=details.st_ino,
        file_type=stat.S_IFMT(details.st_mode),
        # Directory mtime/ctime and incidental Windows attribute bits (for
        # example ARCHIVE) legitimately change when this process creates its
        # own temporary child.  Only replacement identity, type and the
        # security-relevant reparse bit belong in a directory-chain guard.
        is_reparse=bool(_file_attributes(details) & _WINDOWS_REPARSE_POINT),
    )


def _snapshot_chain(
    path: Path,
    *,
    description: str,
    leaf_kind: str,
    leaf_may_be_missing: bool = False,
) -> tuple[_PathIdentity, ...]:
    anchor = Path(path.anchor)
    current = anchor
    components: list[Path] = [anchor]
    for component in path.parts[1:]:
        current /= component
        components.append(current)
    snapshots: list[_PathIdentity] = []
    for index, component_path in enumerate(components):
        leaf = index == len(components) - 1
        try:
            details = os.lstat(component_path)
        except FileNotFoundError:
            if leaf and leaf_may_be_missing:
                break
            raise TorrentSecurityError(
                f"{description} does not exist: {component_path}"
            ) from None
        _reject_link(component_path, details, description=description)
        if leaf:
            if leaf_kind == "file" and not stat.S_ISREG(details.st_mode):
                raise TorrentSecurityError(f"{description} is not a regular file")
            if leaf_kind == "directory" and not stat.S_ISDIR(details.st_mode):
                raise TorrentSecurityError(f"{description} is not a directory")
        elif not stat.S_ISDIR(details.st_mode):
            raise TorrentSecurityError(
                f"{description} has a non-directory parent component"
            )
        snapshots.append(_identity(component_path, details))
    return tuple(snapshots)


def _assert_chain_unchanged(
    chain: tuple[_PathIdentity, ...], *, description: str
) -> None:
    for expected in chain:
        try:
            details = os.lstat(expected.path)
        except FileNotFoundError:
            raise TorrentSecurityError(
                f"{description} changed while it was being used"
            ) from None
        _reject_link(expected.path, details, description=description)
        if _identity(expected.path, details) != expected:
            raise TorrentSecurityError(
                f"{description} changed while it was being used"
            )


def _fingerprint(details: os.stat_result) -> _FileFingerprint:
    return _FileFingerprint(
        device=details.st_dev,
        inode=details.st_ino,
        mode=details.st_mode,
        size=details.st_size,
        modified_ns=details.st_mtime_ns,
        # On Windows ``st_ctime`` is a creation-time compatibility field and
        # may be refreshed asynchronously between lstat/fstat calls.  The file
        # ID, size, mtime, mode and reparse attributes remain the stable guards.
        changed_ns=details.st_ctime_ns if os.name == "posix" else 0,
        file_attributes=_file_attributes(details),
    )


def _open_readonly_nofollow(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise TorrentSecurityError(f"could not safely open input file: {path.name}") from exc
    return os.fdopen(descriptor, "rb", closefd=True)


def _guard_file(path: Path, *, description: str) -> _FileGuard:
    chain = _snapshot_chain(path, description=description, leaf_kind="file")
    details = os.lstat(path)
    return _FileGuard(path=path, chain=chain, fingerprint=_fingerprint(details))


def _assert_file_guard_unchanged(
    guard: _FileGuard, *, description: str = "source file"
) -> None:
    _assert_chain_unchanged(guard.chain, description=description)
    try:
        current = os.lstat(guard.path)
    except FileNotFoundError:
        raise TorrentSecurityError(
            f"{description} changed while it was being used"
        ) from None
    if _fingerprint(current) != guard.fingerprint:
        raise TorrentSecurityError(f"{description} changed while it was being used")


def _read_piece(handle: BinaryIO, piece_length: int) -> bytes:
    result = bytearray()
    while len(result) < piece_length:
        chunk = handle.read(piece_length - len(result))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def _hash_payload(path: Path, piece_length: int) -> _PayloadHashes:
    source = _absolute_path(path, description="payload path")
    if source.suffix.casefold() != ".mkv":
        raise TorrentSecurityError("payload path must name an MKV file")
    guard = _guard_file(source, description="payload path")
    if guard.fingerprint.size <= 0:
        raise TorrentSecurityError("payload MKV cannot be empty")
    digest = hashlib.sha256()
    pieces: list[bytes] = []
    read_size = 0
    with _open_readonly_nofollow(source) as handle:
        opened = os.fstat(handle.fileno())
        if _fingerprint(opened) != guard.fingerprint:
            raise TorrentSecurityError("payload changed before hashing began")
        while piece := _read_piece(handle, piece_length):
            read_size += len(piece)
            digest.update(piece)
            pieces.append(_sha1(piece).digest())
        completed = os.fstat(handle.fileno())
        if _fingerprint(completed) != guard.fingerprint:
            raise TorrentSecurityError("payload changed while it was being hashed")
    if read_size != guard.fingerprint.size:
        raise TorrentSecurityError("payload length changed while it was being hashed")
    _assert_file_guard_unchanged(guard, description="payload path")
    return _PayloadHashes(
        size=read_size,
        sha256=digest.hexdigest(),
        pieces=b"".join(pieces),
        guard=guard,
    )


def _read_guarded_bytes(path: Path, *, description: str, maximum: int) -> bytes:
    safe_path = _absolute_path(path, description=description)
    guard = _guard_file(safe_path, description=description)
    if guard.fingerprint.size > maximum:
        raise TorrentSecurityError(f"{description} exceeds the size limit")
    with _open_readonly_nofollow(safe_path) as handle:
        if _fingerprint(os.fstat(handle.fileno())) != guard.fingerprint:
            raise TorrentSecurityError(f"{description} changed before it was read")
        data = handle.read(maximum + 1)
        if len(data) > maximum:
            raise TorrentSecurityError(f"{description} exceeds the size limit")
        if _fingerprint(os.fstat(handle.fileno())) != guard.fingerprint:
            raise TorrentSecurityError(f"{description} changed while it was read")
    _assert_file_guard_unchanged(guard, description=description)
    return data


def _same_lexical_path(left: Path, right: Path) -> bool:
    def key(value: Path) -> str:
        return unicodedata.normalize("NFC", os.path.normcase(str(value))).casefold()

    return key(left) == key(right)


def _reject_casefold_alias(path: Path, *, description: str) -> None:
    target_key = unicodedata.normalize("NFC", path.name).casefold()
    try:
        entries = tuple(path.parent.iterdir())
    except OSError as exc:
        raise TorrentSecurityError(
            f"could not inspect {description} for casefold aliases"
        ) from exc
    for entry in entries:
        if (
            unicodedata.normalize("NFC", entry.name).casefold() == target_key
            and entry.name != path.name
        ):
            raise TorrentSecurityError(
                f"{description} has a Unicode/casefold-colliding alias"
            )


def _validate_destination(path: Path, *, source: Path) -> Path:
    destination = _absolute_path(path, description="torrent path")
    if destination.suffix.casefold() != ".torrent":
        raise TorrentSecurityError("torrent path must use the .torrent suffix")
    if _same_lexical_path(destination, source):
        raise TorrentSecurityError("torrent path cannot overwrite the payload")
    parent = destination.parent
    _snapshot_chain(parent, description="torrent output directory", leaf_kind="directory")
    _reject_casefold_alias(destination, description="torrent path")
    if os.path.lexists(destination):
        _snapshot_chain(destination, description="torrent path", leaf_kind="file")
        try:
            if os.path.samefile(destination, source):
                raise TorrentSecurityError("torrent path cannot alias the payload")
        except FileNotFoundError:
            raise TorrentSecurityError("torrent path changed during validation") from None
    return destination


def _atomic_write_torrent(path: Path, data: bytes) -> None:
    parent_guard = _snapshot_chain(
        path.parent,
        description="torrent output directory",
        leaf_kind="directory",
    )
    _assert_chain_unchanged(parent_guard, description="torrent output directory")
    _reject_casefold_alias(path, description="torrent path")
    if os.path.lexists(path):
        _snapshot_chain(path, description="torrent path", leaf_kind="file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        temporary_details = os.lstat(temporary)
        _reject_link(temporary, temporary_details, description="temporary torrent path")
        if not stat.S_ISREG(temporary_details.st_mode):
            raise TorrentSecurityError("temporary torrent is not a regular file")
        _assert_chain_unchanged(parent_guard, description="torrent output directory")
        _reject_casefold_alias(path, description="torrent path")
        if os.path.lexists(path):
            _snapshot_chain(path, description="torrent path", leaf_kind="file")
        os.replace(temporary, path)
        _reject_casefold_alias(path, description="torrent path")
        if os.name == "posix":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(path.parent, flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except (IsADirectoryError, PermissionError):
            # Never recurse or follow an attacker-controlled replacement.
            pass
    written = _read_guarded_bytes(
        path, description="written torrent", maximum=_MAX_TORRENT_SIZE
    )
    if written != data:
        raise TorrentSecurityError("written torrent failed byte-for-byte verification")


def build_private_torrent(
    source_path: Path,
    torrent_path: Path,
    *,
    release_name: str,
    profile: TorrentProfile | Mapping[str, object],
) -> TorrentBuildResult:
    """Build, atomically write, and re-verify one deterministic private torrent."""

    policy = _coerce_profile(profile)
    safe_release = validate_release_name(release_name)
    source = _absolute_path(Path(source_path), description="payload path")
    destination = _validate_destination(Path(torrent_path), source=source)

    # Select using the guarded pre-hash size, then hash from the same inode and
    # reject any replacement or metadata/content change observed around it.
    initial_guard = _guard_file(source, description="payload path")
    piece_length = select_piece_size(initial_guard.fingerprint.size, policy)
    payload = _hash_payload(source, piece_length)
    if payload.guard.fingerprint != initial_guard.fingerprint:
        raise TorrentSecurityError("payload changed before hashing began")

    filename = f"{safe_release}.mkv"
    info: dict[bytes, object] = {
        b"files": [{b"length": payload.size, b"path": [filename.encode("utf-8")]}],
        b"name": safe_release.encode("utf-8"),
        b"piece length": piece_length,
        b"pieces": payload.pieces,
        b"private": 1,
        b"source": policy.source.encode("ascii"),
    }
    metainfo: dict[bytes, object] = {
        b"announce": policy.announce_url.encode("ascii"),
        b"info": info,
    }
    torrent_bytes = bencode(metainfo)
    infohash = _sha1(bencode(info)).hexdigest()
    torrent_sha256 = hashlib.sha256(torrent_bytes).hexdigest()

    verified = verify_torrent(
        torrent_bytes,
        expected_release_name=safe_release,
        expected_infohash=infohash,
        expected_profile=policy,
    )
    if verified.piece_count != _piece_count(payload.size, piece_length):
        raise TorrentVerificationError("generated torrent has an invalid piece count")
    _assert_file_guard_unchanged(payload.guard, description="payload path")
    _atomic_write_torrent(destination, torrent_bytes)
    _assert_file_guard_unchanged(payload.guard, description="payload path")
    persisted = verify_torrent(
        destination,
        expected_release_name=safe_release,
        expected_infohash=infohash,
        expected_profile=policy,
    )
    if persisted.torrent_sha256 != torrent_sha256:
        raise TorrentVerificationError("persisted torrent digest does not match the build")
    return TorrentBuildResult(
        torrent_path=destination,
        infohash=infohash,
        sha256=payload.sha256,
        torrent_sha256=torrent_sha256,
        piece_length=piece_length,
        piece_count=verified.piece_count,
        file_size=payload.size,
        payload_path=payload_path_for(safe_release),
        torrent_bytes=torrent_bytes,
    )


def _expect_dict(value: object, *, description: str) -> dict[bytes, object]:
    if not isinstance(value, dict) or any(not isinstance(key, bytes) for key in value):
        raise TorrentVerificationError(f"{description} must be a bencode dictionary")
    return value


def _expect_bytes(value: object, *, description: str) -> bytes:
    if not isinstance(value, bytes):
        raise TorrentVerificationError(f"{description} must be a byte string")
    return value


def _expect_int(value: object, *, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TorrentVerificationError(f"{description} must be an integer")
    return value


def _decode_utf8(value: object, *, description: str) -> str:
    raw = _expect_bytes(value, description=description)
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise TorrentVerificationError(f"{description} is not valid UTF-8") from exc
    if unicodedata.normalize("NFC", decoded) != decoded:
        raise TorrentVerificationError(f"{description} is not canonical NFC text")
    return decoded


def _require_exact_keys(
    value: Mapping[bytes, object], expected: set[bytes], *, description: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(item.decode("ascii", "replace") for item in expected - actual)
        unexpected = sorted(item.decode("ascii", "replace") for item in actual - expected)
        raise TorrentVerificationError(
            f"{description} keys are not canonical; missing={missing}, unexpected={unexpected}"
        )


def _torrent_input_bytes(torrent: bytes | Path) -> bytes:
    if isinstance(torrent, bytes):
        if len(torrent) > _MAX_TORRENT_SIZE:
            raise TorrentVerificationError("torrent exceeds the 16 MiB safety limit")
        return torrent
    if isinstance(torrent, Path):
        return _read_guarded_bytes(
            torrent, description="torrent path", maximum=_MAX_TORRENT_SIZE
        )
    raise TypeError("torrent must be bytes or an absolute pathlib.Path")


def _normalized_sha256(value: str) -> str:
    normalized = value.casefold()
    if _SHA256_HEX.fullmatch(normalized) is None:
        raise TorrentVerificationError("expected_file_sha256 must contain 64 hex digits")
    return normalized


def verify_torrent(
    torrent: bytes | Path,
    *,
    expected_release_name: str | None = None,
    expected_file_sha256: str | None = None,
    payload_file: Path | None = None,
    expected_profile: TorrentProfile | Mapping[str, object] | None = None,
    expected_infohash: str | None = None,
) -> TorrentVerification:
    """Verify canonical structure and optionally re-hash the complete payload.

    A v1 torrent does not carry a SHA-256 payload digest.  Therefore an
    ``expected_file_sha256`` requires ``payload_file`` so the expectation is
    proven from bytes instead of trusted as metadata.
    """

    data = _torrent_input_bytes(torrent)
    try:
        decoded = bdecode(data)
    except BencodeError as exc:
        raise TorrentVerificationError(str(exc)) from exc
    root = _expect_dict(decoded, description="torrent")
    _require_exact_keys(root, {b"announce", b"info"}, description="torrent")
    announce_raw = _expect_bytes(root[b"announce"], description="announce")
    try:
        announce = announce_raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise TorrentVerificationError("announce URL must be ASCII") from exc
    try:
        _validate_announce_url(announce)
    except ValueError as exc:
        raise TorrentVerificationError(str(exc)) from exc

    info = _expect_dict(root[b"info"], description="info")
    _require_exact_keys(
        info,
        {b"files", b"name", b"piece length", b"pieces", b"private", b"source"},
        description="info",
    )
    release_name = _decode_utf8(info[b"name"], description="release name")
    try:
        validate_release_name(release_name)
    except TorrentSecurityError as exc:
        raise TorrentVerificationError(str(exc)) from exc
    if expected_release_name is not None:
        try:
            expected_name = validate_release_name(expected_release_name)
        except TorrentSecurityError as exc:
            raise TorrentVerificationError(str(exc)) from exc
        if release_name != expected_name:
            raise TorrentVerificationError("torrent release name does not match expected")

    if _expect_int(info[b"private"], description="private flag") != 1:
        raise TorrentVerificationError("torrent must have private=1")
    source_raw = _expect_bytes(info[b"source"], description="source")
    try:
        source = source_raw.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise TorrentVerificationError("source must be ASCII") from exc
    if _SAFE_SOURCE.fullmatch(source) is None:
        raise TorrentVerificationError("source is not a safe tracker source token")

    piece_length = _expect_int(info[b"piece length"], description="piece length")
    if (
        not _is_power_of_two(piece_length)
        or not _MIN_PROTOCOL_PIECE_SIZE
        <= piece_length
        <= _MAX_PROTOCOL_PIECE_SIZE
    ):
        raise TorrentVerificationError("piece length is outside the v1 safety bounds")
    pieces = _expect_bytes(info[b"pieces"], description="pieces")
    if not pieces or len(pieces) % 20:
        raise TorrentVerificationError("pieces must contain complete SHA-1 digests")

    files = info[b"files"]
    if not isinstance(files, list) or len(files) != 1:
        raise TorrentVerificationError("torrent must contain exactly one payload file")
    file_entry = _expect_dict(files[0], description="payload file")
    _require_exact_keys(file_entry, {b"length", b"path"}, description="payload file")
    file_size = _expect_int(file_entry[b"length"], description="payload length")
    if file_size <= 0:
        raise TorrentVerificationError("payload length must be positive")
    file_path = file_entry[b"path"]
    if not isinstance(file_path, list) or len(file_path) != 1:
        raise TorrentVerificationError("payload path must have exactly one component")
    filename = _decode_utf8(file_path[0], description="payload filename")
    if filename != f"{release_name}.mkv":
        raise TorrentVerificationError("payload filename is not canonical")
    payload_path = f"{release_name}/{filename}"
    piece_count = len(pieces) // 20
    if piece_count != _piece_count(file_size, piece_length):
        raise TorrentVerificationError("piece count does not match payload length")

    infohash = _sha1(bencode(info)).hexdigest()
    if expected_infohash is not None:
        normalized_infohash = expected_infohash.casefold()
        if not re.fullmatch(r"[0-9a-f]{40}", normalized_infohash):
            raise TorrentVerificationError("expected_infohash must contain 40 hex digits")
        if infohash != normalized_infohash:
            raise TorrentVerificationError("torrent infohash does not match expected")

    policy: TorrentProfile | None = None
    if expected_profile is not None:
        policy = _coerce_profile(expected_profile)
        if announce != policy.announce_url:
            raise TorrentVerificationError("announce URL does not match the profile")
        if source != policy.source:
            raise TorrentVerificationError("source does not match the profile")
        try:
            expected_piece_length = select_piece_size(file_size, policy)
        except TorrentError as exc:
            raise TorrentVerificationError(
                "payload violates the profile piece-count bounds"
            ) from exc
        if piece_length != expected_piece_length:
            raise TorrentVerificationError("piece length does not match the profile")

    expected_digest = (
        _normalized_sha256(expected_file_sha256)
        if expected_file_sha256 is not None
        else None
    )
    if expected_digest is not None and payload_file is None:
        raise TorrentVerificationError(
            "payload_file is required to prove expected_file_sha256"
        )
    payload_sha256: str | None = None
    if payload_file is not None:
        payload = _hash_payload(Path(payload_file), piece_length)
        if payload.size != file_size:
            raise TorrentVerificationError("payload file length does not match torrent")
        if payload.pieces != pieces:
            raise TorrentVerificationError("payload piece hashes do not match torrent")
        payload_sha256 = payload.sha256
        if expected_digest is not None and payload_sha256 != expected_digest:
            raise TorrentVerificationError("payload SHA-256 does not match expected")

    return TorrentVerification(
        infohash=infohash,
        sha256=payload_sha256,
        torrent_sha256=hashlib.sha256(data).hexdigest(),
        piece_length=piece_length,
        piece_count=piece_count,
        file_size=file_size,
        payload_path=payload_path,
        release_name=release_name,
        source=source,
    )


def verify_private_torrent(
    torrent: bytes | Path,
    **kwargs: object,
) -> TorrentVerification:
    """Compatibility-named wrapper around :func:`verify_torrent`."""

    return verify_torrent(torrent, **kwargs)  # type: ignore[arg-type]
