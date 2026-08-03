"""Catbox upload adapter with optional account association."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from bdencode.secrets import SecretUnavailable, read_secret
from bdencode.utils import sha256_file

from .image_upload import ImageUploadError, UploadedImage


class CatboxClient:
    provider_name = "catbox"
    endpoint = "https://catbox.moe/user/api.php"

    def __init__(
        self,
        *,
        userhash: str | None = None,
        credential_loader: Callable[..., str] = read_secret,
        client: httpx.Client | None = None,
        timeout: float = 120,
    ) -> None:
        self._userhash = userhash
        self._credential_loader = credential_loader
        self._client = client
        self._timeout = timeout

    def upload_png(self, path: Path) -> UploadedImage:
        payload = _validated_png(path)
        userhash = self._userhash
        if userhash is None:
            try:
                userhash = self._credential_loader("catbox-userhash")
            except SecretUnavailable:
                userhash = None
            except Exception as exc:
                raise ImageUploadError(
                    "Catbox credential loading failed",
                    provider=self.provider_name,
                ) from exc

        data = {"reqtype": "fileupload"}
        if userhash:
            data["userhash"] = userhash
        owned = self._client is None
        client = self._client or httpx.Client(
            timeout=self._timeout, follow_redirects=False
        )
        post_attempted = False
        close_error: Exception | None = None
        try:
            try:
                post_attempted = True
                response = client.post(
                    self.endpoint,
                    data=data,
                    files={"fileToUpload": (path.name, payload, "image/png")},
                    follow_redirects=False,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise ImageUploadError(
                    "Catbox could not be reached before upload",
                    provider=self.provider_name,
                    allow_fallback=True,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ImageUploadError(
                    "Catbox upload outcome is unknown; retry the same provider",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                raise ImageUploadError(
                    f"Catbox HTTP {status}",
                    provider=self.provider_name,
                    allow_fallback=status == 429,
                    provider_may_have_committed=status >= 500,
                ) from exc
            image_url = response.text.strip()
            if (bool(userhash) and userhash in image_url) or not _allowed_catbox_url(
                image_url
            ):
                raise ImageUploadError(
                    "Catbox returned an invalid image URL",
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
                    "Catbox verification failed after upload; retry the same provider",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
        except ImageUploadError:
            raise
        except Exception as exc:
            raise ImageUploadError(
                "Catbox upload or verification failed",
                provider=self.provider_name,
                provider_may_have_committed=post_attempted,
            ) from exc
        finally:
            active_error = sys.exc_info()[0] is not None
            data.clear()
            userhash = ""
            if owned:
                try:
                    client.close()
                except Exception as exc:
                    if not active_error:
                        close_error = exc
        if close_error is not None:
            raise ImageUploadError(
                "Catbox cleanup failed after upload",
                provider=self.provider_name,
                provider_may_have_committed=True,
            ) from close_error
        try:
            _verify_png_response(downloaded, provider="Catbox")
            local_hash = sha256_file(path)
            remote_hash = hashlib.sha256(downloaded.content).hexdigest()
            if local_hash != remote_hash:
                raise ImageUploadError(
                    "Catbox round-trip changed the PNG bytes",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
        except ImageUploadError:
            raise
        except Exception as exc:
            raise ImageUploadError(
                "Catbox local verification failed after upload",
                provider=self.provider_name,
                provider_may_have_committed=True,
            ) from exc
        return UploadedImage(
            provider=self.provider_name,
            image_url=image_url,
            viewer_url=image_url,
            local_sha256=local_hash,
            remote_sha256=remote_hash,
            bbcode=f"[img]{image_url}[/img]",
        )


def _validated_png(path: Path) -> bytes:
    if path.suffix.lower() != ".png":
        raise ValueError("only PNG evidence may be uploaded")
    payload = path.read_bytes()
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("file does not contain PNG data")
    return payload


def _allowed_catbox_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "files.catbox.moe"
        and parsed.username is None
        and parsed.password is None
        and port is None
        and parsed.path.lower().endswith(".png")
    )


def _verify_png_response(response: httpx.Response, *, provider: str) -> None:
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if content_type.lower() != "image/png" or not response.content.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        raise ImageUploadError(
            f"{provider} direct URL did not return PNG data",
            provider=provider.lower(),
            provider_may_have_committed=True,
        )
