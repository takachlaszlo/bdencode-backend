"""Browser-playable excerpts of a finished MKV for the built-in player.

Browsers cannot play most release MKVs directly (HEVC, DTS, FLAC, PGS, HDR).
The player therefore asks for a short excerpt that is transcoded on demand to
H.264/AAC in MP4 (HDR is tone-mapped to SDR), cached under
``<data>/cache/previews/<job>`` and served with HTTP range support.

The excerpt is a *viewing aid*: it is never attached to the release, never part
of the completed tree or the torrent, and can always be regenerated.  ``ffmpeg``
and ``ffprobe`` are run without a shell, with a hard timeout, one at a time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

MIN_DURATION = 5.0
MAX_DURATION = 30.0
DEFAULT_DURATION = 20.0
ALLOWED_HEIGHTS = (360, 480, 720)
DEFAULT_HEIGHT = 720
MAX_PREVIEWS_PER_JOB = 12
MAX_CACHE_BYTES = 2 * 1024**3
TRANSCODE_TIMEOUT_SECONDS = 240
PREVIEW_NAME_RE = re.compile(
    r"^preview-(?P<start>\d{1,6})s-(?P<duration>\d{1,2})s-(?P<height>\d{3})p-"
    r"(?P<key>[0-9a-f]{10})\.mp4$"
)
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_TRANSCODE_LOCK = threading.Lock()


class PreviewError(RuntimeError):
    """A preview cannot be produced (``code`` is stable for API clients)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    start_seconds: int
    duration_seconds: int = int(DEFAULT_DURATION)
    height: int = DEFAULT_HEIGHT

    def __post_init__(self) -> None:
        for name in ("start_seconds", "duration_seconds", "height"):
            if type(getattr(self, name)) is not int:
                raise PreviewError("invalid", f"{name} must be an integer")
        if self.start_seconds < 0:
            raise PreviewError("invalid", "start_seconds cannot be negative")
        if not MIN_DURATION <= self.duration_seconds <= MAX_DURATION:
            raise PreviewError(
                "invalid",
                f"duration_seconds must be between {MIN_DURATION:g} and {MAX_DURATION:g}",
            )
        if self.height not in ALLOWED_HEIGHTS:
            raise PreviewError(
                "invalid", "height must be one of " + ", ".join(map(str, ALLOWED_HEIGHTS))
            )


def preview_filter(height: int, *, hdr: bool) -> str:
    """Video filter chain: optional HDR->SDR tone map, scale, 8-bit 4:2:0."""

    scale = f"scale=-2:{height}:flags=bicubic"
    if hdr:
        return (
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
            f"{scale},format=yuv420p"
        )
    return f"{scale},format=yuv420p"


def preview_command(
    source: Path,
    output: Path,
    request: PreviewRequest,
    *,
    hdr: bool,
    has_audio: bool,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        # The API service has no CPU quota (only the worker does), so keep an
        # on-demand excerpt from competing with a running encode for every core.
        "-filter_threads",
        "2",
        "-ss",
        str(request.start_seconds),
        "-i",
        str(source),
        "-t",
        str(request.duration_seconds),
        "-map",
        "0:v:0",
    ]
    if has_audio:
        command.extend(("-map", "0:a:0"))
    command.extend(
        (
            "-sn",
            "-dn",
            "-vf",
            preview_filter(request.height, hdr=hdr),
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
        )
    )
    command.extend(("-c:a", "aac", "-b:a", "128k", "-ac", "2") if has_audio else ("-an",))
    command.extend(("-movflags", "+faststart", "-f", "mp4", "-y", str(output)))
    return command


def probe_command(source: Path, *, ffprobe: str = "ffprobe") -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        "-of",
        "json",
        str(source),
    ]


