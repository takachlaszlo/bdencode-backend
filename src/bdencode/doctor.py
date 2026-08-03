"""Deployment diagnostics that never expose credential contents."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from .capabilities import capability_snapshot
from .config import Settings
from .db import Database


MANDATORY_TOOLS = (
    "ffmpeg",
    "ffprobe",
    "mkvmerge",
    "mkvinfo",
    "mkvextract",
    "mediainfo",
    "vspipe",
    "vmaf",
    "bdencode-vmaf",
    "bdencode-libbluray-scan",
)
RECOMMENDED_TOOLS = ("x264", "x265")
MANDATORY_FFMPEG_ENCODERS = {"libx264", "libx265", "flac"}
MANDATORY_FFMPEG_FILTERS = {
    "ssim",
    "psnr",
    "ebur128",
    "astats",
    "aphasemeter",
    "showspectrumpic",
    "zscale",
    "tonemap",
}
MANDATORY_FFMPEG_PROTOCOLS = {"bluray"}


def _path_check(path: Path, *, writable: bool) -> dict[str, Any]:
    exists = path.exists()
    result: dict[str, Any] = {
        "path": str(path),
        "exists": exists,
        "readable": exists and os.access(path, os.R_OK),
        "writable": exists and os.access(path, os.W_OK),
    }
    if exists:
        usage = shutil.disk_usage(path)
        result["free_bytes"] = usage.free
        result["total_bytes"] = usage.total
    result["ok"] = bool(
        result["readable"] and (result["writable"] if writable else True)
    )
    return result


def _credential_status() -> dict[str, Any]:
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    candidates = []
    if credential_dir:
        candidates.append(Path(credential_dir) / "imgbb-api-key")
    candidates.append(Path.home() / ".config" / "bdencode" / "imgbb-api-key.cred")
    for candidate in candidates:
        try:
            details = candidate.stat()
        except OSError:
            continue
        permissions = stat.S_IMODE(details.st_mode)
        return {
            "configured": details.st_size > 0,
            "encrypted_at_rest": candidate.suffix == ".cred",
            "permissions": f"{permissions:04o}",
            "permissions_ok": permissions & 0o077 == 0,
            # Deliberately omit path/content: neither is needed in attachable logs.
        }
    return {"configured": False, "encrypted_at_rest": False, "permissions_ok": False}


def _vapoursynth_plugins() -> dict[str, Any]:
    vspipe = shutil.which("vspipe")
    if not vspipe:
        return {"ok": False, "plugins": [], "error": "vspipe missing"}
    python = Path(vspipe).with_name("python")
    if not python.is_file():
        return {"ok": False, "plugins": [], "error": "tool Python missing"}
    code = (
        "from vapoursynth import core; "
        "import json; "
        "print(json.dumps({n:hasattr(core,n) for n in ('bs','bwdif','vivtc','resize')}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
        plugins = json.loads(completed.stdout) if completed.returncode == 0 else {}
        return {
            "ok": completed.returncode == 0 and all(plugins.values()),
            "plugins": plugins,
            "error": completed.stderr.strip()[-1000:] if completed.returncode else None,
        }
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return {"ok": False, "plugins": [], "error": type(exc).__name__}


def build_report(
    database: Database, settings: Settings, *, prepare: bool = True
) -> dict[str, Any]:
    """Build a complete runtime report.

    The operator-facing CLI keeps its historical preparation behavior.  API
    status reads pass ``prepare=False`` so inspecting an unprepared deployment
    cannot create its configured data or database directories as a side
    effect.
    """

    if prepare:
        settings.create_directories()

    database_available = (
        prepare
        or database.display_path == ":memory:"
        or Path(database.display_path).expanduser().is_file()
    )
    if database_available:
        database.initialize()
    snapshot = capability_snapshot((*MANDATORY_TOOLS, *RECOMMENDED_TOOLS))
    tools = snapshot["tools"]
    missing_mandatory = [
        name for name in MANDATORY_TOOLS if not tools[name]["available"]
    ]
    missing_recommended = [
        name for name in RECOMMENDED_TOOLS if not tools[name]["available"]
    ]
    source_checks = [
        _path_check(path, writable=False) for path in settings.source_roots
    ]
    data_check = _path_check(settings.data_root, writable=True)
    vs = _vapoursynth_plugins()
    ffmpeg = snapshot["ffmpeg"]
    warnings: list[str] = []
    if not database_available:
        warnings.append("database is not initialized")
    if "libvmaf" not in ffmpeg["filters"]:
        warnings.append(
            "FFmpeg libvmaf filter missing; the official standalone VMAF CLI will be used"
        )
    if missing_recommended:
        warnings.append("recommended tools missing: " + ", ".join(missing_recommended))
    if not _credential_status()["configured"]:
        warnings.append("ImgBB upload credential is not configured")
    missing_ffmpeg = {
        "encoders": sorted(MANDATORY_FFMPEG_ENCODERS - set(ffmpeg["encoders"])),
        "filters": sorted(MANDATORY_FFMPEG_FILTERS - set(ffmpeg["filters"])),
        "protocols": sorted(MANDATORY_FFMPEG_PROTOCOLS - set(ffmpeg["protocols"])),
    }
    if any(missing_ffmpeg.values()):
        warnings.append("mandatory FFmpeg capabilities missing")
    ok = (
        database_available
        and not missing_mandatory
        and not any(missing_ffmpeg.values())
        and vs["ok"]
        and data_check["ok"]
        and all(item["ok"] for item in source_checks)
    )
    active_job = database.active_job() if database_available else None
    return {
        "status": "ok" if ok else "error",
        "database": {
            "path": database.display_path,
            "schema_version": (
                database.schema_version() if database_available else None
            ),
            "active_job": active_job.id if active_job else None,
        },
        "paths": {"data": data_check, "sources": source_checks},
        "host": snapshot["host"],
        "tools": tools,
        "ffmpeg": ffmpeg,
        "missing_ffmpeg_capabilities": missing_ffmpeg,
        "vapoursynth": vs,
        "imgbb_credential": _credential_status(),
        "worker_cpu_policy": {
            "requested_percent": settings.cpu_limit_percent,
            "logical_cpus": os.cpu_count(),
            "systemd_cpu_quota_percent": (os.cpu_count() or 1)
            * settings.cpu_limit_percent,
        },
        "warnings": warnings,
    }


def run_doctor(database: Database, settings: Settings) -> int:
    report = build_report(database, settings)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1
