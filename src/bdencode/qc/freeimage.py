"""Freeimage.host API adapter with byte-for-byte verification."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from bdencode.secrets import SecretUnavailable, read_secret
from bdencode.utils import sha256_file

from .catbox import _validated_png, _verify_png_response
from .image_upload import ImageUploadError, UploadedImage


class FreeimageClient:
    provider_name = "freeimage"
    endpoint = "https://freeimage.host/api/1/upload"

    def __init__(
        self,
        *,
        credential_loader: Callable[..., str] = read_secret,
        client: httpx.Client | None = None,
        timeout: float = 120,
    ) -> None:
        self._credential_loader = credential_loader
        self._client = client
        self._timeout = timeout

    def upload_png(self, path: Path) -> UploadedImage:
        payload = _validated_png(path)
        key = ""
        data: dict[str, str] = {"action": "upload", "format": "json"}
        owned = self._client is None
        client = self._client or httpx.Client(
            timeout=self._timeout, follow_redirects=False
        )
        post_attempted = False
        close_error: Exception | None = None
        try:
            try:
                key = self._credential_loader("freeimage-api-key")
            except SecretUnavailable as exc:
                raise ImageUploadError(
                    "Freeimage credential is not configured",
                    provider=self.provider_name,
                ) from exc
            data["key"] = key
            try:
                post_attempted = True
                response = client.post(
                    self.endpoint,
                    data=data,
                    files={"source": (path.name, payload, "image/png")},
                    follow_redirects=False,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise ImageUploadError(
                    "Freeimage could not be reached before upload",
                    provider=self.provider_name,
                    allow_fallback=True,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ImageUploadError(
                    "Freeimage upload outcome is unknown; retry the same provider",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                raise ImageUploadError(
                    f"Freeimage HTTP {status}",
                    provider=self.provider_name,
                    allow_fallback=status == 429,
                    provider_may_have_committed=status >= 500,
                ) from exc
            try:
                document = response.json()
            except ValueError as exc:
                raise ImageUploadError(
                    "Freeimage returned malformed JSON after upload",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
            success = document.get("success") if isinstance(document, dict) else None
            status_code = (
                document.get("status_code") if isinstance(document, dict) else None
            )
            reported_success = (
                isinstance(document, dict)
                and type(status_code) is int
                and status_code == 200
                and isinstance(success, dict)
                and type(success.get("code")) is int
                and success.get("code") == 200
            )
            explicit_rejection = (
                isinstance(document, dict)
                and type(status_code) is int
                and status_code >= 400
                and isinstance(success, dict)
                and type(success.get("code")) is int
                and success.get("code") >= 400
            )
            if explicit_rejection:
                raise ImageUploadError(
                    "Freeimage did not report a successful upload",
                    provider=self.provider_name,
                    allow_fallback=status_code == 429 or status_code >= 500,
                )
            if not reported_success or not isinstance(document.get("image"), dict):
                raise ImageUploadError(
                    "Freeimage returned ambiguous upload status",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
            result: dict[str, Any] = document["image"]
            image_url = str(result.get("url") or "")
            viewer_url = str(result.get("url_viewer") or image_url)
            if (
                (bool(key) and (key in image_url or key in viewer_url))
                or not _allowed_url(image_url, direct_image=True)
                or not _allowed_url(viewer_url, direct_image=False)
            ):
                raise ImageUploadError(
                    "Freeimage returned an invalid URL",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
            try:
                downloaded = client.get(
                    image_url,
                    headers={"Accept": "image/png"},
                    follow_redirects=False,
                )
                downloaded.raise_for_status()
            except httpx.HTTPError as exc:
                raise ImageUploadError(
                    "Freeimage verification failed after upload; retry the same provider",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
        except ImageUploadError:
            raise
        except Exception as exc:
            raise ImageUploadError(
                "Freeimage upload or verification failed",
                provider=self.provider_name,
                provider_may_have_committed=post_attempted,
            ) from exc
        finally:
            active_error = sys.exc_info()[0] is not None
            data.clear()
            key = ""
            if owned:
                try:
                    client.close()
                except Exception as exc:
                    if not active_error:
                        close_error = exc
        if close_error is not None:
            raise ImageUploadError(
                "Freeimage cleanup failed after upload",
                provider=self.provider_name,
                provider_may_have_committed=True,
            ) from close_error
        try:
            _verify_png_response(downloaded, provider="Freeimage")
            local_hash = sha256_file(path)
            remote_hash = hashlib.sha256(downloaded.content).hexdigest()
            if local_hash != remote_hash:
                raise ImageUploadError(
                    "Freeimage round-trip changed the PNG bytes",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
        except ImageUploadError:
            raise
        except Exception as exc:
            raise ImageUploadError(
                "Freeimage local verification failed after upload",
                provider=self.provider_name,
                provider_may_have_committed=True,
            ) from exc
        return UploadedImage(
            provider=self.provider_name,
            image_url=image_url,
            viewer_url=viewer_url,
            local_sha256=local_hash,
            remote_sha256=remote_hash,
            bbcode=f"[url={viewer_url}][img]{image_url}[/img][/url]",
        )


def _allowed_url(value: str, *, direct_image: bool) -> bool:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    allowed_host = (
        host == "freeimage.host"
        or host.endswith(".freeimage.host")
        or host == "iili.io"
        or host.endswith(".iili.io")
    )
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port is None
        and allowed_host
        and bool(parsed.path)
        and (not direct_image or parsed.path.lower().endswith(".png"))
    )
