"""Stream an aligned reference and encode into the official libvmaf CLI.

The Debian 12 FFmpeg build does not provide the ``libvmaf`` filter.  Writing two
feature-length Y4M files merely to call the standalone tool would consume an
unreasonable amount of NVMe space, so this helper connects both producers to
libvmaf through private POSIX FIFOs.  It intentionally never invokes a shell.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Sequence

from .qc.video import standalone_vmaf_command
from .utils import atomic_write_json


class StreamedVmafError(RuntimeError):
    pass


def streamed_vmaf_command(
    script: Path,
    encoded: Path,
    output: Path,
    *,
    hdr10: bool,
    model_4k: bool = False,
    executable: str = "bdencode-vmaf",
) -> list[str]:
    command = [
        executable,
        "--script",
        str(script),
        "--encoded",
        str(encoded),
        "--output",
        str(output),
    ]
    if hdr10:
        command.append("--hdr10")
    if model_4k:
        command.extend(("--model", "vmaf_4k_v0.6.1"))
    return command


def _ffmpeg_y4m_command(
    input_value: str,
    output_fifo: Path,
    *,
    hdr10: bool,
    pipe_input: bool,
    ffmpeg: str,
) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-nostdin", "-v", "warning"]
    if pipe_input:
        command.extend(("-f", "yuv4mpegpipe"))
    command.extend(("-i", input_value, "-map", "0:v:0"))
    if hdr10:
        # VMAF's published HDTV/4K models are SDR models.  Apply one fixed,
        # recorded proof transform to both inputs rather than interpreting PQ
        # code values as if they were SDR luma.
        filters = (
            "zscale=pin=bt2020:tin=smpte2084:min=bt2020nc:rin=tv:"
            "p=bt2020:t=linear:m=bt2020nc:r=tv:npl=100,format=gbrpf32le,"
            "tonemap=mobius:param=0.3:desat=0,"
            "zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p"
        )
    else:
        filters = "format=yuv420p"
    command.extend(
        (
            "-vf",
            filters,
            "-fps_mode",
            "passthrough",
            "-f",
            "yuv4mpegpipe",
            "-y",
            str(output_fifo),
        )
    )
    return command


def _terminate(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10
    for process in processes:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
    for process in processes:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def run_streamed_vmaf(
    script: Path,
    encoded: Path,
    output: Path,
    *,
    hdr10: bool = False,
    model: str = "vmaf_v0.6.1",
    vspipe: str = "vspipe",
    ffmpeg: str = "ffmpeg",
    vmaf: str = "vmaf",
) -> Path:
    """Run VMAF/PSNR/SSIM without materializing lossless intermediate video."""

    if os.name != "posix" or not hasattr(os, "mkfifo"):
        raise StreamedVmafError("streamed VMAF requires POSIX named pipes")
    script = script.resolve(strict=True)
    encoded = encoded.resolve(strict=True)
    output = output.resolve(strict=False)
    output.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    for executable in (vspipe, ffmpeg, vmaf):
        if shutil.which(executable) is None:
            raise StreamedVmafError(
                f"required VMAF executable is missing: {executable}"
            )

    processes: list[subprocess.Popen[bytes]] = []
    prior_handlers: dict[int, object] = {}
    with tempfile.TemporaryDirectory(
        prefix=".vmaf-stream-", dir=output.parent
    ) as temporary:
        temporary_root = Path(temporary)
        reference_fifo = temporary_root / "reference.y4m"
        encoded_fifo = temporary_root / "encoded.y4m"
        partial_output = temporary_root / "result.json"
        os.mkfifo(reference_fifo, 0o600)
        os.mkfifo(encoded_fifo, 0o600)

        consumer = standalone_vmaf_command(
            reference_fifo,
            encoded_fifo,
            partial_output,
            threads=0,
            vmaf=vmaf,
            model=model,
        )
        reference_server = [
            vspipe,
            "--container",
            "y4m",
            str(script),
            "-",
        ]
        reference_converter = _ffmpeg_y4m_command(
            "pipe:0", reference_fifo, hdr10=hdr10, pipe_input=True, ffmpeg=ffmpeg
        )
        encoded_converter = _ffmpeg_y4m_command(
            str(encoded), encoded_fifo, hdr10=hdr10, pipe_input=False, ffmpeg=ffmpeg
        )

        stopping = False

        def stop(_signum: int, _frame: object) -> None:
            nonlocal stopping
            stopping = True

        for number in (signal.SIGINT, signal.SIGTERM):
            try:
                prior_handlers[number] = signal.signal(number, stop)
            except (ValueError, OSError):
                pass
        try:
            # Start the reader first. FIFO opens happen in child processes, so
            # neither producer can block the parent while the graph is built.
            processes.append(
                subprocess.Popen(
                    consumer,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                    shell=False,
                )
            )
            reference_process = subprocess.Popen(
                reference_server,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=None,
                shell=False,
            )
            processes.append(reference_process)
            assert reference_process.stdout is not None
            processes.append(
                subprocess.Popen(
                    reference_converter,
                    stdin=reference_process.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                    shell=False,
                )
            )
            reference_process.stdout.close()
            processes.append(
                subprocess.Popen(
                    encoded_converter,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=None,
                    shell=False,
                )
            )

            while True:
                statuses = [process.poll() for process in processes]
                if stopping:
                    raise StreamedVmafError("streamed VMAF was interrupted")
                failures = [status for status in statuses if status not in (None, 0)]
                if failures:
                    raise StreamedVmafError(
                        "streamed VMAF subprocess failed with exit code "
                        + str(failures[0])
                    )
                if all(status == 0 for status in statuses):
                    break
                time.sleep(0.25)
        except BaseException:
            _terminate(processes)
            raise
        finally:
            for number, handler in prior_handlers.items():
                signal.signal(number, handler)

        try:
            document = json.loads(partial_output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StreamedVmafError("libvmaf did not create valid JSON") from exc
        if not isinstance(document, dict) or not document.get("frames"):
            raise StreamedVmafError("libvmaf JSON contains no frame scores")
        document["bdencode"] = {
            "backend": "official-libvmaf-cli",
            "model": model,
            "additional_features": ["psnr", "float_ssim", "float_ms_ssim"],
            "preprocessing": (
                "hdr10-pq-to-bt709-mobius-proof-transform"
                if hdr10
                else "format-yuv420p"
            ),
            "transport": "private-posix-fifos",
        }
        atomic_write_json(output, document)
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stream VapourSynth and an encode to libvmaf"
    )
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--encoded", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--hdr10", action="store_true")
    parser.add_argument("--model", default="vmaf_v0.6.1")
    args = parser.parse_args(argv)
    try:
        run_streamed_vmaf(
            args.script,
            args.encoded,
            args.output,
            hdr10=args.hdr10,
            model=args.model,
        )
    except (OSError, StreamedVmafError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"bdencode-vmaf: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
