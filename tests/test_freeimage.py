from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from bdencode.qc.freeimage import FreeimageClient
from bdencode.qc.imgbb import ImageUploadError, UploadedImage


PNG = b"\x89PNG\r\n\x1a\nfreeimage-contract-png"


def _png(tmp_path: Path) -> Path:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    return path


def _success_document(
    *,
    image_url: str = "https://iili.io/proof.png",
    viewer_url: str = "https://freeimage.host/i/proof",
) -> dict[str, Any]:
    return {
        "status_code": 200,
        "success": {"message": "image uploaded", "code": 200},
        "image": {
            "name": "proof",
            "extension": "png",
            "mime": "image/png",
            "url": image_url,
            "url_viewer": viewer_url,
        },
        "status_txt": "OK",
    }


def test_freeimage_upload_uses_multipart_secret_and_verifies_direct_image(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)
    api_key = "freeimage-private-api-key"
    credential_names: list[str] = []
    seen_urls: list[str] = []

    def credential_loader(name: str) -> str:
        credential_names.append(name)
        return api_key

    def handler(request: httpx.Request) -> httpx.Response:
        request_url = str(request.url)
        seen_urls.append(request_url)
        assert api_key not in request_url
        if request.method == "POST":
            content_type = request.headers.get("content-type", "")
            assert content_type.startswith("multipart/form-data; boundary=")
            body = request.content
            assert b'name="key"' in body
            assert api_key.encode() in body
            assert b'name="action"' in body
            assert b"upload" in body
            assert b'name="format"' in body
            assert b"json" in body
            assert b'name="source"' in body
            assert b'filename="proof.png"' in body
            assert PNG in body
            return httpx.Response(200, json=_success_document())
        assert request.method == "GET"
        assert request_url == "https://iili.io/proof.png"
        return httpx.Response(
            200,
            content=PNG,
            headers={"content-type": "image/png"},
        )

    uploader = FreeimageClient(
        credential_loader=credential_loader,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = uploader.upload_png(path)

    digest = hashlib.sha256(PNG).hexdigest()
    assert uploader.provider_name == "freeimage"
    assert credential_names == ["freeimage-api-key"]
    assert isinstance(result, UploadedImage)
    assert result.image_url == "https://iili.io/proof.png"
    assert result.viewer_url == "https://freeimage.host/i/proof"
    assert result.local_sha256 == digest
    assert result.remote_sha256 == digest
    assert result.bbcode == (
        "[url=https://freeimage.host/i/proof][img]https://iili.io/proof.png[/img][/url]"
    )
    assert all(api_key not in url for url in seen_urls)
    assert api_key not in result.image_url
    assert api_key not in result.viewer_url
    assert api_key not in result.bbcode


@pytest.mark.parametrize(
    "document",
    [
        {"status_code": 400, "status_txt": "Bad request"},
        {
            **_success_document(),
            "status_code": 400,
            "success": {"message": "upload rejected", "code": 400},
        },
        {"status_code": 200, "success": {"code": 200}},
        {
            "status_code": 200,
            "success": {"code": 200},
            "image": {"url": "not-a-secure-url"},
        },
    ],
)
def test_freeimage_rejects_malformed_upload_response(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json=document)

    with pytest.raises(ImageUploadError):
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)


@pytest.mark.parametrize(
    "document",
    (
        {},
        [],
        {"status_code": 200},
        {"status_code": 200, "success": {}},
        {"status_code": 200, "success": {"code": 200}},
        {
            "status_code": 200.0,
            "success": {"code": 200},
            "image": {"url": "https://iili.io/proof.png"},
        },
    ),
)
def test_freeimage_malformed_2xx_status_is_commit_ambiguous(
    tmp_path: Path, document: object
) -> None:
    path = _png(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document)

    with pytest.raises(ImageUploadError, match="ambiguous") as error:
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_freeimage_explicit_temporary_rejection_remains_safe(tmp_path: Path) -> None:
    path = _png(tmp_path)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status_code": 503,
                "success": {"message": "maintenance", "code": 503},
            },
        )

    with pytest.raises(ImageUploadError) as error:
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.allow_fallback is True
    assert error.value.provider_may_have_committed is False


def test_freeimage_rejects_api_key_in_returned_url(tmp_path: Path) -> None:
    path = _png(tmp_path)
    api_key = "must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(
            200,
            json=_success_document(
                image_url=f"https://iili.io/proof.png?key={api_key}"
            ),
        )

    with pytest.raises(ImageUploadError) as error:
        FreeimageClient(
            credential_loader=lambda _: api_key,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider_may_have_committed is True


def test_freeimage_rejects_api_key_in_returned_viewer_url(tmp_path: Path) -> None:
    path = _png(tmp_path)
    api_key = "must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(
            200,
            json=_success_document(viewer_url=f"https://freeimage.host/i/{api_key}"),
        )

    with pytest.raises(ImageUploadError) as error:
        FreeimageClient(
            credential_loader=lambda _: api_key,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider_may_have_committed is True


def test_freeimage_rejects_changed_direct_download(tmp_path: Path) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_success_document())
        return httpx.Response(
            200,
            content=PNG + b"changed",
            headers={"content-type": "image/png"},
        )

    with pytest.raises(ImageUploadError, match="changed"):
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)


def test_freeimage_rejects_upload_redirect_without_leaking_key(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://attacker.invalid/collect"},
        )

    with pytest.raises(ImageUploadError) as error:
        FreeimageClient(
            credential_loader=lambda _: "private-key",
            client=httpx.Client(
                transport=httpx.MockTransport(handler), follow_redirects=True
            ),
        ).upload_png(path)

    assert len(requests) == 1
    assert requests[0].url.host == "freeimage.host"
    assert error.value.allow_fallback is False


def test_freeimage_verification_failure_never_switches_provider(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_success_document())
        return httpx.Response(503)

    with pytest.raises(ImageUploadError) as error:
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_freeimage_ambiguous_post_outcome_requires_a_durable_provider_pin(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("upload outcome unknown", request=request)

    with pytest.raises(ImageUploadError) as error:
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider == "freeimage"
    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_freeimage_malformed_authority_after_post_remains_committed(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_success_document(image_url="https://[broken/proof.png"),
        )

    with pytest.raises(ImageUploadError, match="invalid URL") as error:
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert len(requests) == 1
    assert error.value.provider == "freeimage"
    assert error.value.provider_may_have_committed is True


def test_freeimage_local_hash_failure_after_post_remains_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_success_document())
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    def fail_hash(_path: Path) -> str:
        raise OSError("local evidence disappeared")

    monkeypatch.setattr("bdencode.qc.freeimage.sha256_file", fail_hash)
    with pytest.raises(ImageUploadError, match="local verification") as error:
        FreeimageClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider == "freeimage"
    assert error.value.provider_may_have_committed is True


def test_freeimage_owned_client_cleanup_failure_remains_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _png(tmp_path)

    class CloseFailingClient:
        def post(self, *_args, **_kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", FreeimageClient.endpoint),
                json=_success_document(),
            )

        def get(self, *_args, **_kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://iili.io/proof.png"),
                content=PNG,
                headers={"content-type": "image/png"},
            )

        def close(self) -> None:
            raise OSError("close failed")

    monkeypatch.setattr(
        "bdencode.qc.freeimage.httpx.Client", lambda **_kwargs: CloseFailingClient()
    )
    with pytest.raises(ImageUploadError, match="cleanup failed") as error:
        FreeimageClient(credential_loader=lambda _: "secret").upload_png(path)

    assert error.value.provider == "freeimage"
    assert error.value.provider_may_have_committed is True
