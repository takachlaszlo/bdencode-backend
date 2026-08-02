from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from bdencode.media.bluray import (
    BluRayScanner,
    StreamKind,
    ToolCapabilities,
    VideoCodec,
)


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
SOURCE = NATIVE / "libbluray_scan.c"
BINARY = NATIVE / "bdencode-libbluray-scan"


def _pkg_config_has_libbluray() -> bool:
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        return False
    return (
        subprocess.run(
            [pkg_config, "--exists", "libbluray"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def test_native_source_has_shell_free_read_only_cli_contract() -> None:
    text = SOURCE.read_text(encoding="utf-8")

    assert "--json" in text
    assert "bd_get_playlist_info" in text
    assert "BLURAY_STREAM_INFO" in text
    assert "system(" not in text
    assert "popen(" not in text
    assert "fopen(" not in text
    assert "shell" not in text.lower()


def test_native_payload_is_consumable_by_bluray_scanner() -> None:
    payload = json.loads(
        """
        {
          "schema_version": 1,
          "playlists": [{
            "id": "00800",
            "duration": 7200.25,
            "angle_count": 2,
            "chapters": [{"index": 0, "start_time": 0.0}],
            "segments": [{
              "clip_id": "00001",
              "in_time": 11.0,
              "out_time": 31.0,
              "relative_start": 0.0,
              "angle": 1
            }],
            "streams": [
              {
                "pid": 4113,
                "coding_type": 27,
                "codec_type": "video",
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "frame_rate": "24000/1001",
                "field_order": "progressive"
              },
              {
                "pid": 4352,
                "coding_type": 131,
                "codec_type": "audio",
                "codec": "truehd",
                "mpls_language": "eng"
              },
              {
                "pid": 4608,
                "coding_type": 144,
                "codec_type": "subtitle",
                "codec": "hdmv_pgs_subtitle",
                "mpls_language": "hun"
              }
            ]
          }]
        }
        """
    )
    scanner = BluRayScanner(
        capabilities=ToolCapabilities(),
        libbluray_provider=lambda _: payload,
    )

    playlist = scanner._playlist_from_payload("00800", {}, payload["playlists"][0])

    assert playlist.duration_seconds == pytest.approx(7200.25)
    assert playlist.chapters == (0.0,)
    assert playlist.angle_count == 2
    assert playlist.segments[0].clip_id == "00001"
    assert playlist.segments[0].duration_seconds == pytest.approx(20.0)
    assert playlist.video_streams[0].video is not None
    assert playlist.video_streams[0].video.codec is VideoCodec.AVC
    assert playlist.audio_streams[0].kind is StreamKind.AUDIO
    assert playlist.audio_streams[0].language is not None
    assert playlist.audio_streams[0].language.iso639_2t == "eng"
    assert playlist.subtitle_streams[0].codec == "hdmv_pgs_subtitle"


@pytest.mark.skipif(
    not _pkg_config_has_libbluray(),
    reason="libbluray development files are unavailable",
)
def test_native_helper_builds_and_keeps_errors_off_stdout() -> None:
    subprocess.run(
        [shutil.which("make") or "make", "-C", str(NATIVE), "clean", "all"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    help_result = subprocess.run(
        [str(BINARY), "--help"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert help_result.stdout.startswith("Usage:")
    assert help_result.stderr == ""

    error_result = subprocess.run(
        [str(BINARY), "--json", str(ROOT / "definitely-missing-disc")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    assert error_result.returncode != 0
    assert error_result.stdout == ""
    assert "Blu-ray" in error_result.stderr
