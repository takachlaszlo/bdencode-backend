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
from .config import ConfigurationError, Settings
from .db import Database
from .qc.video import COMPARISON_FONT_FILE
from .release_profiles import (
    RELEASE_PROFILE_VALIDATION_ERROR,
    load_release_profiles,
)


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
MANDATORY_FFMPEG_ENCODERS = {"libx264", "libx265", "flac", "ac3", "eac3", "dca"}
MANDATORY_FFMPEG_FILTERS = {
    "ssim",
    "psnr",
    "ebur128",
    "astats",
    "aphasemeter",
    "showspectrumpic",
    "zscale",
    "tonemap",
    "drawtext",
    "pad",
}
MANDATORY_FFMPEG_PROTOCOLS = {"bluray"}
MANDATORY_FFMPEG_BITSTREAM_FILTERS = {"dca_core"}


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


def _data_path_check(settings: Settings) -> dict[str, Any]:
    """Report workspace capacity and the narrow runtime write contract.

    The systemd services deliberately see ``data_root`` itself as read-only.
    Only the state, job, completed-output, release-kit, cache and update
    subtrees are made writable.  The UI's workspace status therefore
    represents those required roots collectively while retaining the root
    mount's actual write result for diagnostics.
    """

    root = _path_check(settings.data_root, writable=False)
    required = {
        "state": settings.state_root,
        "jobs": settings.jobs_root,
        "completed": settings.completed_root,
        "release_kits": settings.release_kits_root,
        "cache": settings.cache_root,
        "updates": settings.updates_root,
    }
    required_checks = {
        name: _path_check(path, writable=True) for name, path in required.items()
    }
    runtime_writable = all(item["ok"] for item in required_checks.values())
    root["root_writable"] = root["writable"]
    root["writable"] = runtime_writable
    root["ok"] = bool(root["readable"] and runtime_writable)
    root["required_writable_paths"] = required_checks
    return root


def _release_profiles_status(settings: Settings) -> dict[str, Any]:
    path = settings.resolved_release_profiles_path
    present = os.path.lexists(path)
    if not present:
        return {
            "path": str(path),
            "present": False,
            "valid": False,
            "configured": False,
            "profile_count": 0,
            "error_code": "missing",
        }
    try:
        document = load_release_profiles(path)
    except ConfigurationError as error:
        return {
            "path": str(path),
            "present": True,
            "valid": False,
            "configured": False,
            "profile_count": 0,
            "error_code": error.code or RELEASE_PROFILE_VALIDATION_ERROR,
        }
    return {
        "path": str(path),
        "present": True,
        "valid": True,
        "configured": bool(document.profiles),
        "profile_count": len(document.profiles),
        "error_code": None,
    }


def _credential_status(name: str) -> dict[str, Any]:
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    candidates: list[tuple[Path, bool]] = []
    if credential_dir:
        candidates.append((Path(credential_dir) / name, True))
    candidates.append(
        (Path.home() / ".config" / "bdencode" / f"{name}.cred", False)
    )
    for candidate, is_runtime in candidates:
        try:
            details = candidate.lstat()
        except OSError:
            continue
        permissions = stat.S_IMODE(details.st_mode)
        regular = stat.S_ISREG(details.st_mode)
        symlink = stat.S_ISLNK(details.st_mode)
        current_uid = os.getuid() if hasattr(os, "getuid") else details.st_uid
        owner_ok = is_runtime or details.st_uid == current_uid
        permissions_ok = (
            permissions & 0o077 == 0 if is_runtime else permissions == 0o600
        )
        metadata_ok = regular and not symlink and owner_ok and permissions_ok
        return {
            "configured": metadata_ok and details.st_size > 0,
            "present": True,
            "runtime_loaded": (
                is_runtime and regular and not symlink and details.st_size > 0
            ),
            # A systemd runtime credential is the decrypted, private tmpfs
            # material.  Only the persistent .cred candidate is itself
            # encrypted at rest.
            "encrypted_at_rest": not is_runtime,
            "runtime_plaintext": is_runtime,
            "source": "systemd-runtime" if is_runtime else "encrypted-file",
            "permissions": f"{permissions:04o}",
            "permissions_ok": permissions_ok,
            "owner_ok": owner_ok,
            "metadata_ok": metadata_ok,
            # Deliberately omit path/content: neither is needed in attachable logs.
        }
    return {
        "configured": False,
        "present": False,
        "runtime_loaded": False,
        "encrypted_at_rest": False,
        "runtime_plaintext": False,
        "permissions_ok": False,
        "owner_ok": False,
        "metadata_ok": False,
    }


