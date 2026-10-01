from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bdencode.api import create_app
from bdencode.db import Database
from bdencode.profile_library import (
    BUNDLE_FORMAT,
    MAX_DOCUMENT_BYTES,
    ProfileExists,
    ProfileLibrary,
    ProfileLibraryError,
    ProfileNotFound,
    slugify,
    validate_profile,
)


def profile(**updates: object) -> dict[str, object]:
    document: dict[str, object] = {
        "name": "UHD szemcsés film",
        "description": "Filmszemcsés UHD forráshoz",
        "encoder": "x265",
        "detail_level": "advanced",
        "settings": {"crf": 17.5, "preset": "slower", "noise_reduction": 0},
    }
    document.update(updates)
    return document


def test_slugify_is_ascii_and_stable() -> None:
    assert slugify("UHD szemcsés film") == "uhd-szemcses-film"
    assert slugify("  ÁRVÍZTŰRŐ tükörfúrógép!! ") == "arvizturo-tukorfurogep"
    assert slugify("???") == "profile"
    assert len(slugify("x" * 200)) == 64


def test_validate_builds_real_settings_and_normalizes() -> None:
    result = validate_profile(
        profile(
            auto_crf={"enabled": True, "target_vmaf": 94.5},
            dynamic_hdr="auto",
        )
    )
    assert result.id == "uhd-szemcses-film" and result.encoder == "x265"
    assert result.auto_crf and result.auto_crf["target_vmaf"] == 94.5
    assert result.dynamic_hdr == "auto"
    assert result.selection_fragment() == {
        "detail_level": "advanced",
        "settings": {"crf": 17.5, "preset": "slower", "noise_reduction": 0},
        "auto_crf": result.auto_crf,
        "dynamic_hdr": "auto",
    }
    plain = validate_profile(profile()).selection_fragment()
    assert "auto_crf" not in plain and "dynamic_hdr" not in plain


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"name": ""}, "invalid"),
        ({"name": "x" * 81}, "invalid"),
        ({"name": "two\nlines"}, "invalid"),
        ({"name": 7}, "invalid"),
        ({"description": "x" * 501}, "invalid"),
        ({"description": "bell\x07"}, "invalid"),
        ({"encoder": "av1"}, "invalid"),
        ({"detail_level": "expert"}, "invalid"),
        ({"format": "other"}, "invalid"),
        ({"version": 2}, "unsupported_version"),
        ({"surprise": 1}, "invalid"),
        ({"settings": []}, "invalid"),
        ({"settings": {"crf": 99}}, "invalid_settings"),
        ({"settings": {"crf": True}}, "invalid_settings"),
        ({"settings": {"no_such_field": 1}}, "invalid_settings"),
        ({"settings": {"preset": "warp"}}, "invalid_settings"),
        ({"settings": {"noise_reduction": 5000}}, "invalid_settings"),
        ({"auto_crf": {"enabled": True, "target_vmaf": 10}}, "invalid_auto_crf"),
        ({"dynamic_hdr": "always"}, "invalid_dynamic_hdr"),
    ],
)
def test_validate_rejects_unsafe_or_unsupported_values(updates: dict, code: str) -> None:
    with pytest.raises(ProfileLibraryError) as error:
        validate_profile(profile(**updates))
    assert error.value.code == code


@pytest.mark.parametrize(
    "field",
    ["encoder", "profile", "level", "bit_depth", "pixel_format", "color", "vbv", "hdr10", "aud", "repeat_headers", "annexb"],
)
def test_disc_specific_and_bitstream_settings_are_not_portable(field: str) -> None:
    with pytest.raises(ProfileLibraryError) as error:
        validate_profile(profile(settings={field: "x"}))
    assert error.value.code == "not_portable" and field in str(error.value)


