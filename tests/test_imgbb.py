from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from bdencode.qc.imgbb import ImageUploadError, ImgBBClient


PNG = b"\x89PNG\r\n\x1a\nmock-png-body"


def test_imgbb_upload_round_trip_without_key_in_url(tmp_path: Path) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["url"] = str(request.url)
            seen["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "url": "https://cdn.example/proof.png",
                        "url_viewer": "https://viewer.example/proof",
                        "delete_url": "https://delete.example/token",
                    },
                },
            )
        return httpx.Response(200, content=PNG)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = ImgBBClient(
        credential_loader=lambda _: "top-secret", client=client
    ).upload_png(path)
    assert "top-secret" not in seen["url"]
    assert hashlib.sha256(PNG).hexdigest() == result.remote_sha256
    assert result.bbcode.startswith("[url=https://viewer.example")
    assert not hasattr(result, "delete_url")
    assert b"top-secret" in seen["body"]


def test_imgbb_rejects_changed_download(tmp_path: Path) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"url": "https://cdn.example/proof.png"},
                },
            )
        return httpx.Response(200, content=PNG + b"changed")

    with pytest.raises(ImageUploadError, match="changed"):
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)
