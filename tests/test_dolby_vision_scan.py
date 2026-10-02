from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bdencode.dolby_vision_scan import (
    DolbyVisionLayer,
    detect_dolby_vision_layers,
    enhancement_layer_candidates,
    layer_probe_commands,
    parse_layer_summary,
    with_dolby_vision_layer,
)
from bdencode.hdr_dynamic import DynamicHdrError, DynamicHdrMode, resolve_dynamic_hdr
from bdencode.media.bluray import (
    ContentKind,
    DiscKind,
    DiscScan,
    MediaStream,
    PlaylistCandidate,
    StreamKind,
    ToolCapabilities,
    VideoCodec,
    VideoProperties,
)
from bdencode.process import ProcessFailure, ProcessResult

SUMMARY_MEL = "Summary:\n  Frames: 120\n  Profile: 7 (MEL)\n  DM version: 1 (CM v2.9)\n"
SUMMARY_FEL = "Summary:\n  Frames: 120\n  Profile: 7 (FEL)\n"
SUMMARY_P8 = "Summary:\n  Frames: 120\n  Profile: 8\n"


def video(stream_id: str, index: int, width: int, height: int, *, rate: str = "24000/1001") -> MediaStream:
    return MediaStream(
        id=stream_id, index=index, pid=4113 + index, kind=StreamKind.VIDEO, codec="hevc",
        video=VideoProperties(
            codec=VideoCodec.HEVC, width=width, height=height, frame_rate=rate, bit_depth=10,
            color_primaries="bt2020", color_transfer="smpte2084", color_matrix="bt2020nc",
            hdr10=True, hdr10_base_layer=True,
        ),
    )


def playlist(playlist_id: str = "00001", minutes: float = 117.0, *, recommended: bool = True,
             streams: tuple[MediaStream, ...] | None = None) -> PlaylistCandidate:
    return PlaylistCandidate(
        playlist_id=playlist_id, duration_seconds=minutes * 60, recommended=recommended,
        streams=streams if streams is not None else (video("video:4113", 0, 3840, 2160), video("video:4117", 1, 1920, 1080)),
    )


def scan_of(*playlists: PlaylistCandidate, kind: DiscKind = DiscKind.UHD) -> DiscScan:
    return DiscScan(
        source=Path("/disc"), disc_kind=kind, content_kind=ContentKind.FILM, playlists=tuple(playlists),
        capabilities=ToolCapabilities(), fingerprint="a" * 64,
    )


def test_the_dovi_tool_summary_gives_profile_layer_type_and_frames() -> None:
    assert parse_layer_summary(SUMMARY_MEL) == DolbyVisionLayer(7, "MEL", 120)
    assert parse_layer_summary(SUMMARY_FEL) == DolbyVisionLayer(7, "FEL", 120)
    assert parse_layer_summary(SUMMARY_P8) == DolbyVisionLayer(8, None, 120)
    with pytest.raises(DynamicHdrError):
        parse_layer_summary("Error: No RPU was found in input file")


def test_only_a_smaller_secondary_hevc_stream_with_the_same_frame_rate_is_a_candidate() -> None:
    assert [ordinal for ordinal, _ in enhancement_layer_candidates(playlist())] == [1]
    assert enhancement_layer_candidates(playlist(streams=(video("video:4113", 0, 3840, 2160),))) == ()
    bigger = (video("video:4113", 0, 1920, 1080), video("video:4117", 1, 3840, 2160))
    assert enhancement_layer_candidates(playlist(streams=bigger)) == ()
    other_rate = (video("video:4113", 0, 3840, 2160), video("video:4117", 1, 1920, 1080, rate="25/1"))
    assert enhancement_layer_candidates(playlist(streams=other_rate)) == ()


def test_the_probe_copies_a_few_frames_of_one_stream_of_the_playlist() -> None:
    source, extract = layer_probe_commands(Path("/disc"), "00001", 1, Path("/work/p.rpu"))
    assert source[source.index("-playlist") + 1] == "1" and source[source.index("-map") + 1] == "0:v:1"
    assert "bluray:/disc" in source and source[source.index("-frames:v") + 1] == "120"
    assert extract == ["dovi_tool", "extract-rpu", "-o", str(Path("/work/p.rpu")), "-"]


def test_marking_the_base_layer_records_the_enhancement_stream() -> None:
    marked = with_dolby_vision_layer(playlist(), 1, DolbyVisionLayer(7, "MEL", 120))
    base = marked.video_streams[0].video
    assert base.dolby_vision and base.dolby_vision_profile == 7
    assert base.dolby_vision_el_stream_id == "video:4117" and base.dolby_vision_el_type == "MEL"
    assert marked.video_streams[1].video.dolby_vision is False  # the EL itself is left alone


