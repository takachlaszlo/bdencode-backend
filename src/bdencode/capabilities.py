"""Runtime tool discovery and immutable provenance snapshots."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

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
    except (OSError, TimeoutError):
        version = None
    return ToolCapability(name, str(path), version, _hash_binary(path))


def ffmpeg_features(runner: CommandRunner | None = None) -> dict[str, list[str]]:
    command_runner = runner or CommandRunner()
    if not shutil.which("ffmpeg"):
        return {"encoders": [], "filters": [], "protocols": []}
    result: dict[str, list[str]] = {}
    for category, flag in (
        ("encoders", "-encoders"),
        ("filters", "-filters"),
        ("protocols", "-protocols"),
    ):
        completed = command_runner.capture(
            ["ffmpeg", "-hide_banner", flag], check=False
        )
        text = completed.stdout + completed.stderr
        wanted = {
            "encoders": ("libx264", "libx265", "flac"),
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
