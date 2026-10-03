"""Detect hardware video decoding that ffmpeg can use."""

from __future__ import annotations

import functools
import subprocess


@functools.lru_cache(maxsize=None)
def cuda_decode_available(ffmpeg: str = "ffmpeg") -> bool:
    """Whether ffmpeg can open a CUDA device (NVDEC decoding), checked once per process.

    On WSL the NVIDIA driver of the Windows host provides CUDA; without a GPU, driver or a
    CUDA-enabled ffmpeg the device cannot be created and the CPU decoder is used.
    """

    try:
        completed = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-v", "error",
                "-init_hw_device", "cuda=probe",
                "-f", "lavfi", "-i", "nullsrc=s=64x64",
                "-frames:v", "1", "-f", "null", "-",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0
