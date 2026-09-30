from __future__ import annotations

import json
from pathlib import Path

import pytest

from bdencode.hdr_dynamic import (
    DISCARD_PLAN,
    DynamicHdrError,
    DynamicHdrMode,
    DynamicHdrPlan,
    allowed_forbidden_tokens,
    checked_path,
    dovi_extract_commands,
    dovi_summary_command,
    hdr10plus_extract_commands,
    parse_dovi_summary,
    parse_hdr10plus_json,
    parse_mode,
    require_dolby_vision_profile,
    require_frame_alignment,
    resolve_dynamic_hdr,
    validate_retained_side_data,
    x265_dynamic_params,
    x265_support_from_help,
)


def resolve(requested: DynamicHdrMode, **overrides):
    facts = dict(
        encoder="x265",
        hdr10_enabled=True,
        progressive=True,
        crop_enabled=False,
        dolby_vision=False,
        dolby_vision_profile=None,
        hdr10_base_layer=False,
        hdr10_plus=False,
    )
    facts.update(overrides)
    return resolve_dynamic_hdr(requested, **facts)


def test_default_is_the_discard_policy() -> None:
    assert parse_mode(None) is DynamicHdrMode.DISCARD
    assert resolve(DynamicHdrMode.DISCARD, hdr10_plus=True) is DISCARD_PLAN
    assert not DISCARD_PLAN.retained and DISCARD_PLAN.required_tool is None


@pytest.mark.parametrize("raw", [1, True, "hdr10+", "DOLBY_VISION", ""])
def test_mode_parsing_rejects_unknown_values(raw: object) -> None:
    with pytest.raises(DynamicHdrError) as error:
        parse_mode(raw)
    assert error.value.code == "invalid_mode"


def test_hdr10plus_retention_needs_a_source_that_carries_it() -> None:
    plan = resolve(DynamicHdrMode.HDR10PLUS, hdr10_plus=True)
    assert plan.mode is DynamicHdrMode.HDR10PLUS and plan.retained
    assert plan.required_tool == "hdr10plus_tool"
    with pytest.raises(DynamicHdrError) as error:
        resolve(DynamicHdrMode.HDR10PLUS)
    assert error.value.code == "source_missing"


def test_dolby_vision_profile_7_is_converted_and_profile_8_is_copied() -> None:
    p7 = resolve(
        DynamicHdrMode.DOLBY_VISION,
        dolby_vision=True,
        dolby_vision_profile=7,
        hdr10_base_layer=True,
        crop_enabled=True,
    )
    assert p7.mode is DynamicHdrMode.DOLBY_VISION
    assert p7.convert_mode == 2 and p7.crop_active_area and p7.source_profile == 7
    p8 = resolve(
        DynamicHdrMode.DOLBY_VISION,
        dolby_vision=True,
        dolby_vision_profile=8,
        hdr10_base_layer=True,
    )
    assert p8.convert_mode is None and not p8.crop_active_area


@pytest.mark.parametrize(
    ("facts", "code"),
    [
        ({"dolby_vision": True, "dolby_vision_profile": 7}, "no_hdr10_base"),
        (
            {"dolby_vision": True, "dolby_vision_profile": 5, "hdr10_base_layer": True},
            "unsupported_profile",
        ),
        (
            {"dolby_vision": True, "dolby_vision_profile": None, "hdr10_base_layer": True},
            "unsupported_profile",
        ),
        ({}, "source_missing"),
    ],
)
def test_dolby_vision_refuses_sources_it_cannot_convert(facts, code) -> None:
    with pytest.raises(DynamicHdrError) as error:
        resolve(DynamicHdrMode.DOLBY_VISION, **facts)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("facts", "code"),
    [
        ({"encoder": "x264"}, "unsupported_output"),
        ({"hdr10_enabled": False}, "unsupported_output"),
        ({"progressive": False}, "temporal_filter"),
    ],
)
def test_retention_needs_a_frame_exact_hdr10_x265_output(facts, code) -> None:
    with pytest.raises(DynamicHdrError) as error:
        resolve(DynamicHdrMode.HDR10PLUS, hdr10_plus=True, **facts)
    assert error.value.code == code


