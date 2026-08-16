from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from bdencode.media import (
    BluRayScanner,
    ContentKind,
    Crop,
    DiscKind,
    DiscScan,
    EncodePlanner,
    EncodeRequest,
    EncoderSettings,
    FieldHandling,
    Hdr10Metadata,
    HdrStaticMetadata,
    LanguageResolver,
    LanguageStatus,
    MediaStream,
    PlaylistCandidate,
    PlaylistSegment,
    StreamKind,
    ToolCapabilities,
    TrackAction,
    TrackSelection,
    VideoCodec,
    VideoEncoder,
    VideoProperties,
    recommended_profile,
)


MASTERING = "G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(10000000,1)"


def _stream_set(video: VideoProperties) -> tuple[MediaStream, ...]:
    resolver = LanguageResolver()
    return (
        MediaStream(
            "video:4113", 0, 4113, StreamKind.VIDEO, video.codec.value, video=video
        ),
        MediaStream(
            "audio:4352",
            1,
            4352,
            StreamKind.AUDIO,
            "truehd",
            language=resolver.resolve(mpls="eng", clpi="eng"),
            channels=8,
            channel_layout="7.1",
            sample_rate=48000,
            bit_depth=24,
            object_audio=True,
        ),
        MediaStream(
            "subtitle:4608",
            2,
            4608,
            StreamKind.SUBTITLE,
            "hdmv_pgs_subtitle",
            language=resolver.resolve(mpls="hun", clpi="hun"),
            forced=True,
        ),
    )


def _scan(
    tmp_path: Path,
    *,
    kind: DiscKind = DiscKind.BD,
    video: VideoProperties | None = None,
) -> DiscScan:
    video = video or VideoProperties(
        VideoCodec.AVC,
        width=1920,
        height=1080,
        frame_rate="24000/1001",
        field_order="progressive",
        bit_depth=8,
        pixel_format="yuv420p",
        color_primaries="bt709",
        color_transfer="bt709",
        color_matrix="bt709",
    )
    playlist = PlaylistCandidate(
        "00800",
        7200,
        segments=(
            PlaylistSegment("00001", 0, 3600),
            PlaylistSegment("00002", 0, 3600, 3600),
        ),
        streams=_stream_set(video),
        seamless_branching=True,
        edition_group="feature",
        edition_label="theatrical",
        recommended=True,
    )
    return DiscScan(
        source=tmp_path / "source",
        disc_kind=kind,
        content_kind=ContentKind.FILM,
        playlists=(playlist,),
        capabilities=ToolCapabilities(ffprobe="ffprobe", ffprobe_bluray=True),
        fingerprint="a" * 64,
    )


def _request(
    tmp_path: Path, scan: DiscScan, settings: EncoderSettings
) -> EncodeRequest:
    return EncodeRequest(
        scan=scan,
        playlist_id="800",
        settings=settings,
        work_dir=tmp_path / "encode" / "jobs" / "one" / "work",
        output_path=tmp_path / "encode" / "completed" / "movie.mkv",
        track_selections=(
            TrackSelection("audio:4352", TrackAction.FLAC, order=0, default=True),
            TrackSelection(
                "subtitle:4608",
                TrackAction.COPY,
                order=1,
                forced=True,
                subtitle_kind="forced",
            ),
        ),
    )


