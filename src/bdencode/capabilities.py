"""Runtime tool discovery and immutable provenance snapshots."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .hdr_dynamic import (
    DOVI_TOOL,
    HDR10PLUS_TOOL,
    x265_support_from_help,
)
from .process import CommandRunner


@dataclass(frozen=True, slots=True)
class ToolCapability:
    name: str
    path: str | None
    version: str | None
    sha256: str | None

    @property
    def available(self) -> bool:
        return self.path is not None


VERSION_ARGS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
    "x264": ("--version",),
    "x265": ("--version",),
    "vspipe": ("--version",),
    "mkvmerge": ("--version",),
    "mkvinfo": ("--version",),
    "mkvextract": ("--version",),
    "mediainfo": ("--Version",),
    "bd_info": ("--version",),
    "bdencode-libbluray-scan": ("--help",),
    "tsMuxeR": ("--help",),
    "whisper-cli": ("--help",),
    "vmaf": ("--version",),
    "bdencode-vmaf": ("--help",),
    "hdr10plus_tool": ("--version",),
    "dovi_tool": ("--version",),
}


def _hash_binary(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def discover_tool(name: str, runner: CommandRunner | None = None) -> ToolCapability:
    resolved = shutil.which(name)
    if not resolved:
        return ToolCapability(name, None, None, None)
    path = Path(resolved).resolve(strict=True)
    command_runner = runner or CommandRunner()
    version = None
    try:
        completed = command_runner.capture(
            [path, *VERSION_ARGS.get(name, ("--version",))], check=False
        )
        content = (completed.stdout or completed.stderr).strip()
        version = content.splitlines()[0][:500] if content else None
    except (OSError, TimeoutError, subprocess.SubprocessError):
        # A hung or crashing --version probe (TimeoutExpired is a
        # SubprocessError, not a TimeoutError) must not abort the whole
        # snapshot that the doctor, the API and the job manifest depend on.
        version = None
    return ToolCapability(name, str(path), version, _hash_binary(path))


def ffmpeg_features(runner: CommandRunner | None = None) -> dict[str, list[str]]:
    command_runner = runner or CommandRunner()
    if not shutil.which("ffmpeg"):
        return {
            "encoders": [],
            "filters": [],
            "protocols": [],
            "bitstream_filters": [],
        }
    result: dict[str, list[str]] = {}
    for category, flag in (
        ("encoders", "-encoders"),
        ("filters", "-filters"),
        ("protocols", "-protocols"),
        ("bitstream_filters", "-bsfs"),
    ):
        try:
            completed = command_runner.capture(
                ["ffmpeg", "-hide_banner", flag], check=False
            )
            text = completed.stdout + completed.stderr
        except (OSError, subprocess.SubprocessError):
            # Report the capabilities as absent (fail closed) instead of
            # raising from the diagnostics path.
            text = ""
        wanted = {
            "encoders": ("libx264", "libx265", "flac", "ac3", "eac3", "dca"),
            "filters": (
                "libvmaf",
                "ssim",
                "psnr",
                "signalstats",
                "ebur128",
                "astats",
                "aphasemeter",
                "showspectrumpic",
                "zscale",
                "tonemap",
                "drawtext",
                "pad",
            ),
            "protocols": ("bluray",),
            "bitstream_filters": ("dca_core",),
        }[category]
        result[category] = sorted(
            item for item in wanted if re.search(rf"\b{re.escape(item)}\b", text)
        )
    return result


def capability_snapshot(names: Iterable[str] | None = None) -> dict[str, object]:
    selected = tuple(names or VERSION_ARGS)
    runner = CommandRunner()
    return {
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
        },
        "tools": {
            item.name: asdict(item) | {"available": item.available}
            for item in (discover_tool(name, runner) for name in selected)
        },
        "ffmpeg": ffmpeg_features(runner),
    }


def x265_build_support(runner: CommandRunner | None = None) -> dict[str, bool]:
    """Whether the installed x265 accepts the dynamic-HDR parameters.

    FFmpeg's libx265 wrapper merely warns about an unknown parameter and then
    encodes without it, so support has to be established up front.  A missing
    CLI or a failing probe reports every feature as unsupported (fail closed).
    """

    if not shutil.which("x265"):
        return x265_support_from_help("")
    command_runner = runner or CommandRunner()
    try:
        completed = command_runner.capture(["x265", "--help"], check=False)
        text = f"{completed.stdout}\n{completed.stderr}"
    except (OSError, TimeoutError, subprocess.SubprocessError):
        text = ""
    return x265_support_from_help(text)


def dynamic_hdr_support(
    runner: CommandRunner | None = None,
) -> dict[str, dict[str, object]]:
    """Availability of HDR10+ and Dolby Vision retention on this host."""

    command_runner = runner or CommandRunner()
    build = x265_build_support(command_runner)
    result: dict[str, dict[str, object]] = {}
    for mode, tool_name in (
        ("hdr10plus", HDR10PLUS_TOOL),
        ("dolby_vision", DOVI_TOOL),
    ):
        tool = discover_tool(tool_name, command_runner)
        # HDR10+ metadata is injected into the finished stream, so only the tool is needed; Dolby
        # Vision additionally needs an x265 that accepts its profile signalling.
        needs_x265 = mode == "dolby_vision"
        result[mode] = {
            "tool": tool_name,
            "tool_available": tool.available,
            "tool_version": tool.version,
            "x265_supported": build[mode],
            "method": "inject",
            "available": tool.available and (build[mode] or not needs_x265),
        }
    return result
