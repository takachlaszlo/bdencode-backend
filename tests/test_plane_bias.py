from __future__ import annotations

import struct
from pathlib import Path

import pytest

from bdencode.qc.video import MAXIMUM_MEAN_PLANE_BIAS, plane_bias, y4m_plane_means
from bdencode.worker import _mean_plane_bias, _sampled_video_metric_errors


def write_y4m(
    path: Path, *, width: int = 4, height: int = 4, chroma: str = "420", planes: tuple[int, int, int] = (100, 128, 128),
    frames: int = 1, bits: int = 8,
) -> Path:
    tag = f"{chroma}p{bits}" if bits > 8 else chroma
    cw, ch = {"420": (width // 2, height // 2), "422": (width // 2, height), "444": (width, height)}[chroma]
    body = b""
    for _ in range(frames):
        body += b"FRAME\n"
        for value, count in zip(planes, (width * height, cw * ch, cw * ch)):
            body += bytes([value] * count) if bits == 8 else struct.pack(f"<{count}H", *([value] * count))
    path.write_bytes(f"YUV4MPEG2 W{width} H{height} F24000:1001 Ip C{tag}\n".encode() + body)
    return path


def test_plane_means_of_8_and_10_bit_files(tmp_path: Path) -> None:
    assert y4m_plane_means(write_y4m(tmp_path / "a.y4m", planes=(100, 128, 130))) == (100.0, 128.0, 130.0)
    # 10-bit code values are reported on the 8-bit scale: 400/4 = 100.
    ten = write_y4m(tmp_path / "b.y4m", planes=(400, 512, 520), bits=10, frames=3, chroma="422")
    assert y4m_plane_means(ten) == (100.0, 128.0, 130.0)
    assert y4m_plane_means(write_y4m(tmp_path / "c.y4m", chroma="444", planes=(16, 16, 16))) == (16.0, 16.0, 16.0)


def test_unreadable_files_have_no_means(tmp_path: Path) -> None:
    assert y4m_plane_means(tmp_path / "missing.y4m") is None
    garbage = tmp_path / "garbage.y4m"
    garbage.write_bytes(b"not a y4m at all")
    assert y4m_plane_means(garbage) is None
    truncated = write_y4m(tmp_path / "t.y4m")
    truncated.write_bytes(truncated.read_bytes()[:-3])
    assert y4m_plane_means(truncated) is None
    assert plane_bias(garbage, truncated) is None


def test_bias_is_the_signed_encode_minus_reference_error(tmp_path: Path) -> None:
    ref = write_y4m(tmp_path / "ref.y4m", planes=(100, 128, 128))
    enc = write_y4m(tmp_path / "enc.y4m", planes=(112, 126, 129))
    assert plane_bias(ref, enc) == {"y": 12.0, "u": -2.0, "v": 1.0}
    assert plane_bias(ref, ref) == {"y": 0.0, "u": 0.0, "v": 0.0}


def sample(y: float, u: float = 0.0, v: float = 0.0) -> dict[str, object]:
    return {
        "category": "I", "ssim_all": 0.99, "psnr_average_db": 45.0,
        "plane_bias_8bit": {"y": y, "u": u, "v": v},
    }


def test_a_systematic_shift_fails_even_with_a_high_psnr() -> None:
    assert _sampled_video_metric_errors([sample(0.2, -0.1, 0.1) for _ in range(6)]) == ()
    errors = _sampled_video_metric_errors([sample(2.4), sample(2.0), sample(1.8)])
    assert len(errors) == 1 and "Y plane is shifted by +2.07" in errors[0]
    errors = _sampled_video_metric_errors([sample(0, -3.0, 0) for _ in range(4)])
    assert any("U plane" in item for item in errors)


def test_noise_around_zero_averages_out_and_unmeasured_samples_are_ignored() -> None:
    swinging = [sample(4.0), sample(-4.0), sample(3.5), sample(-3.5)]
    assert _sampled_video_metric_errors(swinging) == ()
    bare = {"category": "P", "ssim_all": 0.99, "psnr_average_db": 45.0}
    assert _mean_plane_bias([bare, {**bare, "plane_bias_8bit": None}]) is None
    assert _sampled_video_metric_errors([bare] * 3) == ()
    assert _mean_plane_bias([sample(1.0), sample(3.0)]) == {"y": 2.0, "u": 0.0, "v": 0.0}


def test_the_limit_is_in_8_bit_code_values() -> None:
    assert MAXIMUM_MEAN_PLANE_BIAS == pytest.approx(1.5)
