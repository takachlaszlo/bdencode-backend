from __future__ import annotations

from collections.abc import Callable

import pytest

from bdencode.qc.image_upload import (
    UploadedImage,
    infer_upload_provider,
    parse_uploaded_image,
    parse_uploaded_image_checkpoint,
)


DIGEST = "a" * 64


def _entry(
    *,
    provider: str = "imgbb",
    image_url: str = "https://i.ibb.co/album/proof.png",
    viewer_url: str = "https://ibb.co/proof",
    bbcode: object = "[img]untrusted checkpoint text[/img]",
) -> dict[str, object]:
    return {
        "provider": provider,
        "image_url": image_url,
        "viewer_url": viewer_url,
        "local_sha256": DIGEST,
        "remote_sha256": DIGEST,
        "bbcode": bbcode,
    }


@pytest.mark.parametrize(
    ("entry", "expected_bbcode"),
    [
        (
            _entry(),
            "[url=https://ibb.co/proof]"
            "[img]https://i.ibb.co/album/proof.png[/img][/url]",
        ),
        (
            _entry(
                provider="catbox",
                image_url="https://files.catbox.moe/proof.png",
                viewer_url="https://files.catbox.moe/proof.png",
            ),
            "[img]https://files.catbox.moe/proof.png[/img]",
        ),
        (
            _entry(
                provider="freeimage",
                image_url="https://iili.io/proof.png",
                viewer_url="https://freeimage.host/i/proof",
            ),
            "[url=https://freeimage.host/i/proof]"
            "[img]https://iili.io/proof.png[/img][/url]",
        ),
    ],
)
def test_checkpoint_parser_accepts_adapter_urls_and_rebuilds_bbcode(
    entry: dict[str, object], expected_bbcode: str
) -> None:
    result = parse_uploaded_image(entry, expected_local_sha256=DIGEST)

    assert isinstance(result, UploadedImage)
    assert result.bbcode == expected_bbcode
    assert result.bbcode != entry["bbcode"]
    assert result.local_sha256 == DIGEST
    assert result.remote_sha256 == DIGEST


def test_checkpoint_parser_ignores_injected_stored_bbcode() -> None:
    entry = _entry(
        bbcode="[/img][/url][url=https://attacker.invalid/?secret=credential]pwned"
    )

    result = parse_uploaded_image_checkpoint(entry, expected_local_sha256=DIGEST)

    assert "attacker.invalid" not in result.bbcode
    assert result.bbcode == (
        "[url=https://ibb.co/proof]"
        "[img]https://i.ibb.co/album/proof.png[/img][/url]"
    )


@pytest.mark.parametrize("field", ["local_sha256", "remote_sha256"])
def test_checkpoint_parser_rejects_hash_tampering(field: str) -> None:
    entry = _entry()
    entry[field] = "b" * 64

    with pytest.raises(ValueError, match="hashes do not match"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)


@pytest.mark.parametrize(
    "digest",
    ["a" * 63, "a" * 65, "g" * 64, "", None, 7],
)
def test_checkpoint_parser_rejects_malformed_hashes(digest: object) -> None:
    entry = _entry()
    entry["remote_sha256"] = digest

    with pytest.raises(ValueError, match="64 hexadecimal"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", "top-secret"),
        ("userhash", "catbox-secret"),
        ("delete_url", "https://ibb.co/delete/private-token"),
        ("extra", True),
    ],
)
def test_checkpoint_parser_rejects_unknown_secret_and_delete_fields(
    field: str, value: object
) -> None:
    entry = _entry()
    entry[field] = value

    with pytest.raises(ValueError, match="fields do not match"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_url", "http://i.ibb.co/proof.png"),
        ("image_url", "https://user:password@i.ibb.co/proof.png"),
        ("image_url", "https://i.ibb.co:443/proof.png"),
        ("image_url", "https://i.ibb.co.evil.invalid/proof.png"),
        ("image_url", "https://ibb.co/not-a-direct-image"),
        ("image_url", "https://i.ibb.co/proof.png?api_key=top-secret"),
        ("image_url", "https://i.ibb.co/proof.png#private-fragment"),
        ("viewer_url", "https://ibb.co.evil.invalid/proof"),
        ("viewer_url", "https://evil.invalid/https://ibb.co/proof"),
    ],
)
def test_checkpoint_parser_rejects_non_provider_and_unsafe_urls(
    field: str, value: str
) -> None:
    entry = _entry()
    entry[field] = value

    with pytest.raises(ValueError, match="URL violates provider policy"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_url", "https://i.ibb.co/proof.png[/img][url=https://evil.invalid]"),
        ("viewer_url", "https://ibb.co/proof\n[/url][img]injected"),
    ],
)
def test_checkpoint_parser_rejects_bbcode_injection_via_urls(
    field: str, value: str
) -> None:
    entry = _entry()
    entry[field] = value

    with pytest.raises(ValueError, match="URL violates provider policy"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)