def test_auto_never_raises_and_prefers_hdr10plus() -> None:
    both = resolve(
        DynamicHdrMode.AUTO,
        hdr10_plus=True,
        dolby_vision=True,
        dolby_vision_profile=7,
        hdr10_base_layer=True,
    )
    assert both.mode is DynamicHdrMode.HDR10PLUS and both.requested is DynamicHdrMode.AUTO
    dolby = resolve(
        DynamicHdrMode.AUTO,
        dolby_vision=True,
        dolby_vision_profile=8,
        hdr10_base_layer=True,
    )
    assert dolby.mode is DynamicHdrMode.DOLBY_VISION
    for facts in (
        {},
        {"encoder": "x264", "hdr10_plus": True},
        {"progressive": False, "hdr10_plus": True},
        {"dolby_vision": True, "dolby_vision_profile": 5, "hdr10_base_layer": True},
    ):
        plan = resolve(DynamicHdrMode.AUTO, **facts)
        assert plan.mode is DynamicHdrMode.DISCARD and plan.reason


def test_extraction_commands_pipe_the_untouched_hevc_stream() -> None:
    plus = hdr10plus_extract_commands(Path("/w/reference.mkv"), Path("/w/dhdr.json"))
    assert plus[0][0] == "ffmpeg" and plus[0][-1] == "-"
    assert plus[0][plus[0].index("-c") + 1] == "copy"
    assert plus[0][plus[0].index("-bsf:v") + 1] == "hevc_mp4toannexb"
    assert plus[1] == ["hdr10plus_tool", "extract", "-o", str(Path("/w/dhdr.json")), "-"]

    plan = DynamicHdrPlan(
        DynamicHdrMode.DOLBY_VISION,
        DynamicHdrMode.DOLBY_VISION,
        "test",
        source_profile=7,
        convert_mode=2,
        crop_active_area=True,
    )
    dovi = dovi_extract_commands(Path("/w/reference.mkv"), Path("/w/rpu.bin"), plan)
    assert dovi[1] == [
        "dovi_tool",
        "-m",
        "2",
        "-c",
        "extract-rpu",
        "-o",
        str(Path("/w/rpu.bin")),
        "-",
    ]
    plain = DynamicHdrPlan(
        DynamicHdrMode.DOLBY_VISION, DynamicHdrMode.DOLBY_VISION, "test", source_profile=8
    )
    assert dovi_extract_commands(Path("r.mkv"), Path("x.bin"), plain)[1] == [
        "dovi_tool",
        "extract-rpu",
        "-o",
        "x.bin",
        "-",
    ]
    assert dovi_summary_command(Path("x.bin")) == [
        "dovi_tool",
        "info",
        "-i",
        "x.bin",
        "--summary",
    ]
    with pytest.raises(DynamicHdrError):
        dovi_extract_commands(Path("r.mkv"), Path("x.bin"), plus_plan())


def plus_plan() -> DynamicHdrPlan:
    return DynamicHdrPlan(
        DynamicHdrMode.HDR10PLUS, DynamicHdrMode.HDR10PLUS, "test"
    )


def hdr10plus_document(frames: int, scenes: int = 2) -> str:
    return json.dumps(
        {
            "JSONInfo": {"HDR10plusProfile": "B", "Version": "1.0"},
            "SceneInfo": [
                {"LuminanceParameters": {"AverageRGB": 100}, "NumberOfWindows": 1}
                for _ in range(frames)
            ],
            "SceneInfoSummary": {
                "SceneFirstFrameIndex": list(range(scenes)),
                "SceneFrameNumbers": [frames // scenes] * scenes,
            },
        }
    )


def test_hdr10plus_metadata_is_validated_and_counted() -> None:
    summary = parse_hdr10plus_json(hdr10plus_document(48, scenes=3))
    assert (summary.frames, summary.scenes) == (48, 3)
    assert parse_hdr10plus_json(
        json.dumps({"SceneInfo": [{"LuminanceParameters": {}}]})
    ).scenes == 1


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"SceneInfo": []}),
        json.dumps({"SceneInfo": [{"NumberOfWindows": 1}]}),
        json.dumps({"SceneInfo": ["x"]}),
    ],
)
def test_hdr10plus_metadata_rejects_unusable_documents(text: str) -> None:
    with pytest.raises(DynamicHdrError):
        parse_hdr10plus_json(text)