def test_bd_plan_uses_x264_flac_pgs_and_shell_free_argv(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    request = _request(tmp_path, scan, recommended_profile("x264"))
    plan = EncodePlanner(work_root=tmp_path / "encode").build(request)

    assert plan.source_video_codec is VideoCodec.AVC
    assert plan.output_encoder is VideoEncoder.X264
    assert plan.comparison_categories == ("I", "P", "B")
    assert [command.stage for command in plan.commands] == [
        "encode_video",
        "extract_audio_01",
        "extract_subtitle_02",
        "mux",
    ]
    video = plan.commands[0].argv
    assert isinstance(video, tuple)
    assert video[0] == "ffmpeg"
    assert ("-playlist", "800") == video[
        video.index("-playlist") : video.index("-playlist") + 2
    ]
    assert "libx264" in video
    assert video[video.index("-level:v") + 1] == "4.1"
    x264_params = video[video.index("-x264-params") + 1]
    assert "keyint=240" in x264_params
    assert "min-keyint=24" in x264_params
    assert "ref=4" in x264_params
    assert "chroma-qp-offset=0" in x264_params
    assert "shell=True" not in video
    audio = plan.commands[1].argv
    assert ("-c:a", "flac") == audio[audio.index("-c:a") : audio.index("-c:a") + 2]
    assert ("-compression_level", "8") == audio[
        audio.index("-compression_level") : audio.index("-compression_level") + 2
    ]
    subtitle = plan.commands[2].argv
    assert ("-c:s", "copy") == subtitle[
        subtitle.index("-c:s") : subtitle.index("-c:s") + 2
    ]
    assert subtitle[-3:-1] == ("-f", "matroska")
    assert "comparison" not in " ".join(plan.commands[-1].argv).lower()
    mux = plan.commands[-1].argv
    forced_flags = [
        mux[index + 1]
        for index, value in enumerate(mux)
        if value == "--forced-display-flag"
    ]
    assert forced_flags == ["0:no", "0:yes"]
    assert plan.needs_review  # object metadata is intentionally lost by FLAC
    assert plan.decisions["requested_encoder"]["level"] is None
    assert plan.decisions["encoder"]["level"] == "4.1"
    assert plan.decisions["encoder"]["chroma_qp_offset"] == -2
    assert plan.decisions["video_policy"]["h264_level_4_1"]["applied"]


def test_25fps_letterbox_crop_retains_five_refs_at_level_4_1(
    tmp_path: Path,
) -> None:
    video = VideoProperties(
        VideoCodec.AVC,
        width=1920,
        height=1080,
        frame_rate="25",
        field_order="progressive",
        bit_depth=8,
        pixel_format="yuv420p",
        color_primaries="bt709",
        color_transfer="bt709",
        color_matrix="bt709",
    )
    scan = _scan(tmp_path, video=video)
    request = replace(
        _request(tmp_path, scan, recommended_profile("x264")),
        crop=Crop(top=138, bottom=138),
    )

    plan = EncodePlanner(work_root=tmp_path / "encode").build(request)
    command = plan.commands[0].argv
    private = command[command.index("-x264-params") + 1]

    assert "keyint=250" in private
    assert "min-keyint=25" in private
    assert "ref=5" in private
    assert command[command.index("-level:v") + 1] == "4.1"
    assert plan.decisions["video_policy"]["output_dimensions"] == {
        "width": 1920,
        "height": 804,
    }
    assert not any("reduced reference frames" in item for item in plan.warnings)


@pytest.mark.parametrize(
    ("action", "encoder", "bitrate"),
    (
        (TrackAction.AC3, "ac3", "640k"),
        (TrackAction.EAC3, "eac3", "1024k"),
        (TrackAction.DTS, "dca", "1536k"),
    ),
)
def test_planner_builds_fixed_lossy_audio_targets_with_7_1_warning(
    tmp_path: Path, action: TrackAction, encoder: str, bitrate: str
) -> None:
    scan = _scan(tmp_path)
    request = replace(
        _request(tmp_path, scan, recommended_profile("x264")),
        track_selections=(
            TrackSelection("audio:4352", action, language="eng"),
            TrackSelection(
                "subtitle:4608",
                TrackAction.COPY,
                language="hun",
                subtitle_kind="forced",
            ),
        ),
    )

    plan = EncodePlanner(work_root=tmp_path / "encode").build(request)
    audio = plan.commands[1].argv

    assert audio[audio.index("-c:a") + 1] == encoder
    assert audio[audio.index("-b:a") + 1] == bitrate
    assert audio[audio.index("-ar") + 1] == "48000"
    assert audio[audio.index("-ac") + 1] == "6"
    assert any("maximum 5.1" in warning for warning in plan.warnings)


def test_planner_extracts_dts_hd_core_without_reencoding(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    dts_hd = replace(
        scan.playlists[0].audio_streams[0],
        codec="dts",
        codec_profile="DTS-HD MA",
    )
    scan = replace(
        scan,
        playlists=(
            replace(
                scan.playlists[0],
                streams=(
                    scan.playlists[0].video_streams[0],
                    dts_hd,
                    scan.playlists[0].subtitle_streams[0],
                ),
            ),
        ),
    )
    request = replace(
        _request(tmp_path, scan, recommended_profile("x264")),
        track_selections=(
            TrackSelection("audio:4352", TrackAction.DTS, language="eng"),
            TrackSelection(
                "subtitle:4608",
                TrackAction.COPY,
                language="hun",
                subtitle_kind="forced",
            ),
        ),
    )

    plan = EncodePlanner(work_root=tmp_path / "encode").build(request)
    audio = plan.commands[1].argv

    assert audio[audio.index("-c:a") + 1] == "copy"
    assert audio[audio.index("-bsf:a") + 1] == "dca_core"
    assert any("without re-encoding" in warning for warning in plan.warnings)


def test_uhd_plan_keeps_static_hdr10_and_drops_dv_and_hdr10plus(tmp_path: Path) -> None:
    video = VideoProperties(
        VideoCodec.HEVC,
        width=3840,
        height=2160,
        frame_rate="24000/1001",
        field_order="progressive",
        bit_depth=10,
        pixel_format="yuv420p10le",
        color_primaries="bt2020",
        color_transfer="smpte2084",
        color_matrix="bt2020nc",
        hdr10=True,
        hdr10_static=HdrStaticMetadata(MASTERING, 1000, 400),
        dolby_vision=True,
        dolby_vision_profile=7,
        hdr10_base_layer=True,
        hdr10_plus=True,
    )
    scan = _scan(tmp_path, kind=DiscKind.UHD, video=video)
    hdr = Hdr10Metadata(True, MASTERING, 1000, 400)
    settings = recommended_profile("x265", hdr10=hdr)
    plan = EncodePlanner(work_root=tmp_path / "encode").build(
        _request(tmp_path, scan, settings)
    )

    assert "libx265" in plan.commands[0].argv
    assert plan.decisions["hdr10_static_retained"] is True
    assert plan.decisions["dolby_vision_retained"] is False
    assert plan.decisions["dynamic_hdr_retained"] is False
    assert any("Dolby Vision" in warning for warning in plan.warnings)
    assert any("HDR10+" in warning for warning in plan.warnings)


def test_codec_disc_and_3d_policy_are_hard_failures(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    with pytest.raises(ValueError, match="must use x264"):
        EncodePlanner(work_root=tmp_path / "encode").build(
            _request(
                tmp_path,
                scan,
                EncoderSettings(encoder=VideoEncoder.X265, profile="main"),
            )
        )

    three_d = VideoProperties(VideoCodec.MVC, width=1920, height=1080, three_d=True)
    scan_3d = _scan(tmp_path, video=three_d)
    with pytest.raises(ValueError, match="3D"):
        EncodePlanner(work_root=tmp_path / "encode").build(
            _request(tmp_path, scan_3d, recommended_profile("x264"))
        )


def test_track_choices_are_explicit_and_flac_is_audio_only(tmp_path: Path) -> None:
    scan = _scan(tmp_path)
    base = _request(tmp_path, scan, recommended_profile("x264"))
    incomplete = EncodeRequest(
        scan=base.scan,
        playlist_id=base.playlist_id,
        settings=base.settings,
        work_dir=base.work_dir,
        output_path=base.output_path,
        track_selections=(TrackSelection("audio:4352", "copy"),),
    )
    with pytest.raises(ValueError, match="every audio/subtitle"):
        EncodePlanner(work_root=tmp_path / "encode").build(incomplete)

    invalid = EncodeRequest(
        scan=base.scan,
        playlist_id=base.playlist_id,
        settings=base.settings,
        work_dir=base.work_dir,
        output_path=base.output_path,
        track_selections=(
            TrackSelection("audio:4352", "copy"),
            TrackSelection("subtitle:4608", "flac"),
        ),
    )
    with pytest.raises(ValueError, match="only for audio"):
        EncodePlanner(work_root=tmp_path / "encode").build(invalid)

    audio_with_subtitle_flags = replace(
        base,
        track_selections=(
            TrackSelection(
                "audio:4352",
                "copy",
                forced=True,
                subtitle_kind="forced",
            ),
            TrackSelection(
                "subtitle:4608",
                "copy",
                subtitle_kind="forced",
            ),
        ),
    )
    with pytest.raises(ValueError, match="audio tracks cannot define"):
        EncodePlanner(work_root=tmp_path / "encode").build(
            audio_with_subtitle_flags
        )

    lpcm = MediaStream(
        "audio:4352",
        1,
        4352,
        StreamKind.AUDIO,
        "pcm_bluray",
        channels=2,
        channel_layout="stereo",
        sample_rate=48000,
        bit_depth=24,
    )
    lpcm_scan = _scan(tmp_path)
    lpcm_scan = replace(
        lpcm_scan,
        playlists=(
            replace(
                lpcm_scan.playlists[0],
                streams=(
                    lpcm_scan.playlists[0].streams[0],
                    lpcm,
                    lpcm_scan.playlists[0].streams[2],
                ),
            ),
        ),
    )
    lpcm_request = replace(
        base,
        scan=lpcm_scan,
        track_selections=(
            TrackSelection("audio:4352", "copy"),
            TrackSelection("subtitle:4608", "copy"),
        ),
    )
    with pytest.raises(ValueError, match="LPCM cannot be copied"):
        EncodePlanner(work_root=tmp_path / "encode").build(lpcm_request)


def test_interlace_crop_and_b_frame_review_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be even"):
        Crop(left=1)
    with pytest.raises(ValueError, match="must be integers"):
        Crop(left=2.0)  # type: ignore[arg-type]

    assert Crop.from_detected_borders(top=139, bottom=139) == Crop(top=138, bottom=138)
    assert Crop.from_detected_borders(top=139, bottom=139, safety=2) == Crop(
        top=136, bottom=136
    )
    with pytest.raises(ValueError, match="at least 16"):
        Crop(left=954, right=954).output_dimensions(1920, 1080)

    interlaced = VideoProperties(
        VideoCodec.MPEG2,
        width=1920,
        height=1080,
        field_order="tt",
        color_primaries="bt709",
        color_transfer="bt709",
        color_matrix="bt709",
    )
    scan = _scan(tmp_path, video=interlaced)
    request = _request(tmp_path, scan, recommended_profile("x264"))
    with pytest.raises(ValueError, match="interlaced"):
        EncodePlanner(work_root=tmp_path / "encode").build(request)

    settings = recommended_profile("x264", overrides={"bframes": 0})
    filtered = EncodeRequest(
        scan=request.scan,
        playlist_id=request.playlist_id,
        settings=settings,
        work_dir=request.work_dir,
        output_path=request.output_path,
        track_selections=request.track_selections,
        field_handling=FieldHandling.IVTC,
        crop=Crop(top=2, bottom=2),
    )
    plan = EncodePlanner(work_root=tmp_path / "encode").build(filtered)
    assert plan.needs_review
    assert any("B-frame comparison" in warning for warning in plan.warnings)
    assert "fieldmatch" in plan.commands[0].argv[plan.commands[0].argv.index("-vf") + 1]


def test_language_resolution_keeps_provenance_conflicts_and_override() -> None:
    resolver = LanguageResolver()
    declared = resolver.resolve(mpls="ger", clpi="deu", pmt="deu")
    assert declared.iso639_2t == "deu"
    assert declared.bcp47 == "de"
    assert declared.status is LanguageStatus.DECLARED
    assert declared.confidence >= 0.96

    mislabeled = resolver.resolve(
        mpls="eng", clpi="eng", pmt="eng", audio_lid="zho", audio_confidence=0.96
    )
    assert mislabeled.iso639_2t == "zho"
    assert mislabeled.status is LanguageStatus.CONFLICT
    assert mislabeled.needs_review

    conflict = resolver.resolve(
        mpls="eng", clpi="hun", audio_lid="hun", audio_confidence=0.94
    )
    assert conflict.iso639_2t == "hun"
    assert conflict.status is LanguageStatus.CONFLICT
    assert conflict.needs_review
    assert {item.source.value for item in conflict.evidence} == {
        "mpls",
        "clpi",
        "audio_lid",
    }

    override = resolver.resolve(mpls="eng", override="hun", overridden_by="operator")
    assert override.status is LanguageStatus.OVERRIDDEN
    assert override.iso639_2t == "hun"
    assert override.overridden_by == "operator"

    cantonese = resolver.resolve(override="yue", overridden_by="operator")
    assert cantonese.iso639_2t == "yue"
    assert cantonese.bcp47 == "yue"


class FakeRunner:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []

    def capture(self, argv, *, timeout=30, check=True):
        self.calls.append(tuple(str(item) for item in argv))
        return type(
            "Result",
            (),
            {"returncode": 0, "stdout": json.dumps(self.payload), "stderr": ""},
        )()


def test_scanner_is_source_guarded_capability_based_and_mockable(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    disc = storage / "Movie"
    playlist_dir = disc / "BDMV" / "PLAYLIST"
    stream_dir = disc / "BDMV" / "STREAM"
    playlist_dir.mkdir(parents=True)
    stream_dir.mkdir(parents=True)
    (playlist_dir / "00800.mpls").write_bytes(b"MPL0200")
    (stream_dir / "00001.m2ts").write_bytes(b"mock")
    probe = {
        "format": {"duration": "7200"},
        "chapters": [{"start_time": "0"}, {"start_time": "600"}],
        "streams": [
            {
                "index": 0,
                "id": "0x1011",
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv420p",
                "color_primaries": "bt709",
                "color_transfer": "bt709",
                "color_space": "bt709",
                "field_order": "progressive",
            },
            {
                "index": 1,
                "id": "0x1100",
                "codec_type": "audio",
                "codec_name": "truehd",
                "tags": {"language": "eng"},
            },
        ],
    }
    native = {
        "playlists": [
            {
                "id": "800",
                "duration": 7200,
                "seamless_branching": True,
                "edition_group": "feature",
                "edition_label": "extended",
                "segments": [
                    {"clip_id": "00001", "in_time": 0, "out_time": 3600},
                    {
                        "clip_id": "00002",
                        "in_time": 0,
                        "out_time": 3600,
                        "relative_start": 3600,
                    },
                ],
                "streams": [
                    {
                        "pid": 4352,
                        "codec_type": "audio",
                        "mpls_language": "eng",
                        "clpi_language": "eng",
                    }
                ],
            }
        ]
    }
    runner = FakeRunner(probe)
    scanner = BluRayScanner(
        runner=runner,
        capabilities=ToolCapabilities(ffprobe="/usr/bin/ffprobe", ffprobe_bluray=True),
        libbluray_provider=lambda _: native,
        source_root=storage,
    )
    result = scanner.scan(disc, content_kind="concert")

    assert result.disc_kind is DiscKind.BD
    assert result.content_kind is ContentKind.CONCERT
    assert result.has_seamless_branching
    assert result.playlists[0].segments[1].relative_start_seconds == 3600
    assert result.playlists[0].audio_streams[0].language.iso639_2t == "eng"
    assert all(isinstance(call, tuple) for call in runner.calls)
    assert runner.calls[0][0] == "/usr/bin/ffprobe"
    assert "-playlist" in runner.calls[0]

    outside = tmp_path / "outside"
    (outside / "BDMV" / "PLAYLIST").mkdir(parents=True)
    (outside / "BDMV" / "STREAM").mkdir()
    with pytest.raises(ValueError, match="within"):
        scanner.scan(outside)
