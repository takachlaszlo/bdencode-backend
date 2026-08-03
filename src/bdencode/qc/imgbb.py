"""ImgBB upload adapter with byte-for-byte round-trip verification."""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from bdencode.secrets import SecretUnavailable, read_secret
from bdencode.utils import sha256_file

from .image_upload import ImageUploadError, UploadedImage


class ImgBBClient:
    provider_name = "imgbb"
    endpoint = "https://api.imgbb.com/1/upload"

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

    def upload_png(self, path: Path, *, expiration: int | None = None) -> UploadedImage:
        if path.suffix.lower() != ".png":
            raise ValueError("only PNG evidence may be uploaded")
        payload = path.read_bytes()
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("file does not contain PNG data")
        key = ""
        data: dict[str, str] = {}
        if expiration is not None:
            if not 60 <= expiration <= 15_552_000:
                raise ValueError(
                    "ImgBB expiration must be between 60 and 15552000 seconds"
                )

        owned = self._client is None
        client = self._client or httpx.Client(
            timeout=self._timeout, follow_redirects=False
        )
        post_attempted = False
        close_error: Exception | None = None
        try:
            try:
                key = self._credential_loader("imgbb-api-key")
            except SecretUnavailable as exc:
                raise ImageUploadError(
                    "ImgBB credential is not configured",
                    provider=self.provider_name,
                    allow_fallback=True,
                ) from exc
            data.update(
                {
                    "key": key,
                    "name": path.stem,
                    "image": base64.b64encode(payload).decode("ascii"),
                }
            )
            if expiration is not None:
                data["expiration"] = str(expiration)
            try:
                post_attempted = True
                response = client.post(
                    self.endpoint,
                    data=data,
                    follow_redirects=False,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise ImageUploadError(
                    "ImgBB could not be reached before upload",
                    provider=self.provider_name,
                    allow_fallback=True,
                ) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ImageUploadError(
                    "ImgBB upload outcome is unknown; retry the same provider",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _api_error_detail(exc.response)
                allow_fallback = exc.response.status_code == 429 or detail[1] == 100
                message = f"ImgBB HTTP {exc.response.status_code}"
                if detail[0]:
                    safe_detail = (
                        detail[0].replace(key, "<redacted>") if key else detail[0]
                    )
                    message += f": {safe_detail}"
                raise ImageUploadError(
                    message,
                    provider=self.provider_name,
                    allow_fallback=allow_fallback,
                    provider_may_have_committed=(
                        exc.response.status_code >= 500 and not allow_fallback
                    ),
                ) from exc
            try:
                document = response.json()
            except ValueError as exc:
                raise ImageUploadError(
                    "ImgBB returned malformed JSON after upload",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
            if not isinstance(document, dict):
                raise ImageUploadError(
                    "ImgBB returned malformed upload data",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
            success = document.get("success")
            status = document.get("status")
            explicit_rejection = success is False
            legacy_rejection = (
                "success" not in document
                and type(status) is int
                and status >= 400
                and _has_explicit_error(document)
            )
            if success is not True and not (explicit_rejection or legacy_rejection):
                raise ImageUploadError(
                    "ImgBB returned ambiguous upload status",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
            if explicit_rejection or legacy_rejection:
                detail, code = _document_error_detail(document)
                safe_detail = detail.replace(key, "<redacted>") if key else detail
                message = "ImgBB did not report a successful upload"
                if safe_detail:
                    message += f": {safe_detail}"
                raise ImageUploadError(
                    message,
                    provider=self.provider_name,
                    allow_fallback=(
                        code == 100
                        or status == 429
                        or (type(status) is int and status >= 500)
                        or _transient_error_text(detail)
                    ),
                )
            if not isinstance(document.get("data"), dict):
                raise ImageUploadError(
                    "ImgBB returned malformed upload data",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
            result: dict[str, Any] = document["data"]
            image_url = str(result.get("url") or result.get("display_url") or "")
            viewer_url = str(result.get("url_viewer") or image_url)
            if (
                (bool(key) and (key in image_url or key in viewer_url))
                or not _allowed_imgbb_url(image_url, direct_image=True)
                or not _allowed_imgbb_url(viewer_url, direct_image=False)
            ):
                raise ImageUploadError(
                    "ImgBB returned an invalid URL",
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
                    "ImgBB verification failed after upload; retry the same provider",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                ) from exc
        except ImageUploadError:
            raise
        except Exception as exc:
            raise ImageUploadError(
                "ImgBB upload or verification failed",
                provider=self.provider_name,
                provider_may_have_committed=post_attempted,
            ) from exc
        finally:
            active_error = sys.exc_info()[0] is not None
            # Do not retain a second in-memory reference to the credential.
            data.clear()
            key = ""
            if owned:
                try:
                    client.close()
                except Exception as exc:
                    # Cleanup must never replace a more useful upload error.
                    if not active_error:
                        close_error = exc
        if close_error is not None:
            raise ImageUploadError(
                "ImgBB cleanup failed after upload",
                provider=self.provider_name,
                provider_may_have_committed=True,
            ) from close_error
        try:
            _verify_png_response(downloaded)
            local_hash = sha256_file(path)
            remote_hash = hashlib.sha256(downloaded.content).hexdigest()
            if local_hash != remote_hash:
                raise ImageUploadError(
                    "ImgBB round-trip changed the PNG bytes",
                    provider=self.provider_name,
                    provider_may_have_committed=True,
                )
        except ImageUploadError:
            raise
        except Exception as exc:
            raise ImageUploadError(
                "ImgBB local verification failed after upload",
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


def _api_error_detail(response: httpx.Response) -> tuple[str, int | None]:
    """Return a bounded provider message without request data or credentials."""

    try:
        document = response.json()
    except ValueError:
        return "", None
    return _document_error_detail(document)


def _document_error_detail(document: Any) -> tuple[str, int | None]:
    error = document.get("error") if isinstance(document, dict) else None
    if isinstance(error, dict):
        raw_message = error.get("message")
        raw_code = error.get("code")
        message = str(raw_message)[:300] if raw_message else ""
        code = raw_code if isinstance(raw_code, int) else None
        return message, code
    if isinstance(error, str):
        return error[:300], None
    return "", None


def _has_explicit_error(document: dict[str, Any]) -> bool:
    error = document.get("error")
    if isinstance(error, dict):
        return bool(error.get("message")) or type(error.get("code")) is int
    return isinstance(error, str) and bool(error.strip())


def _allowed_imgbb_url(value: str, *, direct_image: bool) -> bool:
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
    ):
        return False
    if direct_image:
        return host == "i.ibb.co" and parsed.path.lower().endswith(".png")
    return (
        host == "i.ibb.co"
        or host == "ibb.co"
        or host.endswith(".ibb.co")
        or host == "imgbb.com"
        or host.endswith(".imgbb.com")
    )


def _verify_png_response(response: httpx.Response) -> None:
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if content_type.lower() != "image/png" or not response.content.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        raise ImageUploadError(
            "ImgBB direct URL did not return PNG data",
            provider="imgbb",
            provider_may_have_committed=True,
        )


def _transient_error_text(value: str) -> bool:
    normalized = value.casefold()
    return any(
        marker in normalized
        for marker in (
            "maintenance",
            "temporar",
            "unavailable",
            "timeout",
            "try again",
            "rate limit",
            "overloaded",
        )
    )


def comparison_bbcode(
    title: str, uploads: list[tuple[str, UploadedImage, UploadedImage]]
) -> str:
    lines = [f"[b]{title}[/b]"]
    for label, source, encode in uploads:
        lines.extend(
            [
                f"[b]{label} — Source[/b]",
                source.bbcode,
                f"[b]{label} — Encode[/b]",
                encode.bbcode,
            ]
        )
    return "\n".join(lines) + "\n"