def test_dovi_summary_is_parsed_and_profile_checked() -> None:
    text = """Parsing RPU file...
Summary:
  Frames: 172627
  Profile: 8
  DM version: 1 (CM v2.9)
  Scene/shot count: 1204
"""
    summary = parse_dovi_summary(text)
    assert (summary.frames, summary.profile) == (172627, 8)
    require_dolby_vision_profile(summary)
    with pytest.raises(DynamicHdrError, match="profile 7"):
        require_dolby_vision_profile(parse_dovi_summary("Frames: 10\nProfile: 7 (FEL)\n"))
    for broken in ("", "Frames: 10", "Profile: 8", "Frames: 0\nProfile: 8"):
        with pytest.raises(DynamicHdrError):
            parse_dovi_summary(broken)


def test_frame_alignment_is_exact() -> None:
    require_frame_alignment(100, 100, what="RPU")
    with pytest.raises(DynamicHdrError, match="101 frames") as error:
        require_frame_alignment(101, 100, what="RPU")
    assert error.value.code == "frame_mismatch"


def test_x265_parameters_per_mode() -> None:
    tmp_path = Path("/data/jobs/0001/work")
    location = tmp_path / "dhdr10.json"
    assert x265_dynamic_params(plus_plan(), location) == {
        "dhdr10-info": location.as_posix(),
        "dhdr10-opt": 1,
    }
    dolby = DynamicHdrPlan(
        DynamicHdrMode.DOLBY_VISION, DynamicHdrMode.DOLBY_VISION, "test", source_profile=8
    )
    params = x265_dynamic_params(dolby, tmp_path / "rpu.bin")
    assert params["dolby-vision-profile"] == "8.1"
    assert params["dolby-vision-rpu"].endswith("rpu.bin")
    assert params["aud"] == params["repeat-headers"] == params["hrd"] == 1
    assert x265_dynamic_params(DISCARD_PLAN, location) == {}


@pytest.mark.parametrize("name", ["a b.json", "x:y.json", "x,y.json", "x=y.json"])
def test_metadata_paths_that_could_split_x265_parameters_are_refused(name: str) -> None:
    with pytest.raises(DynamicHdrError) as error:
        checked_path(Path("/data/jobs") / name)
    assert error.value.code == "unsafe_path"


def test_x265_support_is_read_from_help_text() -> None:
    help_text = "   --dhdr10-info <filename>\n   --dolby-vision-rpu <file>\n"
    assert x265_support_from_help(help_text) == {"hdr10plus": True, "dolby_vision": True}
    assert x265_support_from_help("--crf") == {"hdr10plus": False, "dolby_vision": False}
    assert x265_support_from_help("--dolby-vision-rpu") == {
        "hdr10plus": False,
        "dolby_vision": True,
    }


def test_retained_side_data_is_required_and_labelled_correctly() -> None:
    plus = plus_plan()
    assert validate_retained_side_data([], [], plus)
    assert not validate_retained_side_data(
        ["HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"], [], plus
    )
    assert allowed_forbidden_tokens(plus) == {"hdr dynamic", "hdr10+"}
    assert allowed_forbidden_tokens(DISCARD_PLAN) == frozenset()

    dolby = DynamicHdrPlan(
        DynamicHdrMode.DOLBY_VISION, DynamicHdrMode.DOLBY_VISION, "test", source_profile=8
    )
    good = {
        "side_data_type": "DOVI configuration record",
        "dv_profile": 8,
        "dv_bl_signal_compatibility_id": 1,
        "rpu_present_flag": 1,
    }
    assert not validate_retained_side_data([good["side_data_type"]], [good], dolby)
    assert validate_retained_side_data([], [], dolby)
    bad = {**good, "dv_profile": 7, "dv_bl_signal_compatibility_id": 6, "rpu_present_flag": 0}
    errors = validate_retained_side_data([bad["side_data_type"]], [bad], dolby)
    assert len(errors) == 3
    assert allowed_forbidden_tokens(dolby) == {"dolby vision", "dovi"}


