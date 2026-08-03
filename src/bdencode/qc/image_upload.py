"""Shared, secret-safe image upload contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
