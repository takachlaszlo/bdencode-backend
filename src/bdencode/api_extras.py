"""Operator-facing extras: statistics, profile library, player, DB backups.

These routes live outside :mod:`bdencode.api` to keep that module focused on the
queue/pipeline contract.  They use the same ``/api/v1`` prefix, the same
same-origin mutation guard and the same error envelope.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import ConfigurationError, Settings
from .db import Database, NotFoundError, PersistenceError, StateConflictError
from .db_backup import BackupError, list_backups
from .job_statistics import compute_job_statistics, completed_statistics
from .models import ArtifactKind, JobState
from .previews import PreviewError, PreviewRequest, PreviewService
from .profile_library import (
    ProfileExists,
    ProfileLibrary,
    ProfileLibraryError,
    ProfileNotFound,
)

#: Written by the root-owned daily release check; world-readable, no secrets.
RELEASE_UPDATE_STATUS_PATH = Path("/var/lib/bdencode/release-update/status.json")

_PREVIEW_STATUS = {
    "invalid": 422,
    "no_output": 409,
    "not_found": 404,
    "unavailable": 503,
    "timeout": 504,
    "probe_failed": 502,
    "transcode_failed": 502,
}
_LIBRARY_STATUS = {
    "not_found": 404,
    "exists": 409,
    "unsupported_version": 422,
    "too_large": 413,
    "limit": 409,
}


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: int = Field(default=0, ge=0, le=86_400)
    duration_seconds: int = Field(default=20, ge=5, le=30)
    height: Literal[360, 480, 720] = 720


def register_extra_routes(
    application: FastAPI,
    *,
    db: Database,
    settings: Settings | None,
    prefix: str,
) -> None:
    in_memory = db.path == ":memory:"
    base = Path(db.path).expanduser().parent if not in_memory else None

    def library() -> ProfileLibrary:
        directory = (
            settings.state_root / "profile-library"
            if settings is not None
            else (base / "profile-library" if base else None)
        )
        if directory is None:
            raise ConfigurationError("the profile library is not configured")
        return ProfileLibrary(directory)

    def backup_directory() -> Path:
        return settings.state_root / "backups" if settings is not None else db.backup_directory

    def preview_service() -> PreviewService:
        service = getattr(application.state, "preview_service", None)
        if service is not None:
            return service
        root = (
            settings.cache_root
            if settings is not None
            else (base / "cache" if base else None)
        )
        if root is None:
            raise PreviewError("unavailable", "previews need a configured cache")
        application.state.preview_service = PreviewService(root)
        return application.state.preview_service

    def job_root(job_id: str) -> Path | None:
        return settings.job_root(job_id) if settings is not None else None

    # -- error envelope ---------------------------------------------------------------------
    @application.exception_handler(ProfileLibraryError)
    async def library_error(_request: Request, exc: ProfileLibraryError) -> JSONResponse:
        return JSONResponse(
            status_code=_LIBRARY_STATUS.get(exc.code, 422),
            content={"detail": str(exc), "code": exc.code},
        )

    @application.exception_handler(PreviewError)
    async def preview_error(_request: Request, exc: PreviewError) -> JSONResponse:
        return JSONResponse(
            status_code=_PREVIEW_STATUS.get(exc.code, 500),
            content={"detail": str(exc), "code": exc.code},
        )

    @application.exception_handler(BackupError)
    async def backup_error(_request: Request, exc: BackupError) -> JSONResponse:
        return JSONResponse(
            status_code=500, content={"detail": str(exc), "code": "backup_failed"}
        )

    # -- statistics ----------------------------------------------------------------------------
    @application.get(f"{prefix}/statistics")
    def statistics(
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        # ``list_jobs`` orders by queue priority and *oldest first*; take every
        # completed job, then keep the ``limit`` most recently finished.
        jobs = db.list_jobs(states=[JobState.COMPLETED], limit=5000)
        jobs.sort(key=lambda item: item.finished_at or item.updated_at, reverse=True)
        jobs = jobs[:limit]
        return completed_statistics(
            jobs,
            lambda job_id: db.list_events(job_id=job_id, limit=5000),
            lambda job_id: db.list_artifacts(job_id=job_id, limit=1000),
            job_root,
        )

    @application.get(f"{prefix}/jobs/{{job_id}}/statistics")
    def job_statistics(job_id: str) -> dict[str, Any]:
        job = db.get_job(job_id)
        return compute_job_statistics(
            job,
            db.list_events(job_id=job_id, limit=5000),
            db.list_artifacts(job_id=job_id, limit=1000),
            job_root(job_id),
        )

    # -- profile library ----------------------------------------------------------------------------
    @application.get(f"{prefix}/profile-library")
    def list_profiles() -> dict[str, Any]:
        items = library().list()
        return {
            "items": [
                {**item.to_dict(), "selection": item.selection_fragment()} for item in items
            ],
            "count": len(items),
        }

    @application.get(f"{prefix}/profile-library/export")
    def export_library() -> JSONResponse:
        return JSONResponse(
            library().export_bundle(),
            headers={
                "Content-Disposition": 'attachment; filename="bdencode-profiles.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.post(f"{prefix}/profile-library/import")
    def import_profiles(
        document: Annotated[dict[str, Any], Body()],
        on_conflict: Literal["rename", "skip", "overwrite"] = "rename",
    ) -> dict[str, Any]:
        return library().import_document(document, on_conflict=on_conflict)

    @application.post(f"{prefix}/profile-library", status_code=status.HTTP_201_CREATED)
    def save_profile(
        document: Annotated[dict[str, Any], Body()], overwrite: bool = False
    ) -> dict[str, Any]:
        saved = library().save(document, overwrite=overwrite)
        return {**saved.to_dict(), "selection": saved.selection_fragment()}

    @application.get(f"{prefix}/profile-library/{{profile_id}}")
    def get_profile(profile_id: str) -> dict[str, Any]:
        item = library().get(profile_id)
        return {**item.to_dict(), "selection": item.selection_fragment()}

    @application.get(f"{prefix}/profile-library/{{profile_id}}/export")
    def export_profile(profile_id: str) -> JSONResponse:
        document = library().export(profile_id)
        return JSONResponse(
            document,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{profile_id}.bdencode-profile.json"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.delete(
        f"{prefix}/profile-library/{{profile_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_profile(profile_id: str) -> Response:
        library().delete(profile_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # -- built-in player -------------------------------------------------------------------------------
    def output_file(job_id: str) -> Path:
        job = db.get_job(job_id)
        artifact = next(
            (
                item
                for item in db.list_artifacts(job_id=job_id, limit=1000)
                if item.kind is ArtifactKind.OUTPUT
            ),
            None,
        )
        if artifact is None:
            raise StateConflictError(
                "the job has no finished MKV yet", current=job.state
            )
        path = Path(artifact.path)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise NotFoundError("the finished MKV is missing") from exc
        if settings is not None and not (
            resolved.is_relative_to(settings.completed_root)
            or resolved.is_relative_to(settings.jobs_root)
        ):
            raise ConfigurationError("output path is outside the completed/job roots")
        return resolved

    @application.get(f"{prefix}/jobs/{{job_id}}/player")
    def player_info(job_id: str) -> dict[str, Any]:
        source = output_file(job_id)
        service = preview_service()
        return {
            **service.media_info(source),
            "previews": service.list(job_id),
            "limits": {
                "min_duration_seconds": 5,
                "max_duration_seconds": 30,
                "heights": [360, 480, 720],
            },
        }

    @application.get(f"{prefix}/jobs/{{job_id}}/previews")
    def list_previews(job_id: str) -> dict[str, Any]:
        db.get_job(job_id)
        return {"items": preview_service().list(job_id)}

    @application.post(
        f"{prefix}/jobs/{{job_id}}/previews", status_code=status.HTTP_201_CREATED
    )
    def create_preview(
        job_id: str, body: PreviewBody, response: Response
    ) -> dict[str, Any]:
        source = output_file(job_id)
        record, created = preview_service().ensure(
            job_id,
            source,
            PreviewRequest(body.start_seconds, body.duration_seconds, body.height),
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return {**record, "created": created}

    @application.get(
        f"{prefix}/jobs/{{job_id}}/previews/{{name}}", response_class=FileResponse
    )
    def preview_content(job_id: str, name: str) -> FileResponse:
        db.get_job(job_id)
        path = preview_service().path_for(job_id, name)
        return FileResponse(
            path,
            media_type="video/mp4",
            content_disposition_type="inline",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
        )

    @application.delete(
        f"{prefix}/jobs/{{job_id}}/previews/{{name}}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_preview(job_id: str, name: str) -> Response:
        db.get_job(job_id)
        preview_service().delete(job_id, name)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # -- database status and backups ---------------------------------------------------------------------------
    @application.get(f"{prefix}/system/database")
    def database_status() -> dict[str, Any]:
        path = Path(db.path).expanduser()
        listing = list_backups(backup_directory())
        return {
            "path": db.display_path,
            "schema_version": db.schema_version(),
            "size_bytes": path.stat().st_size if path.is_file() else None,
            "integrity": db.integrity_check(),
            "migrations": db.migration_history(),
            "backup_count": len(listing),
            "latest_backup": listing[0].to_dict() if listing else None,
        }

    @application.get(f"{prefix}/system/backups")
    def backups() -> dict[str, Any]:
        directory = backup_directory()
        return {
            "directory": str(directory),
            "items": [item.to_dict() for item in list_backups(directory)],
        }

    @application.get(f"{prefix}/system/release-update")
    def release_update_status() -> dict[str, Any]:
        """The daily release check's last outcome (written by bdencode-release-update)."""

        path = Path(os.environ.get("BDENCODE_RELEASE_STATUS_PATH") or RELEASE_UPDATE_STATUS_PATH)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"available": False, "status": None}
        if not isinstance(document, dict):
            return {"available": False, "status": None}
        keys = (
            "state", "message", "checked_at", "last_successful_check_at", "installed_version",
            "latest_version", "latest_tag", "installed_at", "installed_commit", "media_updates",
        )
        return {"available": True, "status": {key: document.get(key) for key in keys if key in document}}

    @application.post(f"{prefix}/system/backups", status_code=status.HTTP_201_CREATED)
    def create_backup_now() -> dict[str, Any]:
        try:
            info = db.backup("manual", backup_directory())
        except PersistenceError as exc:
            raise ConfigurationError(str(exc)) from exc
        return info.to_dict()