def test_plan_round_trips_through_its_report_form() -> None:
    plan = DynamicHdrPlan(
        DynamicHdrMode.AUTO,
        DynamicHdrMode.DOLBY_VISION,
        "test",
        source_profile=7,
        convert_mode=2,
        crop_active_area=True,
    )
    assert DynamicHdrPlan.from_dict(plan.to_dict()) == plan
    assert DynamicHdrPlan.from_dict(DISCARD_PLAN.to_dict()) == DISCARD_PLAN
    for broken in (
        None,
        {},
        {**plan.to_dict(), "mode": "nope"},
        {**plan.to_dict(), "source_profile": "7"},
        {**plan.to_dict(), "crop_active_area": 1},
    ):
        with pytest.raises(DynamicHdrError) as error:
            DynamicHdrPlan.from_dict(broken)
        assert error.value.code == "invalid_report"


MASTERING = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"


def _static_side_data() -> list[dict[str, object]]:
    return [
        {
            "side_data_type": "Mastering display metadata",
            "green_x": "8500/50000",
            "green_y": "39850/50000",
            "blue_x": "6550/50000",
            "blue_y": "2300/50000",
            "red_x": "35400/50000",
            "red_y": "14600/50000",
            "white_point_x": "15635/50000",
            "white_point_y": "16450/50000",
            "max_luminance": "10000000/10000",
            "min_luminance": "1/10000",
        },
        {
            "side_data_type": "Content light level metadata",
            "max_content": 1000,
            "max_average": 400,
        },
    ]


def _gate(extra: list[dict[str, object]], plan: DynamicHdrPlan, stream_extra=()):
    from bdencode.mux import validate_dynamic_hdr_output, validate_hdr10_side_data

    frames = {"frames": [{"side_data_list": [*_static_side_data(), *extra]}]}
    streams = {"streams": [{"side_data_list": list(stream_extra)}]}
    return (
        *validate_hdr10_side_data(
            streams,
            frames,
            enabled=True,
            mastering_display=MASTERING,
            max_cll=1000,
            max_fall=400,
            allowed_dynamic=allowed_forbidden_tokens(plan),
        ),
        *validate_dynamic_hdr_output(streams, frames, plan),
    )


def test_output_gate_keeps_forbidding_dynamic_hdr_by_default() -> None:
    hdr10plus = [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}]
    errors = _gate(hdr10plus, DISCARD_PLAN)
    assert any("hdr10+" in item for item in errors)
    dolby = [{"side_data_type": "Dolby Vision RPU Data"}]
    assert any("dolby vision" in item for item in _gate(dolby, DISCARD_PLAN))


def test_output_gate_accepts_and_requires_the_retained_layer() -> None:
    hdr10plus = [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}]
    assert _gate(hdr10plus, plus_plan()) == ()
    missing = _gate([], plus_plan())
    assert len(missing) == 1 and "smpte2094-40" in missing[0]

    dolby_plan = DynamicHdrPlan(
        DynamicHdrMode.DOLBY_VISION, DynamicHdrMode.DOLBY_VISION, "t", source_profile=8
    )
    record = {
        "side_data_type": "DOVI configuration record",
        "dv_profile": 8,
        "dv_bl_signal_compatibility_id": 1,
        "rpu_present_flag": 1,
    }
    frame_rpu = [{"side_data_type": "Dolby Vision Metadata"}]
    assert _gate(frame_rpu, dolby_plan, stream_extra=[record]) == ()
    assert _gate(frame_rpu, dolby_plan)  # config record missing
    # HDR10+ is not tolerated when only Dolby Vision was planned, and vice versa.
    assert _gate(hdr10plus, dolby_plan, stream_extra=[record])
    assert any(
        "dolby vision" in item or "dovi" in item
        for item in _gate(frame_rpu + [record], plus_plan())
    )