def _comparison_font_status() -> dict[str, Any]:
    try:
        details = COMPARISON_FONT_FILE.stat()
    except OSError:
        return {
            "path": str(COMPARISON_FONT_FILE),
            "present": False,
            "readable": False,
            "regular": False,
            "ok": False,
        }
    regular = stat.S_ISREG(details.st_mode)
    readable = os.access(COMPARISON_FONT_FILE, os.R_OK)
    return {
        "path": str(COMPARISON_FONT_FILE),
        "present": True,
        "readable": readable,
        "regular": regular,
        "ok": regular and readable,
    }


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
    data_check = _data_path_check(settings)
    vs = _vapoursynth_plugins()
    ffmpeg = snapshot["ffmpeg"]
    comparison_font = _comparison_font_status()
    image_upload_credentials = {
        "imgbb": _credential_status("imgbb-api-key"),
        "catbox": _credential_status("catbox-userhash"),
        "freeimage": _credential_status("freeimage-api-key"),
    }
    release_credentials = {
        "qbittorrent_username": _credential_status("qbittorrent-username"),
        "qbittorrent_password": _credential_status("qbittorrent-password"),
        "aither_api_token": _credential_status("tracker-aither-api-token"),
    }
    release_profiles = _release_profiles_status(settings)
    warnings: list[str] = []
    if not database_available:
        warnings.append("database is not initialized")
    if "libvmaf" not in ffmpeg["filters"]:
        warnings.append(
            "FFmpeg libvmaf filter missing; the official standalone VMAF CLI will be used"
        )
    if missing_recommended:
        warnings.append("recommended tools missing: " + ", ".join(missing_recommended))
    if not comparison_font["ok"]:
        warnings.append("comparison annotation font is missing or unreadable")
    if not image_upload_credentials["imgbb"]["configured"]:
        warnings.append("ImgBB upload credential is not configured")
    if not image_upload_credentials["catbox"]["configured"]:
        warnings.append(
            "Catbox account credential is not configured; fallback uploads are anonymous"
        )
    if not image_upload_credentials["freeimage"]["configured"]:
        warnings.append("Freeimage upload credential is not configured")
    if release_profiles["present"] and not release_profiles["valid"]:
        warnings.append("release profile configuration is invalid")
    missing_ffmpeg = {
        "encoders": sorted(MANDATORY_FFMPEG_ENCODERS - set(ffmpeg["encoders"])),
        "filters": sorted(MANDATORY_FFMPEG_FILTERS - set(ffmpeg["filters"])),
        "protocols": sorted(MANDATORY_FFMPEG_PROTOCOLS - set(ffmpeg["protocols"])),
        "bitstream_filters": sorted(
            MANDATORY_FFMPEG_BITSTREAM_FILTERS
            - set(ffmpeg.get("bitstream_filters", ()))
        ),
    }
    if any(missing_ffmpeg.values()):
        warnings.append("mandatory FFmpeg capabilities missing")
    ok = (
        database_available
        and not missing_mandatory
        and not any(missing_ffmpeg.values())
        and comparison_font["ok"]
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
        "comparison_annotation": {"font": comparison_font},
        "vapoursynth": vs,
        "image_upload_credentials": image_upload_credentials,
        "release_credentials": release_credentials,
        "release_profiles": release_profiles,
        # Compatibility alias for older frontends and monitoring clients.
        "imgbb_credential": image_upload_credentials["imgbb"],
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
