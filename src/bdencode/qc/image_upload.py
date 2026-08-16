"""Shared, secret-safe image upload contracts."""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping, Protocol
from urllib.parse import urlsplit


IMAGE_UPLOAD_PROVIDERS: Final = frozenset({"imgbb", "catbox", "freeimage"})
UPLOADED_IMAGE_FIELDS: Final = frozenset(
    {
        "provider",
        "image_url",
        "viewer_url",
        "local_sha256",
        "remote_sha256",
        "bbcode",
    }
)
LEGACY_UPLOADED_IMAGE_FIELDS: Final = UPLOADED_IMAGE_FIELDS - {"provider"}

_SHA256_RE: Final = re.compile(r"[0-9a-fA-F]{64}\Z")
_UNSAFE_BBCODE_URL_RE: Final = re.compile(r"[\x00-\x20\x7f\[\]]")

__all__ = [
    "IMAGE_UPLOAD_PROVIDERS",
    "ImageUploadClient",
    "ImageUploadError",
    "LEGACY_UPLOADED_IMAGE_FIELDS",
    "UPLOADED_IMAGE_FIELDS",
    "UploadedImage",
    "build_upload_bbcode",
    "infer_upload_provider",
    "is_allowed_upload_url",
    "parse_uploaded_image",
    "parse_uploaded_image_checkpoint",
]


