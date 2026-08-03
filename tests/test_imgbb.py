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
                        "url": "https://i.ibb.co/proof.png",
                        "url_viewer": "https://ibb.co/proof",
                        "delete_url": "https://delete.example/token",
                    },
                },
            )
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = ImgBBClient(
        credential_loader=lambda _: "top-secret", client=client
    ).upload_png(path)
    assert "top-secret" not in seen["url"]
    assert hashlib.sha256(PNG).hexdigest() == result.remote_sha256
    assert result.bbcode.startswith("[url=https://ibb.co")
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
                    "data": {"url": "https://i.ibb.co/proof.png"},
                },
            )
        return httpx.Response(
            200,
            content=PNG + b"changed",
            headers={"content-type": "image/png"},
        )

    with pytest.raises(ImageUploadError, match="changed"):
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)


def test_imgbb_maintenance_document_allows_secret_safe_fallback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    secret = "primary-private-key"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": False,
                "status": 503,
                "error": {"message": f"Maintenance; {secret}", "code": 0},
            },
        )

    with pytest.raises(ImageUploadError) as error:
        ImgBBClient(
            credential_loader=lambda _: secret,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.allow_fallback is True
    assert error.value.provider == "imgbb"
    assert secret not in str(error.value)
    assert "<redacted>" in str(error.value)


@pytest.mark.parametrize(
    "document",
    (
        {},
        {"success": None},
        {"success": "false"},
        {"status": 503},
    ),
)
def test_imgbb_malformed_2xx_status_is_commit_ambiguous(
    tmp_path: Path, document: dict[str, object]
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document)

    with pytest.raises(ImageUploadError, match="ambiguous") as error:
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_imgbb_legacy_explicit_rejection_without_success_remains_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": 503,
                "error": {"message": "maintenance", "code": 0},
            },
        )

    with pytest.raises(ImageUploadError) as error:
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.allow_fallback is True
    assert error.value.provider_may_have_committed is False


def test_imgbb_rejects_cross_origin_upload_redirect_without_following(
    tmp_path: Path,
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://attacker.invalid/collect"},
        )

    with pytest.raises(ImageUploadError) as error:
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(
                transport=httpx.MockTransport(handler), follow_redirects=True
            ),
        ).upload_png(path)

    assert len(requests) == 1
    assert requests[0].url.host == "api.imgbb.com"
    assert error.value.allow_fallback is False


def test_imgbb_verification_failure_never_switches_provider(tmp_path: Path) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"url": "https://i.ibb.co/proof.png"},
                },
            )
        return httpx.Response(503)

    with pytest.raises(ImageUploadError) as error:
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_imgbb_ambiguous_post_outcome_requires_a_durable_provider_pin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("upload outcome unknown", request=request)

    with pytest.raises(ImageUploadError) as error:
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider == "imgbb"
    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_imgbb_rejects_non_provider_verification_url(tmp_path: Path) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"url": "https://127.0.0.1/private.png"},
            },
        )

    with pytest.raises(ImageUploadError, match="invalid URL"):
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert len(requests) == 1


def test_imgbb_malformed_authority_after_post_remains_committed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"url": "https://[broken/proof.png"},
            },
        )

    with pytest.raises(ImageUploadError, match="invalid URL") as error:
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert len(requests) == 1
    assert error.value.provider == "imgbb"
    assert error.value.provider_may_have_committed is True


@pytest.mark.parametrize("echo_location", ("direct", "viewer"))
def test_imgbb_rejects_credential_echo_in_returned_urls(
    tmp_path: Path, echo_location: str
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    secret = "private-api-key"
    image_url = "https://i.ibb.co/proof.png"
    viewer_url = "https://ibb.co/proof"
    if echo_location == "direct":
        image_url = f"https://i.ibb.co/{secret}.png"
    else:
        viewer_url = f"https://ibb.co/{secret}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"url": image_url, "url_viewer": viewer_url},
            },
        )

    with pytest.raises(ImageUploadError, match="invalid URL") as error:
        ImgBBClient(
            credential_loader=lambda _: secret,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider_may_have_committed is True


def test_imgbb_local_hash_failure_after_post_remains_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {"url": "https://i.ibb.co/proof.png"},
                },
            )
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    def fail_hash(_path: Path) -> str:
        raise OSError("local evidence disappeared")

    monkeypatch.setattr("bdencode.qc.imgbb.sha256_file", fail_hash)
    with pytest.raises(ImageUploadError, match="local verification") as error:
        ImgBBClient(
            credential_loader=lambda _: "secret",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider == "imgbb"
    assert error.value.provider_may_have_committed is True


def test_imgbb_owned_client_cleanup_failure_remains_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    class CloseFailingClient:
        def post(self, *_args, **_kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", ImgBBClient.endpoint),
                json={
                    "success": True,
                    "data": {"url": "https://i.ibb.co/proof.png"},
                },
            )

        def get(self, *_args, **_kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://i.ibb.co/proof.png"),
                content=PNG,
                headers={"content-type": "image/png"},
            )

        def close(self) -> None:
            raise OSError("close failed")

    monkeypatch.setattr(
        "bdencode.qc.imgbb.httpx.Client", lambda **_kwargs: CloseFailingClient()
    )
    with pytest.raises(ImageUploadError, match="cleanup failed") as error:
        ImgBBClient(credential_loader=lambda _: "secret").upload_png(path)

    assert error.value.provider == "imgbb"
    assert error.value.provider_may_have_committed is True


def test_imgbb_cleanup_failure_does_not_mask_an_earlier_upload_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)

    class FailingClient:
        def post(self, url: str, **_kwargs) -> httpx.Response:
            request = httpx.Request("POST", url)
            raise httpx.ConnectTimeout("not connected", request=request)

        def close(self) -> None:
            raise OSError("close failed")

    monkeypatch.setattr(
        "bdencode.qc.imgbb.httpx.Client", lambda **_kwargs: FailingClient()
    )
    with pytest.raises(ImageUploadError, match="could not be reached") as error:
        ImgBBClient(credential_loader=lambda _: "secret").upload_png(path)

    assert error.value.allow_fallback is True
    assert error.value.provider_may_have_committed is False
