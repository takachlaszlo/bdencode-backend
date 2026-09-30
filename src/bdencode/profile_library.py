"""A small, shareable library of named encoding profiles.

A profile is a named bundle of *portable* encoder choices: the concrete
``EncoderSettings`` overrides (CRF, preset, psychovisual tools, noise
reduction ...), optionally an automatic-CRF target and a dynamic-HDR policy.
Anything that belongs to one particular disc - colour metadata, HDR10
mastering values, the codec profile/level, bit depth, bitstream policy - is
deliberately not portable and is rejected on import, exactly like the fields the
AI adviser may not touch.

Profiles are stored one JSON file each under ``<state>/profile-library`` and are
exchanged as ``bdencode-profile`` (one profile) or ``bdencode-profile-bundle``
documents.  Every import is validated by building real ``EncoderSettings``, so a
shared file can never smuggle an unsupported or unsafe encoder parameter into a
job.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .crf_search import AutoCrfConfig, CrfSearchError
from .hdr_dynamic import DynamicHdrError, DynamicHdrMode, parse_mode
from .media.profiles import DetailLevel, VideoEncoder, recommended_profile
from .utils import atomic_write_json

PROFILE_FORMAT = "bdencode-profile"
BUNDLE_FORMAT = "bdencode-profile-bundle"
FORMAT_VERSION = 1
MAX_PROFILES = 200
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_SETTINGS = 60

# Disc-specific or bitstream-policy fields that a shared profile must not carry.
NON_PORTABLE_SETTINGS = frozenset(
    {
        "encoder",
        "detail_level",
        "profile",
        "level",
        "bit_depth",
        "pixel_format",
        "color",
        "vbv",
        "hdr10",
        "aud",
        "repeat_headers",
        "annexb",
    }
)
# One API process serves requests from a thread pool; serialize the
# check-then-write sequences so two saves of the same name cannot both "win".
_LOCK = threading.RLock()
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PROFILE_KEYS = {
    "format",
    "version",
    "id",
    "name",
    "description",
    "encoder",
    "detail_level",
    "settings",
    "auto_crf",
    "dynamic_hdr",
    "created_at",
    "updated_at",
}


class ProfileLibraryError(ValueError):
    """A profile or bundle is invalid (``code`` is stable for API clients)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProfileNotFound(ProfileLibraryError):
    def __init__(self, profile_id: str) -> None:
        super().__init__("not_found", f"profile not found: {profile_id}")


class ProfileExists(ProfileLibraryError):
    def __init__(self, profile_id: str) -> None:
        super().__init__("exists", f"a profile with id {profile_id!r} already exists")


def slugify(name: str) -> str:
    """ASCII slug of a profile name (``"UHD szemcsés film"`` -> ``uhd-szemcses-film``)."""

    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:64].strip("-")
    return slug or "profile"


