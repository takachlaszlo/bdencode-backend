from __future__ import annotations

import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.qc.video import extract_y4m_at_timestamp_command
from bdencode.worker import PipelineWorker

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="needs a real ffmpeg"
)


def _output_muxer_index(command: list[str]) -> int:
    return len(command) - 1 - command[::-1].index("-f")


def test_both_y4m_writers_pass_strict_minus_one_to_the_output_muxer() -> None:
    # FFmpeg's Y4M muxer only writes 8-bit yuv420p/422p/444p/gray by default;
    # any 10-bit pixel format needs ``-strict -1`` (otherwise exit code 234).
    frame = extract_y4m_at_timestamp_command(
        Path("encode.mkv"), Decimal("0"), Path("frame.y4m")
    )
    reference = PipelineWorker._reference_y4m_pipeline(
        Path("reference.vpy"), 71, Path("reference.y4m")
    )[1]
    for command in (frame, reference):
        strict = command.index("-strict")
        assert command[strict + 1] == "-1"
        assert strict < _output_muxer_index(command)
        assert command[_output_muxer_index(command) + 1] == "yuv4mpegpipe"


def _ten_bit_clip(tmp_path: Path) -> Path:
    clip = tmp_path / "ten-bit.mkv"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=size=64x64:rate=24:duration=1",
            "-vf", "format=yuv420p10le", "-c:v", "ffv1", "-y", str(clip),
        ],
        check=True,
    )
    return clip


@needs_ffmpeg
def test_real_ffmpeg_extracts_a_ten_bit_native_yuv_frame(tmp_path: Path) -> None:
    clip = _ten_bit_clip(tmp_path)
    output = tmp_path / "frame.y4m"

    completed = subprocess.run(
        extract_y4m_at_timestamp_command(clip, Decimal("0"), output),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_bytes().startswith(b"YUV4MPEG2")
    assert b"C420p10" in output.read_bytes()[:120]


@needs_ffmpeg
def test_real_ffmpeg_turns_a_piped_ten_bit_reference_frame_into_y4m(
    tmp_path: Path,
) -> None:
    clip = _ten_bit_clip(tmp_path)
    piped = tmp_path / "from-vspipe.y4m"
    # What vspipe would send for a 10-bit title.
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-i", str(clip),
            "-frames:v", "1", "-strict", "-1", "-f", "yuv4mpegpipe", "-y",
            str(piped),
        ],
        check=True,
    )
    command = PipelineWorker._reference_y4m_pipeline(
        Path("reference.vpy"), 0, tmp_path / "reference.y4m"
    )[1]

    with piped.open("rb") as stdin:
        completed = subprocess.run(
            command, stdin=stdin, capture_output=True, text=True
        )

    assert completed.returncode == 0, completed.stderr
    assert b"C420p10" in (tmp_path / "reference.y4m").read_bytes()[:120]