def test_library_round_trips_and_keeps_creation_time_on_overwrite(tmp_path: Path) -> None:
    library = ProfileLibrary(tmp_path / "library")
    assert library.list() == []
    first = library.save(profile())
    assert (tmp_path / "library" / "uhd-szemcses-film.json").is_file()
    assert library.get(first.id).settings["crf"] == 17.5

    with pytest.raises(ProfileExists):
        library.save(profile())
    updated = library.save(profile(settings={"crf": 19}), overwrite=True)
    assert updated.created_at == first.created_at
    assert library.get(first.id).settings == {"crf": 19}
    assert [item.id for item in library.list()] == [first.id]

    library.delete(first.id)
    with pytest.raises(ProfileNotFound):
        library.get(first.id)
    with pytest.raises(ProfileNotFound):
        library.delete(first.id)


@pytest.mark.parametrize("bad_id", ["../evil", "UPPER", "a/b", "", "x" * 65, "ő"])
def test_profile_ids_cannot_escape_the_library(tmp_path: Path, bad_id: str) -> None:
    library = ProfileLibrary(tmp_path / "library")
    with pytest.raises(ProfileLibraryError):
        library.get(bad_id)
    with pytest.raises(ProfileLibraryError):
        library.delete(bad_id)


def test_a_damaged_file_does_not_hide_the_other_profiles(tmp_path: Path) -> None:
    library = ProfileLibrary(tmp_path / "library")
    library.save(profile(name="Good"))
    (tmp_path / "library" / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "library" / "hacked.json").write_text(
        json.dumps({"name": "Hacked", "encoder": "x264", "settings": {"level": "4.1"}}),
        encoding="utf-8",
    )
    (tmp_path / "library" / "Not-An-Id.json").write_text("{}", encoding="utf-8")
    assert [item.id for item in library.list()] == ["good"]


def test_export_and_import_round_trip_between_libraries(tmp_path: Path) -> None:
    source = ProfileLibrary(tmp_path / "a")
    source.save(profile(name="Alpha", auto_crf={"enabled": True}))
    source.save(profile(name="Beta", encoder="x264", settings={"crf": 16}))
    bundle = source.export_bundle()
    assert bundle["format"] == BUNDLE_FORMAT and len(bundle["profiles"]) == 2

    target = ProfileLibrary(tmp_path / "b")
    outcome = target.import_document(bundle)
    assert [item["id"] for item in outcome["imported"]] == ["alpha", "beta"]
    assert outcome["errors"] == [] and outcome["skipped"] == []
    assert target.get("alpha").auto_crf["enabled"] is True

    single = target.import_document(source.export("beta"), on_conflict="skip")
    assert single["skipped"] == [{"name": "Beta", "id": "beta"}] and not single["imported"]
    renamed = target.import_document(source.export("beta"))
    assert renamed["imported"] == [{"name": "Beta", "id": "beta-2"}]
    overwritten = target.import_document(profile(name="Beta", settings={"crf": 20}, encoder="x264"), on_conflict="overwrite")
    assert overwritten["imported"] == [{"name": "Beta", "id": "beta"}]
    assert target.get("beta").settings == {"crf": 20}


def test_import_ignores_file_supplied_ids_and_reports_bad_entries(tmp_path: Path) -> None:
    library = ProfileLibrary(tmp_path / "library")
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": 1,
        "profiles": [
            profile(name="Fine", id="../../etc/passwd", created_at="1999-01-01"),
            profile(name="Bad", settings={"crf": 500}),
            "not an object",
            profile(name="Hdr", settings={"color": {"primaries": "bt709"}}),
        ],
    }
    outcome = library.import_document(bundle)
    assert outcome["imported"] == [{"name": "Fine", "id": "fine"}]
    assert [item["code"] for item in outcome["errors"]] == [
        "invalid_settings",
        "invalid",
        "not_portable",
    ]
    assert not list((tmp_path).glob("**/passwd*"))
    assert library.get("fine").created_at != "1999-01-01"