class Probe:
    """Scripted run/run_pipeline: the dovi_tool answer per probed ordinal."""

    def __init__(self, answers: dict[int, str | Exception], tmp_path: Path) -> None:
        self.answers = answers
        self.probed: list[int] = []
        self.tmp_path = tmp_path

    def run_pipeline(self, commands: Any, **kwargs: Any) -> None:
        ordinal = int(commands[0][commands[0].index("-map") + 1].split(":")[-1])
        self.probed.append(ordinal)
        answer = self.answers[ordinal]
        if isinstance(answer, Exception):
            raise answer
        Path(commands[1][commands[1].index("-o") + 1]).write_bytes(b"rpu")
        self.current = answer

    def run(self, argv: Any, **kwargs: Any) -> None:
        kwargs["stdout_path"].write_text(self.current, encoding="utf-8")


def detect(scan: DiscScan, probe: Probe, tmp_path: Path, *, tool: bool = True) -> DiscScan:
    return detect_dolby_vision_layers(
        scan, Path("/disc"), run_pipeline=probe.run_pipeline, run=probe.run,
        read_text=lambda path: path.read_text(encoding="utf-8"), work=tmp_path / "work",
        logs=tmp_path / "logs", tool_available=tool,
    )


def test_a_profile_7_disc_is_recognised_and_the_probe_file_is_removed(tmp_path: Path) -> None:
    probe = Probe({1: SUMMARY_MEL}, tmp_path)
    result = detect(scan_of(playlist()), probe, tmp_path)
    base = result.playlists[0].video_streams[0].video
    assert base.dolby_vision and base.dolby_vision_profile == 7 and base.dolby_vision_el_stream_id == "video:4117"
    assert not list((tmp_path / "work").glob("probe-*.rpu"))


@pytest.mark.parametrize(
    "answer",
    [
        ProcessFailure(ProcessResult(("dovi_tool", "extract-rpu"), 1, 0.0, 1.0, None, None)),
        "Summary:\n  Frames: 120\n  Profile: 8\n",
        "garbage",
    ],
    ids=["no-rpu", "profile-8", "unreadable"],
)
def test_a_secondary_stream_without_a_profile_7_rpu_changes_nothing(tmp_path: Path, answer: Any) -> None:
    original = scan_of(playlist())
    assert detect(original, Probe({1: answer}, tmp_path), tmp_path) == original


def test_only_uhd_discs_and_hosts_with_dovi_tool_are_probed(tmp_path: Path) -> None:
    probe = Probe({1: SUMMARY_MEL}, tmp_path)
    blu_ray = scan_of(playlist(), kind=DiscKind.BD)
    assert detect(blu_ray, probe, tmp_path) == blu_ray
    uhd = scan_of(playlist())
    assert detect(uhd, probe, tmp_path, tool=False) == uhd
    assert probe.probed == []


def test_short_playlists_are_not_probed(tmp_path: Path) -> None:
    probe = Probe({1: SUMMARY_MEL}, tmp_path)
    main, extra = playlist("00001", 117.0), playlist("00099", 5.0, recommended=False)
    result = detect(scan_of(main, extra), probe, tmp_path)
    assert probe.probed == [1]  # only the main playlist
    assert result.playlists[0].video_streams[0].video.dolby_vision
    assert result.playlists[1] == extra


def test_the_plan_for_a_profile_7_disc_reads_the_rpu_from_the_enhancement_layer() -> None:
    plan = resolve_dynamic_hdr(
        DynamicHdrMode.AUTO, encoder="x265", hdr10_enabled=True, progressive=True, crop_enabled=True,
        dolby_vision=True, dolby_vision_profile=7, hdr10_base_layer=True, hdr10_plus=False,
        dolby_vision_el_ordinal=1, dolby_vision_el_type="MEL",
    )
    assert plan.mode is DynamicHdrMode.DOLBY_VISION and plan.convert_mode == 2 and plan.el_video_ordinal == 1
    assert "FEL" not in plan.reason

    from bdencode.hdr_dynamic import DynamicHdrPlan, dovi_extract_commands

    assert DynamicHdrPlan.from_dict(plan.to_dict()) == plan
    source, extract = dovi_extract_commands(Path("ref.mkv"), Path("rpu.bin"), plan)
    assert source[source.index("-map") + 1] == "0:v:1"
    assert extract[:4] == ["dovi_tool", "-m", "2", "-c"] and extract[-4:] == ["extract-rpu", "-o", "rpu.bin", "-"]


def test_a_full_enhancement_layer_is_flagged_as_dropped_and_profile_8_keeps_the_base_layer() -> None:
    fel = resolve_dynamic_hdr(
        DynamicHdrMode.DOLBY_VISION, encoder="x265", hdr10_enabled=True, progressive=True, crop_enabled=False,
        dolby_vision=True, dolby_vision_profile=7, hdr10_base_layer=True, hdr10_plus=False,
        dolby_vision_el_ordinal=1, dolby_vision_el_type="FEL",
    )
    assert "FEL" in fel.reason and "dropped" in fel.reason
    single = resolve_dynamic_hdr(
        DynamicHdrMode.DOLBY_VISION, encoder="x265", hdr10_enabled=True, progressive=True, crop_enabled=False,
        dolby_vision=True, dolby_vision_profile=8, hdr10_base_layer=True, hdr10_plus=False,
        dolby_vision_el_ordinal=1,
    )
    assert single.el_video_ordinal is None  # an 8.x source carries its RPUs in the base layer