def _clean_text(value: object, field: str, *, limit: int, required: bool) -> str:
    if value is None and not required:
        return ""
    if type(value) is not str:
        raise ProfileLibraryError("invalid", f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise ProfileLibraryError("invalid", f"{field} is required")
    if len(text) > limit:
        raise ProfileLibraryError("invalid", f"{field} is longer than {limit} characters")
    if any(unicodedata.category(char).startswith("C") and char not in "\n" for char in text):
        raise ProfileLibraryError("invalid", f"{field} contains control characters")
    return text


@dataclass(frozen=True, slots=True)
class LibraryProfile:
    id: str
    name: str
    description: str
    encoder: str
    detail_level: str
    settings: dict[str, Any]
    auto_crf: dict[str, Any] | None
    dynamic_hdr: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "format": PROFILE_FORMAT,
            "version": FORMAT_VERSION,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "encoder": self.encoder,
            "detail_level": self.detail_level,
            "settings": dict(self.settings),
            "dynamic_hdr": self.dynamic_hdr,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.auto_crf is not None:
            document["auto_crf"] = dict(self.auto_crf)
        return document

    def selection_fragment(self) -> dict[str, Any]:
        """What a selection editor merges into ``selection.video``."""

        fragment: dict[str, Any] = {
            "detail_level": self.detail_level,
            "settings": dict(self.settings),
        }
        if self.auto_crf is not None:
            fragment["auto_crf"] = dict(self.auto_crf)
        if self.dynamic_hdr != DynamicHdrMode.DISCARD.value:
            fragment["dynamic_hdr"] = self.dynamic_hdr
        return fragment


def validate_profile(
    document: object,
    *,
    profile_id: str | None = None,
    now: datetime | None = None,
    created_at: str | None = None,
) -> LibraryProfile:
    """Validate a ``bdencode-profile`` document and return the normalized profile."""

    if not isinstance(document, Mapping):
        raise ProfileLibraryError("invalid", "a profile must be a JSON object")
    unknown = set(document) - _PROFILE_KEYS
    if unknown:
        raise ProfileLibraryError(
            "invalid", "unknown profile field(s): " + ", ".join(sorted(map(str, unknown)))
        )
    if document.get("format", PROFILE_FORMAT) != PROFILE_FORMAT:
        raise ProfileLibraryError("invalid", f"format must be {PROFILE_FORMAT!r}")
    if document.get("version", FORMAT_VERSION) != FORMAT_VERSION:
        raise ProfileLibraryError(
            "unsupported_version", f"only profile version {FORMAT_VERSION} is supported"
        )
    name = _clean_text(document.get("name"), "name", limit=80, required=True)
    if "\n" in name:
        raise ProfileLibraryError("invalid", "name must be a single line")
    description = _clean_text(
        document.get("description"), "description", limit=500, required=False
    )
    try:
        encoder = VideoEncoder(document.get("encoder"))
        detail = DetailLevel(document.get("detail_level", DetailLevel.ADVANCED.value))
    except ValueError as exc:
        raise ProfileLibraryError(
            "invalid", "encoder must be x264 or x265 and detail_level a known level"
        ) from exc

    settings = document.get("settings", {})
    if not isinstance(settings, Mapping) or not all(type(key) is str for key in settings):
        raise ProfileLibraryError("invalid", "settings must be an object")
    if len(settings) > MAX_SETTINGS:
        raise ProfileLibraryError("invalid", f"at most {MAX_SETTINGS} settings are allowed")
    blocked = sorted(set(settings) & NON_PORTABLE_SETTINGS)
    if blocked:
        raise ProfileLibraryError(
            "not_portable",
            "these settings belong to one disc or to the bitstream policy and cannot "
            "be shared: " + ", ".join(blocked),
        )
    try:
        # Building real settings proves every value is supported by the encoder.
        recommended_profile(encoder, detail_level=detail, overrides=dict(settings))
    except (TypeError, ValueError) as exc:
        raise ProfileLibraryError("invalid_settings", f"invalid settings: {exc}") from exc

    auto_crf: dict[str, Any] | None = None
    if document.get("auto_crf") is not None:
        try:
            auto_crf = AutoCrfConfig.from_mapping(document["auto_crf"]).to_dict()
        except CrfSearchError as exc:
            raise ProfileLibraryError("invalid_auto_crf", str(exc)) from exc
    try:
        dynamic = parse_mode(document.get("dynamic_hdr")).value
    except DynamicHdrError as exc:
        raise ProfileLibraryError("invalid_dynamic_hdr", str(exc)) from exc

    stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    return LibraryProfile(
        id=profile_id or slugify(name),
        name=name,
        description=description,
        encoder=encoder.value,
        detail_level=detail.value,
        settings=dict(settings),
        auto_crf=auto_crf,
        dynamic_hdr=dynamic,
        created_at=created_at or stamp,
        updated_at=stamp,
    )


class ProfileLibrary:
    """File-backed profile store (one JSON file per profile)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory).expanduser()

    def _path(self, profile_id: str) -> Path:
        if not _ID_RE.fullmatch(profile_id):
            raise ProfileLibraryError("invalid", "profile id must be lowercase letters, digits and dashes")
        return self.directory / f"{profile_id}.json"

    def list(self) -> list[LibraryProfile]:
        if not self.directory.is_dir():
            return []
        profiles: list[LibraryProfile] = []
        for path in sorted(self.directory.glob("*.json")):
            if not _ID_RE.fullmatch(path.stem):
                continue
            try:
                profiles.append(self._load(path))
            except ProfileLibraryError:
                continue  # a hand-edited or damaged file must not hide the rest
        return sorted(profiles, key=lambda item: (item.name.casefold(), item.id))

    def _load(self, path: Path) -> LibraryProfile:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProfileLibraryError("invalid", f"unreadable profile file: {path.name}") from exc
        if not isinstance(document, Mapping):
            raise ProfileLibraryError("invalid", "profile file is not an object")
        return validate_profile(
            {key: value for key, value in document.items() if key != "id"},
            profile_id=path.stem,
            now=_parse_stamp(document.get("updated_at")),
            created_at=str(document.get("created_at") or ""),
        )

    def get(self, profile_id: str) -> LibraryProfile:
        path = self._path(profile_id)
        if not path.is_file():
            raise ProfileNotFound(profile_id)
        return self._load(path)

    def save(
        self,
        document: object,
        *,
        overwrite: bool = False,
        profile_id: str | None = None,
    ) -> LibraryProfile:
        with _LOCK:
            return self._save(document, overwrite=overwrite, profile_id=profile_id)

    def _save(
        self,
        document: object,
        *,
        overwrite: bool,
        profile_id: str | None,
    ) -> LibraryProfile:
        candidate = validate_profile(document, profile_id=profile_id)
        path = self._path(candidate.id)
        existing = path.is_file()
        if existing and not overwrite:
            raise ProfileExists(candidate.id)
        if not existing and len(list(self.directory.glob("*.json"))) >= MAX_PROFILES:
            raise ProfileLibraryError("limit", f"the library is limited to {MAX_PROFILES} profiles")
        if existing:
            candidate = validate_profile(
                document,
                profile_id=candidate.id,
                created_at=self.get(candidate.id).created_at,
            )
        self.directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        atomic_write_json(path, candidate.to_dict())
        return candidate

    def delete(self, profile_id: str) -> None:
        with _LOCK:
            path = self._path(profile_id)
            if not path.is_file():
                raise ProfileNotFound(profile_id)
            path.unlink()

    def export(self, profile_id: str) -> dict[str, Any]:
        return self.get(profile_id).to_dict()

    def export_bundle(self) -> dict[str, Any]:
        return {
            "format": BUNDLE_FORMAT,
            "version": FORMAT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "profiles": [item.to_dict() for item in self.list()],
        }

    def import_document(
        self, document: object, *, on_conflict: str = "rename"
    ) -> dict[str, Any]:
        with _LOCK:
            return self._import_document(document, on_conflict=on_conflict)

    def _import_document(
        self, document: object, *, on_conflict: str
    ) -> dict[str, Any]:
        """Import a profile or bundle; invalid entries are reported, not fatal.

        ``on_conflict`` is ``rename`` (keep both, suffix the id), ``skip`` or
        ``overwrite``.  The ids inside the file are ignored: they are derived
        from the names so a shared file cannot choose a path.
        """

        if on_conflict not in {"rename", "skip", "overwrite"}:
            raise ProfileLibraryError("invalid", "on_conflict must be rename, skip or overwrite")
        try:
            size = len(json.dumps(document))
        except (TypeError, ValueError) as exc:
            raise ProfileLibraryError("invalid", "the document is not valid JSON data") from exc
        if size > MAX_DOCUMENT_BYTES:
            raise ProfileLibraryError("too_large", "the document is larger than 512 KiB")
        if not isinstance(document, Mapping):
            raise ProfileLibraryError("invalid", "the document must be a JSON object")
        if document.get("format") == BUNDLE_FORMAT:
            if document.get("version") != FORMAT_VERSION:
                raise ProfileLibraryError("unsupported_version", "unsupported bundle version")
            entries = document.get("profiles")
            if not isinstance(entries, list):
                raise ProfileLibraryError("invalid", "a bundle needs a profiles array")
        else:
            entries = [document]
        imported: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        for index, entry in enumerate(entries):
            label = (
                str(entry.get("name"))[:80] if isinstance(entry, Mapping) else f"entry {index + 1}"
            )
            try:
                clean = _shareable(entry)
                candidate = validate_profile(clean)
                target = candidate.id
                if self._path(target).is_file():
                    if on_conflict == "skip":
                        skipped.append({"name": candidate.name, "id": target})
                        continue
                    if on_conflict == "rename":
                        target = self._free_id(candidate.id)
                saved = self.save(
                    clean, overwrite=on_conflict == "overwrite", profile_id=target
                )
                imported.append({"name": saved.name, "id": saved.id})
            except ProfileLibraryError as exc:
                errors.append({"name": label, "code": exc.code, "message": str(exc)})
        return {"imported": imported, "skipped": skipped, "errors": errors}

    def _free_id(self, base: str) -> str:
        for number in range(2, 1000):
            suffix = f"-{number}"
            candidate = f"{base[: 64 - len(suffix)]}{suffix}"
            if not self._path(candidate).exists():
                return candidate
        raise ProfileLibraryError("limit", "no free profile id")


def _shareable(entry: object) -> object:
    """Drop the machine-local bookkeeping of a shared profile (ids, timestamps)."""

    if not isinstance(entry, Mapping):
        return entry
    return {
        key: value
        for key, value in entry.items()
        if key not in {"id", "created_at", "updated_at"}
    }


def _parse_stamp(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
