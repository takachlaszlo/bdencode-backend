"""ImgBB upload adapter with byte-for-byte round-trip verification."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from bdencode.secrets import read_secret
from bdencode.utils import sha256_file


class ImageUploadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UploadedImage:
    image_url: str
    viewer_url: str
    local_sha256: str
    remote_sha256: str
    bbcode: str


class ImgBBClient:
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
        key = self._credential_loader("imgbb-api-key")
        data: dict[str, str] = {
            "key": key,
            "name": path.stem,
            "image": base64.b64encode(payload).decode("ascii"),
        }
        if expiration is not None:
            if not 60 <= expiration <= 15_552_000:
                raise ValueError(
                    "ImgBB expiration must be between 60 and 15552000 seconds"
                )
            data["expiration"] = str(expiration)

        owned = self._client is None
        client = self._client or httpx.Client(
            timeout=self._timeout, follow_redirects=True
        )
        try:
            response = client.post(self.endpoint, data=data)
            response.raise_for_status()
            document = response.json()
            if not document.get("success") or not isinstance(
                document.get("data"), dict
            ):
                raise ImageUploadError("ImgBB did not report a successful upload")
            result: dict[str, Any] = document["data"]
            image_url = str(result.get("url") or result.get("display_url") or "")
            viewer_url = str(result.get("url_viewer") or image_url)
            if not image_url.startswith("https://") or not viewer_url.startswith(
                "https://"
            ):
                raise ImageUploadError("ImgBB returned an invalid URL")
            downloaded = client.get(image_url)
            downloaded.raise_for_status()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ImageUploadError("ImgBB upload or verification failed") from exc
        finally:
            # Do not retain a second in-memory reference to the credential.
            data.clear()
            key = ""
            if owned:
                client.close()

        import hashlib

        local_hash = sha256_file(path)
        remote_hash = hashlib.sha256(downloaded.content).hexdigest()
        if local_hash != remote_hash:
            raise ImageUploadError("ImgBB round-trip changed the PNG bytes")
        return UploadedImage(
            image_url=image_url,
            viewer_url=viewer_url,
            local_sha256=local_hash,
            remote_sha256=remote_hash,
            bbcode=f"[url={viewer_url}][img]{image_url}[/img][/url]",
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
