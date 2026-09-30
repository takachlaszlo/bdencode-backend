from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from bdencode.qc.catbox import CatboxClient
from bdencode.qc.freeimage import FreeimageClient
from bdencode.qc.imgbb import ImageUploadError, ImgBBClient
from bdencode.secrets import SecretUnavailable, read_secret


PNG = b"\x89PNG\r\n\x1a\nsecret-fallback-png"


def _credentials(tmp_path: Path, **files: str) -> dict[str, str]:
    directory = tmp_path / "credentials"
    directory.mkdir()
    for name, content in files.items():
        (directory / name.replace("_", "-")).write_text(content, encoding="utf-8")
    return {"CREDENTIALS_DIRECTORY": str(directory)}


def test_present_credential_is_read_and_stripped(tmp_path: Path) -> None:
    env = _credentials(tmp_path, imgbb_api_key="  value\n")
    assert read_secret("imgbb-api-key", environment=env) == "value"


def test_missing_credential_in_existing_directory_is_unavailable(
    tmp_path: Path,
) -> None:
    # systemd only creates the credentials that were configured, so a partial
    # set is normal and must not surface as a raw FileNotFoundError.
    env = _credentials(tmp_path, catbox_userhash="hash")
    with pytest.raises(SecretUnavailable):
        read_secret("imgbb-api-key", environment=env)


def test_missing_credential_falls_back_to_environment_when_allowed(
    tmp_path: Path,
) -> None:
    env = _credentials(tmp_path, catbox_userhash="hash")
    env["IMGBB_API_KEY"] = " from-env "
    assert (
        read_secret("imgbb-api-key", environment=env, allow_environment=True)
        == "from-env"
    )


def test_empty_credential_file_falls_back_to_environment(tmp_path: Path) -> None:
    env = _credentials(tmp_path, imgbb_api_key="\n")
    env["IMGBB_API_KEY"] = "from-env"
    assert (
        read_secret("imgbb-api-key", environment=env, allow_environment=True)
        == "from-env"
    )
    with pytest.raises(SecretUnavailable):
        read_secret("imgbb-api-key", environment=env)


def test_missing_credentials_directory_is_unavailable(tmp_path: Path) -> None:
    env = {"CREDENTIALS_DIRECTORY": str(tmp_path / "does-not-exist")}
    with pytest.raises(SecretUnavailable):
        read_secret("imgbb-api-key", environment=env)


def test_credential_name_cannot_escape_the_credentials_directory(
    tmp_path: Path,
) -> None:
    env = _credentials(tmp_path, imgbb_api_key="value")
    (tmp_path / "outside").write_text("stolen", encoding="utf-8")
    with pytest.raises(SecretUnavailable):
        read_secret("../outside", environment=env)


def test_partial_credential_set_keeps_the_provider_chain_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a Catbox userhash is provisioned: ImgBB must yield to the fallback."""

    env = _credentials(tmp_path, catbox_userhash="account-hash")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    png = tmp_path / "proof.png"
    png.write_bytes(PNG)

    # ImgBB: an unconfigured key must be a safe, fallback-eligible failure.
    with pytest.raises(ImageUploadError) as imgbb:
        ImgBBClient().upload_png(png)
    assert imgbb.value.allow_fallback is True
    assert imgbb.value.provider == "imgbb"

    # Freeimage: reported as not configured, not as a loader crash.
    with pytest.raises(ImageUploadError) as freeimage:
        FreeimageClient().upload_png(png)
    assert "not configured" in str(freeimage.value)

    # Catbox: the configured userhash is used.
    seen: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = request.content
            return httpx.Response(200, text="https://files.catbox.moe/proof.png")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    result = CatboxClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    ).upload_png(png)
    assert result.provider == "catbox"
    assert b"account-hash" in seen["body"]


def test_catbox_without_userhash_uploads_anonymously_beside_other_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _credentials(tmp_path, imgbb_api_key="key-only")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    png = tmp_path / "proof.png"
    png.write_bytes(PNG)
    seen: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = request.content
            return httpx.Response(200, text="https://files.catbox.moe/proof.png")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    result = CatboxClient(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    ).upload_png(png)
    assert result.provider == "catbox"
    assert b"userhash" not in seen["body"]
