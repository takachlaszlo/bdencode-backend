"""FastAPI surface for the backend core (no frontend assets)."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse

from .db import (
    Database,
    NotFoundError,
    QueueBlockedError,
    StateConflictError,
)
from .config import load_settings
from .models import (
    BLOCKING_STATES,
    TERMINAL_STATES,
    Artifact,
    ArtifactCreate,
    ArtifactList,
    CapabilitiesResponse,
    ContentType,
    DiscType,
    Event,
    EventCreate,
    EventList,
    HealthResponse,
    Job,
    JobCreate,
    JobList,
    JobProgressRequest,
    JobSelectionRequest,
    JobState,
    JobTransitionRequest,
    ListMeta,
    QueueClaimResponse,
    Scan,
    ScanCreate,
    ScanList,
    ScanState,
    ScanUpdate,
    SelectionValidationResponse,
    allowed_transitions,
)
from .queue import JobQueue
from .config import ConfigurationError, Settings
from .doctor import build_report
from .media.profiles import (
    DetailLevel,
    VideoEncoder,
    profile_schema,
    recommended_profile,
)
from .media.planner import EncodePlanner, EncodeRequest
from .analyzer import MkvAnalyzer
from .utils import sha256_file
from .worker import (
    ReviewRequired,
    _field_handling,
    _planner_crop,
    _scan_from_dict,
    parse_selection,
)


API_PREFIX = "/api/v1"
API_VERSION = "1"


def default_database_path() -> Path:
    configured = os.environ.get("BDENCODE_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    return load_settings().resolved_database_path


def create_app(
    database: Database | str | Path | None = None,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    if isinstance(database, Database):
        db = database
    else:
        db = Database(database or default_database_path())
    queue = JobQueue(db)

    application = FastAPI(
        title="BDEncode Backend",
        version=API_VERSION,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    application.state.database = db
    application.state.queue = queue
    application.state.settings = settings

    @application.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @application.exception_handler(StateConflictError)
    async def conflict_handler(
        _request: Request, exc: StateConflictError
    ) -> JSONResponse:
        content: dict[str, str] = {"detail": str(exc)}
        if exc.current is not None:
            content["current_state"] = exc.current.value
        return JSONResponse(status_code=409, content=content)

    @application.exception_handler(QueueBlockedError)
    async def blocked_handler(
        _request: Request, exc: QueueBlockedError
    ) -> JSONResponse:
        content: dict[str, object] = {"detail": str(exc)}
        if exc.active_job is not None:
            content["active_job"] = exc.active_job.model_dump(mode="json")
        return JSONResponse(status_code=409, content=content)

    @application.exception_handler(ConfigurationError)
    async def configuration_handler(
        _request: Request, exc: ConfigurationError
    ) -> JSONResponse:
        content: dict[str, object] = {"detail": str(exc)}
        if exc.code:
            content["code"] = exc.code
        if exc.context:
            content["context"] = exc.context
        return JSONResponse(status_code=422, content=content)

    @application.get(f"{API_PREFIX}/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        active = db.active_job()
        return HealthResponse(
            status="ok",
            database=db.display_path,
            schema_version=db.schema_version(),
            active_job_id=active.id if active else None,
            blocking_state=active.state if active else None,
            queued_jobs=queue.queued_count(),
        )

    @application.get(f"{API_PREFIX}/capabilities", response_model=CapabilitiesResponse)
    def capabilities() -> CapabilitiesResponse:
        transitions = {
            state.value: sorted(allowed_transitions(state), key=lambda item: item.value)
            for state in JobState
        }
        return CapabilitiesResponse(
            api_version=API_VERSION,
            job_states=list(JobState),
            terminal_states=sorted(TERMINAL_STATES, key=lambda item: item.value),
            blocking_states=sorted(BLOCKING_STATES, key=lambda item: item.value),
            transitions=transitions,
            input_video_codecs=["AVC", "VC-1", "MPEG-2", "HEVC"],
            output_video_codecs=["x264", "x265"],
            disc_types=list(DiscType),
            content_types=list(ContentType),
            detail_levels=["beginner", "advanced", "pro"],
            audio_actions=["copy", "flac", "omit"],
            constraints={
                "max_active_jobs": 1,
                "queued_jobs_allowed": True,
                "cpu_budget_fraction": 0.8,
                "supports_3d": False,
                "dolby_vision_retention": False,
                "hdr_modes": ["SDR", "HDR10"],
                "comparison_images": "lossless PNG",
            },
        )

    @application.get(f"{API_PREFIX}/runtime-capabilities")
    def runtime_capabilities() -> dict[str, object]:
        runtime_settings = settings or load_settings()
        return build_report(db, runtime_settings, prepare=False)

    @application.get(f"{API_PREFIX}/profiles/{{encoder}}/schema")
    def encoder_schema(
        encoder: VideoEncoder,
        detail_level: DetailLevel = DetailLevel.BEGINNER,
    ) -> dict[str, object]:
        return {
            "encoder": encoder.value,
            "detail_level": detail_level.value,
            "fields": profile_schema(encoder, detail_level),
        }

    @application.get(f"{API_PREFIX}/profiles/{{encoder}}/recommendation")
    def encoder_recommendation(
        encoder: VideoEncoder,
        detail_level: DetailLevel = DetailLevel.BEGINNER,
        content_type: str = "film",
    ) -> dict[str, object]:
        profile = recommended_profile(
            encoder,
            detail_level=detail_level,
            content_type=content_type,
        )
        return {
            "source": "deterministic_expert_rules",
            "requires_operator_confirmation": True,
            "settings": profile.to_dict(),
        }

    @application.get(f"{API_PREFIX}/sources")
    def browse_sources(path: str | None = None) -> dict[str, object]:
        if settings is None:
            return {"roots": [], "path": None, "entries": []}
        selected = (
            settings.source_roots[0]
            if path is None
            else settings.authorize_source(path)
        )
        if not selected.is_dir():
            raise ConfigurationError("source browser path must be a directory")
        entries: list[dict[str, object]] = []
        for item in sorted(selected.iterdir(), key=lambda value: value.name.casefold()):
            if item.is_symlink():
                try:
                    settings.authorize_source(item)
                except (ConfigurationError, OSError):
                    continue
            if not item.is_dir():
                continue
            entries.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "is_bluray": (item / "BDMV" / "PLAYLIST").is_dir(),
                }
            )
            if len(entries) >= 1000:
                break
        return {
            "roots": [str(root) for root in settings.source_roots],
            "path": str(selected),
            "entries": entries,
        }

    @application.get(f"{API_PREFIX}/analyze-mkv")
    def analyze_mkv(path: str) -> dict[str, object]:
        target = Path(path).expanduser().resolve(strict=True)
        if settings is not None and not (
            target.is_relative_to(settings.completed_root)
            or target.is_relative_to(settings.jobs_root)
        ):
            raise ConfigurationError("MKV analysis is limited to job/completed roots")
        return MkvAnalyzer().analyze(target).to_dict()

    @application.post(
        f"{API_PREFIX}/jobs", response_model=Job, status_code=status.HTTP_201_CREATED
    )
    def create_job(request: JobCreate) -> Job:
        if settings is not None:
            source = settings.authorize_source(request.source_path)
            if request.work_path is not None:
                work = Path(request.work_path).expanduser().resolve(strict=False)
                if not work.is_relative_to(settings.jobs_root):
                    raise ConfigurationError("work_path must be inside the jobs root")
            if request.output_path is not None:
                output = Path(request.output_path).expanduser().resolve(strict=False)
                if not output.is_relative_to(settings.completed_root):
                    raise ConfigurationError(
                        "output_path must be inside the completed root"
                    )
            request = request.model_copy(update={"source_path": str(source)})
        return queue.enqueue(request)

    @application.get(f"{API_PREFIX}/jobs", response_model=JobList)
    def list_jobs(
        states: Annotated[list[JobState] | None, Query(alias="state")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JobList:
        items = db.list_jobs(states=states, limit=limit, offset=offset)
        return JobList(
            items=items,
            meta=ListMeta(limit=limit, offset=offset, count=len(items)),
        )

    @application.post(
        f"{API_PREFIX}/jobs/claim-next", response_model=QueueClaimResponse
    )
    def claim_next() -> QueueClaimResponse:
        return queue.claim_status()

    @application.get(f"{API_PREFIX}/jobs/{{job_id}}", response_model=Job)
    def get_job(job_id: str) -> Job:
        return db.get_job(job_id)

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/transition", response_model=Job)
    def transition_job(job_id: str, request: JobTransitionRequest) -> Job:
        return queue.advance(
            job_id,
            request.state,
            message=request.message,
            details=request.details,
            expected_version=request.expected_version,
        )

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/selection", response_model=Job)
    def select_job(job_id: str, request: JobSelectionRequest) -> Job:
        return db.set_selection(
            job_id,
            request.selection,
            message=request.message,
            expected_version=request.expected_version,
        )

    @application.post(
        f"{API_PREFIX}/jobs/{{job_id}}/selection/validate",
        response_model=SelectionValidationResponse,
    )
    def validate_job_selection(
        job_id: str, request: JobSelectionRequest
    ) -> SelectionValidationResponse:
        job = db.get_job(job_id)
        if job.state not in {JobState.AWAITING_SELECTION, JobState.NEEDS_REVIEW}:
            raise StateConflictError(
                "selection is only validated while awaiting selection or review",
                current=job.state,
            )
        if (
            request.expected_version is not None
            and job.version != request.expected_version
        ):
            raise StateConflictError(
                f"job version is {job.version}, expected {request.expected_version}",
                current=job.state,
            )

        scans = db.list_scans(job_id=job_id, limit=500)
        scan_row = next(
            (
                item
                for item in scans
                if item.status in {ScanState.AWAITING_SELECTION, ScanState.COMPLETED}
            ),
            None,
        )
        if scan_row is None:
            raise StateConflictError(
                "job has no successful scan to validate against", current=job.state
            )

        candidate = job.model_copy(update={"selection": request.selection})
        try:
            scan = _scan_from_dict(scan_row.result)
            selection = parse_selection(candidate, scan)
        except ReviewRequired as exc:
            details = dict(exc.details)
            code = details.pop("code", None)
            raise ConfigurationError(
                str(exc),
                code=str(code) if code else None,
                context=details,
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(f"stored scan result is invalid: {exc}") from exc

        work_root = (
            settings.data_root if settings is not None else Path.cwd()
        ).resolve(strict=False)
        preview_root = work_root / ".selection-validation" / job.id
        try:
            plan = EncodePlanner(work_root=work_root).build(
                EncodeRequest(
                    scan=scan,
                    playlist_id=selection.playlist_id,
                    settings=selection.settings,
                    work_dir=preview_root / "work",
                    output_path=preview_root / "output.mkv",
                    track_selections=selection.tracks,
                    field_handling=_field_handling(selection.temporal_filter),
                    crop=_planner_crop(selection.crop),
                    angle=selection.angle,
                    overwrite=True,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"selection cannot be planned safely: {exc}"
            ) from exc

        return SelectionValidationResponse(
            valid=True,
            playlist_id=selection.playlist_id,
            encoder=selection.settings.encoder.value,
            settings=selection.settings.to_dict(),
            ffmpeg_video_args=list(selection.settings.ffmpeg_video_args()),
            crop=asdict(selection.crop),
            temporal_filter=selection.temporal_filter.value,
            advisory_warnings=list(plan.warnings),
        )

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/progress", response_model=Job)
    def job_progress(job_id: str, request: JobProgressRequest) -> Job:
        return db.record_progress(
            job_id,
            request.progress,
            message=request.message,
            details=request.details,
        )

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/resume", response_model=Job)
    def resume_job(job_id: str) -> Job:
        return queue.resume_review(job_id)

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/retry-upload", response_model=Job)
    def retry_upload(job_id: str) -> Job:
        return queue.retry_upload(job_id)

    @application.delete(f"{API_PREFIX}/jobs/{{job_id}}", response_model=Job)
    def cancel_job(job_id: str) -> Job:
        return queue.cancel(job_id)

    @application.post(
        f"{API_PREFIX}/scans", response_model=Scan, status_code=status.HTTP_201_CREATED
    )
    def create_scan(request: ScanCreate) -> Scan:
        return db.create_scan(request)

    @application.get(f"{API_PREFIX}/scans", response_model=ScanList)
    def list_scans(
        job_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ScanList:
        items = db.list_scans(job_id=job_id, limit=limit, offset=offset)
        return ScanList(
            items=items,
            meta=ListMeta(limit=limit, offset=offset, count=len(items)),
        )

    @application.get(f"{API_PREFIX}/scans/{{scan_id}}", response_model=Scan)
    def get_scan(scan_id: str) -> Scan:
        return db.get_scan(scan_id)

    @application.patch(f"{API_PREFIX}/scans/{{scan_id}}", response_model=Scan)
    def update_scan(scan_id: str, request: ScanUpdate) -> Scan:
        return db.update_scan(scan_id, request)

    @application.post(
        f"{API_PREFIX}/artifacts",
        response_model=Artifact,
        status_code=status.HTTP_201_CREATED,
    )
    def create_artifact(request: ArtifactCreate) -> Artifact:
        return db.create_artifact(request)

    @application.get(f"{API_PREFIX}/artifacts", response_model=ArtifactList)
    def list_artifacts(
        job_id: str | None = None,
        scan_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ArtifactList:
        items = db.list_artifacts(
            job_id=job_id, scan_id=scan_id, limit=limit, offset=offset
        )
        return ArtifactList(
            items=items,
            meta=ListMeta(limit=limit, offset=offset, count=len(items)),
        )

    @application.get(f"{API_PREFIX}/artifacts/{{artifact_id}}", response_model=Artifact)
    def get_artifact(artifact_id: str) -> Artifact:
        return db.get_artifact(artifact_id)

    @application.get(
        f"{API_PREFIX}/artifacts/{{artifact_id}}/content", response_class=FileResponse
    )
    def artifact_content(artifact_id: str) -> FileResponse:
        artifact = db.get_artifact(artifact_id)
        target = Path(artifact.path).expanduser().resolve(strict=True)
        if settings is not None and not (
            target.is_relative_to(settings.jobs_root)
            or target.is_relative_to(settings.completed_root)
        ):
            raise ConfigurationError("artifact path is outside job/completed roots")
        if not target.is_file():
            raise NotFoundError(f"artifact file is missing: {artifact_id}")
        if artifact.sha256 and sha256_file(target) != artifact.sha256.lower():
            raise StateConflictError("artifact hash verification failed")
        return FileResponse(
            target,
            media_type=artifact.mime_type or "application/octet-stream",
            filename=artifact.name,
            # Comparison and spectrum PNGs are consumed directly by the
            # authenticated web UI.  Keep every other artifact downloadable.
            content_disposition_type=(
                "inline" if artifact.mime_type == "image/png" else "attachment"
            ),
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @application.post(
        f"{API_PREFIX}/events",
        response_model=Event,
        status_code=status.HTTP_201_CREATED,
    )
    def create_event(request: EventCreate) -> Event:
        return db.add_event(request)

    @application.get(f"{API_PREFIX}/events", response_model=EventList)
    def list_events(
        job_id: str | None = None,
        scan_id: str | None = None,
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> EventList:
        items = db.list_events(
            job_id=job_id, scan_id=scan_id, after_id=after_id, limit=limit
        )
        cursor = items[-1].id if items else after_id
        return EventList(items=items, after_id=cursor)

    return application


# Keeps ``uvicorn bdencode.api:app`` useful; no database is opened until a route
# actually accesses it, so importing the module has no filesystem side effects.
app = create_app()