@pytest.mark.parametrize("bbcode", ["", None, False, 0, []])
def test_checkpoint_parser_requires_truthy_string_bbcode(bbcode: object) -> None:
    entry = _entry(bbcode=bbcode)

    with pytest.raises(ValueError, match="BBCode must be a non-empty string"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)


def test_legacy_checkpoint_accepts_explicit_provider_with_exact_field_set() -> None:
    entry = _entry()
    del entry["provider"]

    result = parse_uploaded_image(
        entry,
        expected_local_sha256=DIGEST,
        legacy_provider="imgbb",
    )

    assert result.provider == "imgbb"


@pytest.mark.parametrize(
    ("entry", "expected_provider"),
    [
        (_entry(), "imgbb"),
        (
            _entry(
                provider="catbox",
                image_url="https://files.catbox.moe/proof.png",
                viewer_url="https://files.catbox.moe/proof.png",
            ),
            "catbox",
        ),
        (
            _entry(
                provider="freeimage",
                image_url="https://cdn.iili.io/proof.png",
                viewer_url="https://freeimage.host/i/proof",
            ),
            "freeimage",
        ),
    ],
)
def test_legacy_checkpoint_can_infer_provider(
    entry: dict[str, object], expected_provider: str
) -> None:
    del entry["provider"]

    result = parse_uploaded_image(
        entry,
        expected_local_sha256=DIGEST,
        infer_legacy_provider=True,
    )

    assert result.provider == expected_provider
    assert infer_upload_provider(result.image_url, result.viewer_url) == expected_provider


def test_legacy_checkpoint_requires_explicit_opt_in_and_exact_fields() -> None:
    entry = _entry()
    del entry["provider"]

    with pytest.raises(ValueError, match="no trusted provider"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)

    entry["delete_url"] = "https://ibb.co/delete/private-token"
    with pytest.raises(ValueError, match="fields do not match"):
        parse_uploaded_image(
            entry,
            expected_local_sha256=DIGEST,
            infer_legacy_provider=True,
        )


def test_current_checkpoint_rejects_conflicting_explicit_provider() -> None:
    with pytest.raises(ValueError, match="conflicts with checkpoint"):
        parse_uploaded_image(
            _entry(),
            expected_local_sha256=DIGEST,
            legacy_provider="catbox",
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: entry.pop("viewer_url"),
        lambda entry: entry.update(provider="ImgBB"),
        lambda entry: entry.update(provider=True),
    ],
)
def test_checkpoint_parser_fails_closed_on_missing_or_invalid_fields(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    entry = _entry()
    mutate(entry)

    with pytest.raises(ValueError):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)


def test_catbox_checkpoint_requires_identical_direct_and_viewer_urls() -> None:
    entry = _entry(
        provider="catbox",
        image_url="https://files.catbox.moe/direct.png",
        viewer_url="https://files.catbox.moe/different.png",
    )

    with pytest.raises(ValueError, match="must match"):
        parse_uploaded_image(entry, expected_local_sha256=DIGEST)