def parse_media_info(document: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize ffprobe JSON for the player (duration, chapters, tracks)."""

    streams = [s for s in document.get("streams", []) if isinstance(s, Mapping)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    transfer = str((video or {}).get("color_transfer") or "")
    duration = None
    try:
        duration = float(document.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        duration = None

    def tag(stream: Mapping[str, Any], name: str) -> str | None:
        tags = stream.get("tags")
        value = tags.get(name) if isinstance(tags, Mapping) else None
        return str(value) if value else None

    chapters = []
    for item in document.get("chapters", []):
        if not isinstance(item, Mapping):
            continue
        try:
            start = float(item.get("start_time"))
        except (TypeError, ValueError):
            continue
        title = tag(item, "title") or f"Chapter {len(chapters) + 1}"
        chapters.append({"start_seconds": round(start, 3), "title": title[:120]})
    return {
        "duration_seconds": duration,
        "video": (
            {
                "codec": video.get("codec_name"),
                "width": video.get("width"),
                "height": video.get("height"),
                "pix_fmt": video.get("pix_fmt"),
                "hdr": transfer in _HDR_TRANSFERS,
                "color_transfer": transfer or None,
            }
            if video
            else None
        ),
        "audio": [
            {
                "codec": s.get("codec_name"),
                "channels": s.get("channels"),
                "language": tag(s, "language"),
                "title": tag(s, "title"),
            }
            for s in streams
            if s.get("codec_type") == "audio"
        ],
        "subtitles": sum(1 for s in streams if s.get("codec_type") == "subtitle"),
        "chapters": chapters,
    }


Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
    )


class PreviewService:
    """Generates and serves cached excerpts for completed jobs."""

    def __init__(
        self,
        cache_root: Path,
        *,
        runner: Runner = _run,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        self.cache_root = Path(cache_root)
        self.runner = runner
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def directory(self, job_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
            raise PreviewError("invalid", "invalid job id")
        return self.cache_root / "previews" / job_id

    def _invoke(
        self, argv: Sequence[str], timeout: float
    ) -> "subprocess.CompletedProcess[str]":
        try:
            return self.runner(argv, timeout)
        except subprocess.TimeoutExpired as exc:
            raise PreviewError("timeout", "the media tool took too long") from exc
        except OSError as exc:
            raise PreviewError(
                "unavailable", "ffmpeg/ffprobe is not installed or cannot be started"
            ) from exc

    # -- inspection ---------------------------------------------------------------
    def media_info(self, source: Path) -> dict[str, Any]:
        completed = self._invoke(probe_command(source, ffprobe=self.ffprobe), 60)
        if completed.returncode != 0:
            raise PreviewError("probe_failed", "ffprobe could not read the media file")
        try:
            return parse_media_info(json.loads(completed.stdout))
        except ValueError as exc:
            raise PreviewError("probe_failed", "ffprobe returned invalid JSON") from exc

    # -- generation -----------------------------------------------------------------
    def _key(self, source: Path, request: PreviewRequest) -> str:
        details = source.stat()
        material = f"{source.name}|{details.st_size}|{details.st_mtime_ns}|{request}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]

    def name_for(self, source: Path, request: PreviewRequest) -> str:
        return (
            f"preview-{request.start_seconds}s-{request.duration_seconds}s-"
            f"{request.height}p-{self._key(source, request)}.mp4"
        )

    def ensure(
        self, job_id: str, source: Path, request: PreviewRequest
    ) -> tuple[dict[str, Any], bool]:
        """Return ``(record, created)``; an existing valid excerpt is reused."""

        if not source.is_file():
            raise PreviewError("no_output", "the finished MKV is not available")
        info = self.media_info(source)
        duration = info.get("duration_seconds")
        if duration is not None and request.start_seconds >= duration:
            raise PreviewError("invalid", "start_seconds is beyond the end of the film")
        directory = self.directory(job_id)
        target = directory / self.name_for(source, request)
        if target.is_file() and target.stat().st_size > 0:
            return self._record(target), False
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        partial = directory / f".{target.name}.partial"
        video = info.get("video") or {}
        command = preview_command(
            source,
            partial,
            request,
            hdr=bool(video.get("hdr")),
            has_audio=bool(info.get("audio")),
            ffmpeg=self.ffmpeg,
        )
        # The whole create-and-publish sequence runs under the lock.  Requests
        # for the same excerpt share one partial file name, so another thread
        # must never start (or clean up) while this one is still writing or
        # renaming it; a request that waited also re-checks for a finished file.
        with _TRANSCODE_LOCK:
            try:
                if target.is_file() and target.stat().st_size > 0:
                    return self._record(target), False
                completed = self._invoke(command, TRANSCODE_TIMEOUT_SECONDS)
                if (
                    completed.returncode != 0
                    or not partial.is_file()
                    or partial.stat().st_size == 0
                ):
                    raise PreviewError(
                        "transcode_failed",
                        "ffmpeg could not create the excerpt"
                        + (f": {completed.stderr.strip()[-300:]}" if completed.stderr else ""),
                    )
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)
            self._prune(directory, keep=target.name)
        return self._record(target), True

    # -- listing / serving -----------------------------------------------------------------
    def _record(self, path: Path) -> dict[str, Any]:
        match = PREVIEW_NAME_RE.fullmatch(path.name)
        assert match is not None
        return {
            "name": path.name,
            "start_seconds": int(match["start"]),
            "duration_seconds": int(match["duration"]),
            "height": int(match["height"]),
            "size_bytes": path.stat().st_size,
            "created_at": path.stat().st_mtime,
        }

    def list(self, job_id: str) -> list[dict[str, Any]]:
        directory = self.directory(job_id)
        if not directory.is_dir():
            return []
        records = [
            self._record(path)
            for path in directory.iterdir()
            if PREVIEW_NAME_RE.fullmatch(path.name) and path.is_file()
        ]
        return sorted(records, key=lambda item: item["start_seconds"])

    def path_for(self, job_id: str, name: str) -> Path:
        if PREVIEW_NAME_RE.fullmatch(name) is None:
            raise PreviewError("invalid", "invalid preview name")
        path = self.directory(job_id) / name
        if not path.is_file():
            raise PreviewError("not_found", "preview not found")
        return path

    def delete(self, job_id: str, name: str) -> None:
        self.path_for(job_id, name).unlink()

    def _prune(self, directory: Path, *, keep: str) -> None:
        files = sorted(
            (p for p in directory.iterdir() if PREVIEW_NAME_RE.fullmatch(p.name)),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in files)
        while files and (len(files) > MAX_PREVIEWS_PER_JOB or total > MAX_CACHE_BYTES):
            victim = files.pop(0)
            if victim.name == keep:
                continue
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