def test_import_limits_and_conflict_mode_are_validated(tmp_path: Path) -> None:
    library = ProfileLibrary(tmp_path / "library")
    with pytest.raises(ProfileLibraryError, match="on_conflict"):
        library.import_document(profile(), on_conflict="merge")
    with pytest.raises(ProfileLibraryError) as error:
        library.import_document(profile(description="x" * (MAX_DOCUMENT_BYTES + 1)))
    assert error.value.code == "too_large"
    with pytest.raises(ProfileLibraryError):
        library.import_document([1, 2])
    with pytest.raises(ProfileLibraryError):
        library.import_document({"format": BUNDLE_FORMAT, "version": 9, "profiles": []})
    with pytest.raises(ProfileLibraryError):
        library.import_document({"format": BUNDLE_FORMAT, "version": 1, "profiles": "x"})


def test_library_size_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bdencode.profile_library as module

    monkeypatch.setattr(module, "MAX_PROFILES", 2)
    library = ProfileLibrary(tmp_path / "library")
    library.save(profile(name="One"))
    library.save(profile(name="Two"))
    with pytest.raises(ProfileLibraryError) as error:
        library.save(profile(name="Three"))
    assert error.value.code == "limit"


# -- HTTP API ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(Database(tmp_path / "state" / "api.sqlite3"))) as test_client:
        yield test_client


def test_http_library_lifecycle(client: TestClient) -> None:
    assert client.get("/api/v1/profile-library").json() == {"items": [], "count": 0}

    created = client.post("/api/v1/profile-library", json=profile(auto_crf={"enabled": True}))
    assert created.status_code == 201
    body = created.json()
    assert body["id"] == "uhd-szemcses-film"
    assert body["selection"]["auto_crf"]["enabled"] is True
    assert body["selection"]["settings"]["crf"] == 17.5

    assert client.post("/api/v1/profile-library", json=profile()).status_code == 409
    assert client.post("/api/v1/profile-library?overwrite=true", json=profile()).status_code == 201

    listed = client.get("/api/v1/profile-library").json()
    assert listed["count"] == 1 and listed["items"][0]["name"] == "UHD szemcsés film"

    exported = client.get("/api/v1/profile-library/uhd-szemcses-film/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.json()["format"] == "bdencode-profile"

    bundle = client.get("/api/v1/profile-library/export").json()
    assert bundle["format"] == BUNDLE_FORMAT and len(bundle["profiles"]) == 1

    assert client.delete("/api/v1/profile-library/uhd-szemcses-film").status_code == 204
    assert client.get("/api/v1/profile-library/uhd-szemcses-film").status_code == 404
    assert client.delete("/api/v1/profile-library/uhd-szemcses-film").status_code == 404


def test_http_import_reports_per_entry_results(client: TestClient) -> None:
    bundle = {
        "format": BUNDLE_FORMAT,
        "version": 1,
        "profiles": [profile(name="Ok"), profile(name="Bad", settings={"crf": 500})],
    }
    response = client.post("/api/v1/profile-library/import", json=bundle)
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["imported"]] == ["ok"]
    assert body["errors"][0]["code"] == "invalid_settings"

    assert client.post("/api/v1/profile-library/import?on_conflict=merge", json=profile()).status_code == 422
    rejected = client.post("/api/v1/profile-library", json=profile(settings={"color": {}}))
    assert rejected.status_code == 422 and rejected.json()["code"] == "not_portable"
    assert client.get("/api/v1/profile-library/..%2Fevil").status_code in {404, 422}


def test_concurrent_saves_of_one_name_never_both_succeed(tmp_path: Path) -> None:
    import threading

    library = ProfileLibrary(tmp_path / "library")
    outcomes: list[str] = []
    barrier = threading.Barrier(6)

    def save() -> None:
        barrier.wait(timeout=10)
        try:
            library.save(profile(name="Race"))
            outcomes.append("saved")
        except ProfileExists:
            outcomes.append("exists")

    threads = [threading.Thread(target=save) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(outcomes) == ["exists"] * 5 + ["saved"]
    assert [item.id for item in library.list()] == ["race"]