class ImageUploadError(RuntimeError):
    """An image host rejected or could not verify an upload.

    ``allow_fallback`` is true only when no image has yet been committed to the
    provider and trying the next configured host is safe.  When
    ``provider_may_have_committed`` is true, callers must durably pin the batch
    to ``provider`` before surfacing the error.  This also covers a successful
    POST whose verification subsequently failed: a later retry may use the same
    host, but must never fall through to another one.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        allow_fallback: bool = False,
        provider_may_have_committed: bool = False,
    ) -> None:
        if allow_fallback and provider_may_have_committed:
            raise ValueError(
                "an ambiguous or committed upload cannot allow provider fallback"
            )
        super().__init__(message)
        self.provider = provider
        self.allow_fallback = allow_fallback
        self.provider_may_have_committed = provider_may_have_committed


@dataclass(frozen=True, slots=True)
class UploadedImage:
    provider: str
    image_url: str
    viewer_url: str
    local_sha256: str
    remote_sha256: str
    bbcode: str


class ImageUploadClient(Protocol):
    provider_name: str

    def upload_png(self, path: Path) -> UploadedImage: ...


def parse_uploaded_image(
    raw: object,
    *,
    expected_local_sha256: str,
    legacy_provider: str | None = None,
    infer_legacy_provider: bool = False,
) -> UploadedImage:
    """Parse one persisted upload result using a fail-closed checkpoint policy.

    Current records must contain exactly the six fields in
    :data:`UPLOADED_IMAGE_FIELDS`.  A schema-v1 record may omit ``provider``
    only when the caller explicitly supplies ``legacy_provider`` or opts into
    unambiguous inference from both provider URLs.  Unknown fields are never
    carried through a migration.

    The stored BBCode is deliberately treated only as a required completion
    marker.  The returned value always contains newly constructed BBCode made
    from the validated URLs.
    """

    if not isinstance(raw, Mapping):
        raise ValueError("uploaded image checkpoint entry must be an object")
    raw_fields = frozenset(raw.keys())
    current_record = raw_fields == UPLOADED_IMAGE_FIELDS
    legacy_record = raw_fields == LEGACY_UPLOADED_IMAGE_FIELDS
    if not current_record and not legacy_record:
        raise ValueError("uploaded image checkpoint fields do not match the schema")

    expected_hash = _parse_sha256(
        expected_local_sha256,
        field="expected local SHA-256",
    )
    local_hash = _parse_sha256(raw["local_sha256"], field="local SHA-256")
    remote_hash = _parse_sha256(raw["remote_sha256"], field="remote SHA-256")
    if not hmac.compare_digest(local_hash, expected_hash) or not hmac.compare_digest(
        remote_hash, expected_hash
    ):
        raise ValueError("uploaded image hashes do not match the expected local file")

    image_url = _required_string(raw["image_url"], field="image URL")
    viewer_url = _required_string(raw["viewer_url"], field="viewer URL")
    raw_bbcode = raw["bbcode"]
    if not isinstance(raw_bbcode, str) or not raw_bbcode:
        raise ValueError("uploaded image BBCode must be a non-empty string")

    if current_record:
        provider = _parse_provider(raw["provider"])
        if legacy_provider is not None:
            explicit_provider = _parse_provider(legacy_provider)
            if provider != explicit_provider:
                raise ValueError("uploaded image provider conflicts with checkpoint")
    else:
        if legacy_provider is not None:
            provider = _parse_provider(legacy_provider)
        elif infer_legacy_provider:
            provider = infer_upload_provider(image_url, viewer_url)
        else:
            raise ValueError("legacy uploaded image has no trusted provider")

    if not is_allowed_upload_url(provider, image_url, direct_image=True):
        raise ValueError("uploaded image direct URL violates provider policy")
    if not is_allowed_upload_url(provider, viewer_url, direct_image=False):
        raise ValueError("uploaded image viewer URL violates provider policy")
    if provider == "catbox" and image_url != viewer_url:
        raise ValueError("Catbox direct and viewer URLs must match")

    return UploadedImage(
        provider=provider,
        image_url=image_url,
        viewer_url=viewer_url,
        local_sha256=local_hash,
        remote_sha256=remote_hash,
        bbcode=build_upload_bbcode(provider, image_url, viewer_url),
    )


# The longer name makes the checkpoint boundary explicit at call sites while
# retaining a compact parser name for direct users of this module.
parse_uploaded_image_checkpoint = parse_uploaded_image


def infer_upload_provider(image_url: str, viewer_url: str) -> str:
    """Return the sole provider whose direct/viewer policies accept both URLs."""

    matches = [
        provider
        for provider in sorted(IMAGE_UPLOAD_PROVIDERS)
        if is_allowed_upload_url(provider, image_url, direct_image=True)
        and is_allowed_upload_url(provider, viewer_url, direct_image=False)
        and (provider != "catbox" or image_url == viewer_url)
    ]
    if len(matches) != 1:
        raise ValueError("legacy uploaded image provider cannot be inferred uniquely")
    return matches[0]


def is_allowed_upload_url(
    provider: str,
    value: str,
    *,
    direct_image: bool,
) -> bool:
    """Apply the upload adapters' provider-specific URL policy.

    URL text that could alter the reconstructed BBCode is rejected in addition
    to the adapters' HTTPS, authority, host and path restrictions.
    """

    if provider not in IMAGE_UPLOAD_PROVIDERS or not isinstance(value, str):
        return False
    if not value or _UNSAFE_BBCODE_URL_RE.search(value):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        return False

    if provider == "catbox":
        return host == "files.catbox.moe" and parsed.path.lower().endswith(".png")
    if provider == "imgbb":
        if direct_image:
            return host == "i.ibb.co" and parsed.path.lower().endswith(".png")
        return (
            host == "i.ibb.co"
            or host == "ibb.co"
            or host.endswith(".ibb.co")
            or host == "imgbb.com"
            or host.endswith(".imgbb.com")
        )

    allowed_freeimage_host = (
        host == "freeimage.host"
        or host.endswith(".freeimage.host")
        or host == "iili.io"
        or host.endswith(".iili.io")
    )
    return allowed_freeimage_host and (
        not direct_image or parsed.path.lower().endswith(".png")
    )


def build_upload_bbcode(provider: str, image_url: str, viewer_url: str) -> str:
    """Build canonical BBCode from URLs that pass the public policy helpers."""

    parsed_provider = _parse_provider(provider)
    if not is_allowed_upload_url(parsed_provider, image_url, direct_image=True):
        raise ValueError("uploaded image direct URL violates provider policy")
    if not is_allowed_upload_url(parsed_provider, viewer_url, direct_image=False):
        raise ValueError("uploaded image viewer URL violates provider policy")
    if parsed_provider == "catbox":
        if image_url != viewer_url:
            raise ValueError("Catbox direct and viewer URLs must match")
        return f"[img]{image_url}[/img]"
    return f"[url={viewer_url}][img]{image_url}[/img][/url]"


def _required_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"uploaded image {field} must be a non-empty string")
    return value


def _parse_provider(value: object) -> str:
    if not isinstance(value, str) or value not in IMAGE_UPLOAD_PROVIDERS:
        raise ValueError("uploaded image provider is unsupported")
    return value


def _parse_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"uploaded image {field} must contain 64 hexadecimal digits")
    return value.lower()
