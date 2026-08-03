from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from bdencode.qc.catbox import CatboxClient
from bdencode.qc.imgbb import ImageUploadError, UploadedImage


PNG = b"\x89PNG\r\n\x1a\ncatbox-contract-png"


def _png(tmp_path: Path) -> Path:
    path = tmp_path / "proof.png"
    path.write_bytes(PNG)
    return path


def test_catbox_anonymous_upload_omits_userhash_and_verifies_round_trip(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = request.content
            seen["url"] = str(request.url)
            seen["body"] = body
            assert b'name="reqtype"' in body
            assert b"fileupload" in body
            assert b'name="fileToUpload"' in body
            assert b'filename="proof.png"' in body
            assert b"userhash" not in body
            return httpx.Response(200, text="https://files.catbox.moe/proof.png\n")
        assert str(request.url) == "https://files.catbox.moe/proof.png"
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    uploader = CatboxClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = uploader.upload_png(path)

    digest = hashlib.sha256(PNG).hexdigest()
    assert uploader.provider_name == "catbox"
    assert isinstance(result, UploadedImage)
    assert result.image_url == "https://files.catbox.moe/proof.png"
    assert result.viewer_url == result.image_url
    assert result.local_sha256 == digest
    assert result.remote_sha256 == digest
    assert result.image_url in result.bbcode
    assert "userhash" not in str(seen["url"])


def test_catbox_account_upload_sends_explicit_userhash_only_in_multipart_body(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)
    userhash = "catbox-private-userhash"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert b'name="userhash"' in request.content
            assert userhash.encode() in request.content
            assert userhash not in str(request.url)
            return httpx.Response(200, text="https://files.catbox.moe/account.png")
        assert userhash not in str(request.url)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    result = CatboxClient(
        userhash=userhash,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).upload_png(path)

    assert isinstance(result, UploadedImage)
    assert userhash not in result.image_url
    assert userhash not in result.viewer_url
    assert userhash not in result.bbcode


def test_catbox_rejects_changed_download(tmp_path: Path) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text="https://files.catbox.moe/changed.png")
        return httpx.Response(
            200,
            content=PNG + b"changed",
            headers={"content-type": "image/png"},
        )

    with pytest.raises(ImageUploadError, match="changed"):
        CatboxClient(
            client=httpx.Client(transport=httpx.MockTransport(handler))
        ).upload_png(path)


def test_catbox_rejects_upload_redirect_without_leaking_userhash(
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
        CatboxClient(
            userhash="private-userhash",
            client=httpx.Client(
                transport=httpx.MockTransport(handler), follow_redirects=True
            ),
        ).upload_png(path)

    assert len(requests) == 1
    assert requests[0].url.host == "catbox.moe"
    assert error.value.allow_fallback is False


def test_catbox_verification_failure_never_switches_provider(tmp_path: Path) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text="https://files.catbox.moe/proof.png")
        return httpx.Response(503)

    with pytest.raises(ImageUploadError) as error:
        CatboxClient(
            client=httpx.Client(transport=httpx.MockTransport(handler))
        ).upload_png(path)

    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_catbox_ambiguous_post_outcome_requires_a_durable_provider_pin(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("upload outcome unknown", request=request)

    with pytest.raises(ImageUploadError) as error:
        CatboxClient(
            client=httpx.Client(transport=httpx.MockTransport(handler))
        ).upload_png(path)

    assert error.value.provider == "catbox"
    assert error.value.allow_fallback is False
    assert error.value.provider_may_have_committed is True


def test_catbox_malformed_authority_after_post_remains_committed(
    tmp_path: Path,
) -> None:
    path = _png(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="https://[broken/proof.png")

    with pytest.raises(ImageUploadError, match="invalid image URL") as error:
        CatboxClient(
            client=httpx.Client(transport=httpx.MockTransport(handler))
        ).upload_png(path)

    assert len(requests) == 1
    assert error.value.provider == "catbox"
    assert error.value.provider_may_have_committed is True


def test_catbox_rejects_userhash_echo_in_returned_url(tmp_path: Path) -> None:
    path = _png(tmp_path)
    userhash = "private-userhash"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, text=f"https://files.catbox.moe/{userhash}.png")

    with pytest.raises(ImageUploadError, match="invalid image URL") as error:
        CatboxClient(
            userhash=userhash,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        ).upload_png(path)

    assert error.value.provider_may_have_committed is True


def test_catbox_local_hash_failure_after_post_remains_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _png(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text="https://files.catbox.moe/proof.png")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    def fail_hash(_path: Path) -> str:
        raise OSError("local evidence disappeared")

    monkeypatch.setattr("bdencode.qc.catbox.sha256_file", fail_hash)
    with pytest.raises(ImageUploadError, match="local verification") as error:
        CatboxClient(
            client=httpx.Client(transport=httpx.MockTransport(handler))
        ).upload_png(path)

    assert error.value.provider == "catbox"
    assert error.value.provider_may_have_committed is True


def test_catbox_owned_client_cleanup_failure_remains_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _png(tmp_path)

    class CloseFailingClient:
        def post(self, *_args, **_kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", CatboxClient.endpoint),
                text="https://files.catbox.moe/proof.png",
            )

        def get(self, *_args, **_kwargs) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("GET", "https://files.catbox.moe/proof.png"),
                content=PNG,
                headers={"content-type": "image/png"},
            )

        def close(self) -> None:
            raise OSError("close failed")

    monkeypatch.setattr(
        "bdencode.qc.catbox.httpx.Client", lambda **_kwargs: CloseFailingClient()
    )
    with pytest.raises(ImageUploadError, match="cleanup failed") as error:
        CatboxClient(userhash="account-userhash").upload_png(path)

    assert error.value.provider == "catbox"
    assert error.value.provider_may_have_committed is True
