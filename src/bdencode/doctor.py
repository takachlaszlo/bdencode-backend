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
WORKER_SERVICE = "bdencode-worker.service"
API_SERVICE = "bdencode-api.service"
CREDENTIAL_SERVICE_DROPINS = {
    WORKER_SERVICE: Path(
        "/etc/systemd/system/bdencode-worker.service.d/credential.conf"
    ),
    API_SERVICE: Path("/etc/systemd/system/bdencode-api.service.d/credential.conf"),
}


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


def _service_active(service: str) -> bool | None:
    """Return the observable systemd state without turning it into a hard error."""

    try:
        completed = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode in {3, 4}:
        return False
    return None


def _credential_consumer_status(
    name: str,
    encrypted_path: Path,
    service: str,
    *,
    service_active: bool | None,
) -> dict[str, Any]:
    """Describe whether a credential is wired to its real consumer service.

    The API must never receive image-host secrets merely so it can inspect the
    worker.  Instead, it verifies the root-owned systemd binding and reports the
    consumer service state.  This distinguishes "ready for the worker" from
    "loaded in the API process" without reading or exposing secret material.
    """

    dropin = CREDENTIAL_SERVICE_DROPINS[service]
    service_bound = False
    binding_present = os.path.lexists(dropin)
    if binding_present:
        try:
            details = dropin.lstat()
            if stat.S_ISREG(details.st_mode) and not stat.S_ISLNK(details.st_mode):
                expected = f"LoadCredentialEncrypted={name}:{encrypted_path}"
                service_bound = expected in {
                    line.strip()
                    for line in dropin.read_text(encoding="utf-8").splitlines()
                }
        except (OSError, UnicodeError):
            service_bound = False
    return {
        "consumer_service": service,
        "service_binding_present": binding_present,
        "service_bound": service_bound,
        "service_active": service_active,
    }


def _credential_status(
    name: str,
    *,
    consumer_service: str | None = None,
    consumer_active: bool | None = None,
) -> dict[str, Any]:
    credential_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    encrypted_path = Path.home() / ".config" / "bdencode" / f"{name}.cred"
    candidates: list[tuple[Path, bool]] = []
    if credential_dir:
        candidates.append((Path(credential_dir) / name, True))
    candidates.append((encrypted_path, False))
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
        configured = metadata_ok and details.st_size > 0
        result: dict[str, Any] = {
            "configured": configured,
            "present": True,
            # For another systemd service this process cannot safely inspect
            # the private runtime credential directory.  Null means
            # "not observable here", not "missing from the worker".
            "runtime_loaded": configured if is_runtime else None,
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
        if consumer_service is not None:
            consumer = _credential_consumer_status(
                name,
                encrypted_path,
                consumer_service,
                service_active=consumer_active,
            )
            result.update(consumer)
            result["ready_for_consumer"] = bool(
                configured and (is_runtime or consumer["service_bound"])
            )
        return result
    result = {
        "configured": False,
        "present": False,
        "runtime_loaded": False,
        "encrypted_at_rest": False,
        "runtime_plaintext": False,
        "permissions_ok": False,
        "owner_ok": False,
        "metadata_ok": False,
    }
    if consumer_service is not None:
        consumer = _credential_consumer_status(
            name,
            encrypted_path,
            consumer_service,
            service_active=consumer_active,
        )
        result.update(consumer)
        result["ready_for_consumer"] = False
    return result


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
    worker_active = _service_active(WORKER_SERVICE)
    api_active = _service_active(API_SERVICE)
    image_upload_credentials = {
        "imgbb": _credential_status(
            "imgbb-api-key",
            consumer_service=WORKER_SERVICE,
            consumer_active=worker_active,
        ),
        "catbox": _credential_status(
            "catbox-userhash",
            consumer_service=WORKER_SERVICE,
            consumer_active=worker_active,
        ),
        "freeimage": _credential_status(
            "freeimage-api-key",
            consumer_service=WORKER_SERVICE,
            consumer_active=worker_active,
        ),
    }
    release_credentials = {
        "qbittorrent_username": _credential_status(
            "qbittorrent-username",
            consumer_service=API_SERVICE,
            consumer_active=api_active,
        ),
        "qbittorrent_password": _credential_status(
            "qbittorrent-password",
            consumer_service=API_SERVICE,
            consumer_active=api_active,
        ),
        "aither_api_token": _credential_status(
            "tracker-aither-api-token",
            consumer_service=API_SERVICE,
            consumer_active=api_active,
        ),
    }
    ai_credential = _credential_status(
        "openai-api-key",
        consumer_service=API_SERVICE,
        consumer_active=api_active,
    )
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
    for label, credential in image_upload_credentials.items():
        if credential["configured"] and not credential["ready_for_consumer"]:
            warnings.append(
                f"{label} upload credential is not bound to {WORKER_SERVICE}"
            )
    if ai_credential["configured"] and not ai_credential["ready_for_consumer"]:
        warnings.append(
            f"OpenAI API credential is not bound to {API_SERVICE}"
        )
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
        "ai_recommendation": {
            "provider": "openai",
            "model": settings.ai_model,
            "credential": ai_credential,
        },
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
