"""FastAPI surface for the backend core (no frontend assets)."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .audio import AUDIO_ACTIONS, audio_presets_payload
from .ai_recommendation import (
    AIRecommendationError,
    AIRecommendationRequest,
    AIRecommendationResponse,
    AIRecommendationService,
    AIRecommendationUnavailable,
    RecommendationContext,
)
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
    ArtifactKind,
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
    JobRetryRequest,
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
from .maintenance import (
    MaintenanceDomainGuard,
    MaintenanceJournal,
    MaintenanceSafetyError,
    MaintenanceTargetSpec,
    inspect_job_storage,
)
from .queue import JobQueue
from .config import ConfigurationError, Settings
from .doctor import build_report
from .media.profiles import (
    DetailLevel,
    VideoEncoder,
    gop_for_frame_rate,
    profile_schema,
    recommended_profile,
)
from .media.planner import EncodePlanner, EncodeRequest
from .analyzer import MkvAnalyzer
from .release import ReleaseMetadata, ReleasePreparationState
from .release_profiles import RELEASE_PROFILE_VALIDATION_ERROR
from .release_service import (
    ReleasePreparationView,
    ReleaseService,
    ReleaseServiceError,
)
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
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TRUSTED_OPERATOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+\-]{0,127}$")


def _is_api_state_change(request: Request) -> bool:
    """Return whether a request crosses the API mutation trust boundary."""

    path = str(request.scope.get("path", ""))
    return request.method.upper() in _STATE_CHANGING_METHODS and (
        path == API_PREFIX or path.startswith(f"{API_PREFIX}/")
    )


def _same_origin_violation(request: Request) -> str | None:
    """Classify browser cross-origin mutations without blocking API clients.

    Non-browser clients normally omit both browser provenance headers and remain
    compatible. ``scope['scheme']`` is deliberate: in production Uvicorn
    derives it from the trusted loopback reverse proxy, while the public
    authority is retained in the proxy-preserved Host header.
    """

    fetch_sites = request.headers.getlist("sec-fetch-site")
    if any(
        token.strip().casefold() == "cross-site"
        for value in fetch_sites
        for token in value.split(",")
    ):
        return "cross-site"

    origins = request.headers.getlist("origin")
    if not origins:
        return None
    hosts = request.headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1:
        return "invalid-origin"

    origin = origins[0].strip()
    host = hosts[0].strip()
    try:
        parsed = urlsplit(origin)
        # Force validation of a possibly malformed explicit port.
        _ = parsed.port
    except ValueError:
        return "invalid-origin"
    expected_scheme = str(request.scope.get("scheme", "")).casefold()
    if (
        expected_scheme not in {"http", "https"}
        or parsed.scheme.casefold() != expected_scheme
        or not parsed.netloc
        or parsed.netloc.casefold() != host.casefold()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return "origin-mismatch"
    return None


class _ApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobControlActionRequest(_ApiRequest):
    expected_control_revision: int | None = Field(default=None, ge=1)
    message: str | None = Field(default=None, max_length=4000)


class CleanupRequest(_ApiRequest):
    scope: Literal["temporary"] = "temporary"
    expected_version: int | None = Field(default=None, ge=1)


class ReleasePreparationCreateRequest(_ApiRequest):
    profile_id: str = Field(min_length=1, max_length=64)
    metadata: ReleaseMetadata


class ReleaseActionRequest(_ApiRequest):
    expected_version: int = Field(ge=1)


class ReleasePublishRequest(ReleaseActionRequest):
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompletedReleaseDeleteRequest(_ApiRequest):
    confirmation: str = Field(min_length=1, max_length=255)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    force_if_seeded: bool = False
    preparation_versions: dict[str, int]

    @field_validator("preparation_versions", mode="before")
    @classmethod
    def validate_preparation_versions(cls, value: Any) -> dict[str, int]:
        if (
            not isinstance(value, Mapping)
            or len(value) > 1000
            or any(
                not isinstance(identifier, str)
                or not re.fullmatch(r"[0-9a-f]{32}", identifier)
                or type(version) is not int
                or version < 1
                for identifier, version in value.items()
            )
        ):
            raise ValueError("invalid release preparation version snapshot")
        return dict(value)


def default_database_path() -> Path:
    configured = os.environ.get("BDENCODE_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    return load_settings().resolved_database_path


def create_app(
    database: Database | str | Path | None = None,
    *,
    settings: Settings | None = None,
    ai_recommender: AIRecommendationService | None = None,
) -> FastAPI:
    if isinstance(database, Database):
        db = database
    else:
        db = Database(database or default_database_path())
    queue = JobQueue(db)
    release_service: ReleaseService | None = None
    release_service_lock = threading.Lock()
    maintenance_journal: MaintenanceJournal | None = None
    recommendation_service = ai_recommender or AIRecommendationService(
        model=(settings.ai_model if settings is not None else "gpt-5.6-terra"),
        timeout_seconds=(
            settings.ai_timeout_seconds if settings is not None else 60.0
        ),
    )

    if settings is not None and all(
        path.is_dir()
        for path in (
            settings.data_root,
            settings.jobs_root,
            settings.completed_root,
            settings.release_kits_root,
        )
    ):
        maintenance_journal = MaintenanceJournal(db, settings)
        maintenance_journal.recover()

    def configured_maintenance_journal() -> MaintenanceJournal:
        nonlocal maintenance_journal
        if settings is None:
            raise ConfigurationError("maintenance service is not configured")
        if maintenance_journal is None:
            maintenance_journal = MaintenanceJournal(db, settings)
            maintenance_journal.recover()
        return maintenance_journal

    def configured_release_service() -> ReleaseService:
        nonlocal release_service
        if settings is None:
            raise ConfigurationError("release service is not configured")
        if release_service is None:
            # Construction performs durable crash recovery.  Serialize the
            # first request so two concurrent requests cannot recover the same
            # operation lease through two independent service instances.
            with release_service_lock:
                if release_service is None:
                    release_service = ReleaseService(
                        db,
                        settings,
                        maintenance_journal=configured_maintenance_journal(),
                    )
        return release_service

    application = FastAPI(
        title="BDEncode Backend",
        version=__version__,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )
    application.state.database = db
    application.state.queue = queue
    application.state.settings = settings
    application.state.maintenance_journal = maintenance_journal

    @application.middleware("http")
    async def require_same_origin_mutations(
        http_request: Request, call_next
    ) -> Response:
        if _is_api_state_change(http_request) and _same_origin_violation(http_request):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": "cross-origin state-changing requests are forbidden"
                },
            )
        return await call_next(http_request)

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
        if exc.code == RELEASE_PROFILE_VALIDATION_ERROR:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "release profile configuration is invalid",
                    "code": RELEASE_PROFILE_VALIDATION_ERROR,
                },
            )
        content: dict[str, object] = {"detail": str(exc)}
        if exc.code:
            content["code"] = exc.code
        if exc.context:
            content["context"] = exc.context
        return JSONResponse(status_code=422, content=content)

    @application.exception_handler(MaintenanceSafetyError)
    async def maintenance_safety_handler(
        _request: Request, exc: MaintenanceSafetyError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(ReleaseServiceError)
    async def release_service_handler(
        _request: Request, exc: ReleaseServiceError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(AIRecommendationUnavailable)
    async def ai_unavailable_handler(
        _request: Request, exc: AIRecommendationUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "code": "ai_recommendation_unavailable"},
        )

    @application.exception_handler(AIRecommendationError)
    async def ai_error_handler(
        _request: Request, exc: AIRecommendationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"detail": str(exc), "code": "ai_recommendation_failed"},
        )

    @application.get(f"{API_PREFIX}/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        active = db.encoding_job()
        preparing = db.preparing_job()
        return HealthResponse(
            status="ok",
            database=db.display_path,
            schema_version=db.schema_version(),
            active_job_id=active.id if active else None,
            blocking_state=active.state if active else None,
            preparing_job_id=preparing.id if preparing else None,
            ready_jobs=queue.ready_count(),
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
            backend_version=__version__,
            job_states=list(JobState),
            terminal_states=sorted(TERMINAL_STATES, key=lambda item: item.value),
            blocking_states=sorted(BLOCKING_STATES, key=lambda item: item.value),
            transitions=transitions,
            input_video_codecs=["AVC", "VC-1", "MPEG-2", "HEVC"],
            output_video_codecs=["x264", "x265"],
            disc_types=list(DiscType),
            content_types=list(ContentType),
            detail_levels=["beginner", "advanced", "pro"],
            audio_actions=list(AUDIO_ACTIONS),
            constraints={
                "max_active_jobs": 1,
                "max_concurrent_scans": 1,
                "queued_jobs_allowed": True,
                "preparation_during_encode": True,
                "ready_queue_requires_selection": True,
                "cancelled_job_restart": True,
                "failed_cancelled_job_purge": True,
                "durable_pause_resume": True,
                "control_request_acknowledgement": True,
                "storage_cleanup_preview": True,
                "release_preparation": True,
                "private_v1_torrent": True,
                "qbittorrent_paused_recheck": True,
                "tracker_publish_requires_dupe_receipt": True,
                "cpu_budget_fraction": 0.8,
                "supports_3d": False,
                "dolby_vision_retention": False,
                "hdr_modes": ["SDR", "HDR10"],
                "comparison_images": "lossless PNG",
                "ai_recommendation": True,
                "ai_recommendation_requires_confirmation": True,
                "audio_transcode_presets": audio_presets_payload(),
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

    @application.get(f"{API_PREFIX}/ai-recommendation/status")
    def ai_recommendation_status() -> dict[str, Any]:
        return recommendation_service.status()

    @application.post(
        f"{API_PREFIX}/jobs/{{job_id}}/ai-recommendation",
        response_model=AIRecommendationResponse,
    )
    def recommend_job_settings(
        job_id: str, request: AIRecommendationRequest
    ) -> AIRecommendationResponse:
        job = db.get_job(job_id)
        if job.state not in {JobState.AWAITING_SELECTION, JobState.NEEDS_REVIEW}:
            raise StateConflictError(
                "AI recommendation is available only while configuring a job",
                current=job.state,
            )
        scan_row = next(
            (
                item
                for item in db.list_scans(job_id=job_id, limit=500)
                if item.status in {ScanState.AWAITING_SELECTION, ScanState.COMPLETED}
            ),
            None,
        )
        if scan_row is None:
            raise StateConflictError(
                "job has no successful scan for AI recommendation", current=job.state
            )
        raw_scan = scan_row.result
        playlists = raw_scan.get("playlists")
        if not isinstance(playlists, list):
            raise ConfigurationError("stored scan result has no playlists")
        playlist = next(
            (
                item
                for item in playlists
                if isinstance(item, Mapping)
                and str(item.get("playlist_id")) == request.playlist_id
            ),
            None,
        )
        if playlist is None:
            raise ConfigurationError("selected playlist is not present in the scan")
        streams = playlist.get("streams")
        stream_rows = [item for item in streams or [] if isinstance(item, Mapping)]
        video_row = next(
            (item for item in stream_rows if item.get("kind") == "video"), None
        )
        video = (
            video_row.get("video")
            if isinstance(video_row, Mapping)
            and isinstance(video_row.get("video"), Mapping)
            else {}
        )
        encoder = (
            VideoEncoder.X265
            if str(raw_scan.get("disc_kind", "bd")).lower() == "uhd"
            else VideoEncoder.X264
        )
        content_type = str(
            raw_scan.get("content_kind") or job.content_type.value
        ).lower()
        gop_overrides: dict[str, Any] = {}
        frame_rate = video.get("frame_rate")
        if frame_rate:
            try:
                keyint, min_keyint = gop_for_frame_rate(str(frame_rate))
            except ValueError:
                pass
            else:
                gop_overrides = {"keyint": keyint, "min_keyint": min_keyint}
        base = recommended_profile(
            encoder,
            detail_level=request.detail_level,
            content_type=content_type,
            overrides=gop_overrides,
        )
        scan_facts = {
            "disc_kind": str(raw_scan.get("disc_kind", "unknown")),
            "content_kind": content_type,
            "playlist": {
                "duration_seconds": playlist.get("duration_seconds"),
                "chapter_count": len(playlist.get("chapters") or []),
                "segment_count": len(playlist.get("segments") or []),
                "angle_count": playlist.get("angle_count", 1),
                "seamless_branching": bool(playlist.get("seamless_branching")),
            },
            "video": {
                key: video.get(key)
                for key in (
                    "codec",
                    "width",
                    "height",
                    "frame_rate",
                    "field_order",
                    "bit_depth",
                    "pixel_format",
                    "color_primaries",
                    "color_transfer",
                    "color_matrix",
                    "color_range",
                    "hdr10",
                    "dolby_vision",
                    "hdr10_base_layer",
                    "three_d",
                )
            },
            "audio": [
                {
                    "codec": item.get("codec"),
                    "codec_profile": item.get("codec_profile"),
                    "channels": item.get("channels"),
                    "sample_rate": item.get("sample_rate"),
                    "bit_depth": item.get("bit_depth"),
                    "object_audio": bool(item.get("object_audio")),
                }
                for item in stream_rows
                if item.get("kind") == "audio"
            ],
            "subtitle_track_count": sum(
                1 for item in stream_rows if item.get("kind") == "subtitle"
            ),
            # Do not forward free-form scanner warnings: older scanner builds
            # may include a source path in their prose.  The count retains a
            # useful uncertainty signal without disclosing filesystem data.
            "scan_warning_count": len(raw_scan.get("warnings") or []),
        }
        return recommendation_service.recommend(
            RecommendationContext(
                encoder=encoder,
                detail_level=request.detail_level,
                content_type=content_type,
                scan_facts=scan_facts,
                base_settings=base,
                quality_priority=request.quality_priority,
                target_size_gib=request.target_size_gib,
                genre=request.genre,
                prompt=request.prompt,
            )
        )

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
            expected_state=request.expected_state,
            emit_event=request.emit_event,
        )

    @application.post(
        f"{API_PREFIX}/jobs/{{job_id}}/pause",
        response_model=Job,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def pause_job(job_id: str, request: JobControlActionRequest | None = None) -> Job:
        action = request or JobControlActionRequest()
        return queue.request_pause(
            job_id,
            message=action.message or "pause requested by operator",
            expected_control_revision=action.expected_control_revision,
        )

    @application.post(
        f"{API_PREFIX}/jobs/{{job_id}}/continue",
        response_model=Job,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def continue_job(
        job_id: str, request: JobControlActionRequest | None = None
    ) -> Job:
        action = request or JobControlActionRequest()
        return queue.resume(
            job_id,
            message=action.message or "continued by operator",
            expected_control_revision=action.expected_control_revision,
        )

    @application.post(
        f"{API_PREFIX}/jobs/{{job_id}}/cancel",
        response_model=Job,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def request_job_cancellation(
        job_id: str, request: JobControlActionRequest | None = None
    ) -> Job:
        action = request or JobControlActionRequest()
        return queue.request_cancel(
            job_id,
            message=action.message or "cancellation requested by operator",
            expected_control_revision=action.expected_control_revision,
        )

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/resume", response_model=Job)
    def resume_job(job_id: str) -> Job:
        return queue.resume_review(job_id)

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/retry-upload", response_model=Job)
    def retry_upload(job_id: str) -> Job:
        return queue.retry_upload(job_id)

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/retry", response_model=Job)
    def retry_failed_job(job_id: str, request: JobRetryRequest | None = None) -> Job:
        retry = request or JobRetryRequest()
        return queue.retry_failed(
            job_id,
            message=retry.message,
            expected_version=retry.expected_version,
        )

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/restart", response_model=Job)
    def restart_cancelled_job(
        job_id: str, request: JobRetryRequest | None = None
    ) -> Job:
        restart = request or JobRetryRequest()
        return queue.restart_cancelled(
            job_id,
            message=restart.message,
            expected_version=restart.expected_version,
        )

    def configured_settings() -> Settings:
        if settings is None:
            raise ConfigurationError(
                "this operation requires configured workspace roots"
            )
        return settings

    def completed_release_for(job_id: str) -> Path | None:
        runtime = configured_settings()
        outputs = [
            item
            for item in db.list_artifacts(job_id=job_id, limit=1000)
            if item.kind is ArtifactKind.OUTPUT
        ]
        if len(outputs) != 1:
            return None
        output = Path(outputs[0].path)
        if not os.path.lexists(output.parent):
            return None
        resolved = output.parent.resolve(strict=True)
        completed_root = runtime.completed_root.resolve(strict=True)
        if resolved.parent != completed_root:
            raise MaintenanceSafetyError(
                "completed release escaped the configured root"
            )
        return resolved

    @application.get(f"{API_PREFIX}/jobs/{{job_id}}/storage")
    def job_storage(job_id: str) -> dict[str, object]:
        runtime = configured_settings()
        job = db.get_job(job_id)
        completed = completed_release_for(job.id)
        report = inspect_job_storage(
            runtime.job_root(job.id),
            jobs_root=runtime.jobs_root,
            completed_release=completed,
            completed_root=runtime.completed_root if completed is not None else None,
        )
        work = next(item for item in report.categories if item.name == "work")
        return {
            **report.to_dict(),
            "workspace_status": "AVAILABLE" if work.present else "CLEANED",
            "cleanup_allowed": job.state is JobState.COMPLETED and work.present,
            "release_present": completed is not None,
        }

    @application.post(f"{API_PREFIX}/jobs/{{job_id}}/cleanup")
    def cleanup_job(job_id: str, request: CleanupRequest) -> dict[str, object]:
        runtime = configured_settings()
        job = db.get_job(job_id)
        if job.state is not JobState.COMPLETED:
            raise StateConflictError(
                "temporary cleanup is currently limited to completed jobs",
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
        journal = configured_maintenance_journal()
        work_path = runtime.job_root(job.id) / "work"
        target_specs = (
            [
                MaintenanceTargetSpec(
                    work_path,
                    runtime.job_root(job.id),
                    "temporary work directory",
                )
            ]
            if os.path.lexists(work_path)
            else []
        )
        operation = (
            journal.begin(
                "completed-workspace-cleanup",
                job.id,
                target_specs,
                guard=MaintenanceDomainGuard(
                    job_id=job.id,
                    expected_job_version=job.version,
                    allowed_job_states=(JobState.COMPLETED.value,),
                ),
            )
            if target_specs
            else None
        )
        event_payload: dict[str, object] = {
            "scope": request.scope,
            "bytes_removed": sum(
                int(target["bytes_moved"])
                for target in (operation.targets if operation is not None else ())
            ),
        }

        try:
            if operation is not None:
                journal.stage(operation.id)
            db.record_completed_cleanup(
                job.id,
                expected_version=request.expected_version,
                cleanup=None,
                payload=event_payload,
                maintenance_operation_id=(
                    operation.id if operation is not None else None
                ),
            )
        except BaseException:
            if operation is not None:
                journal.rollback(operation.id)
            raise
        removed = int(event_payload["bytes_removed"])
        if operation is not None:
            try:
                journal.finalize(operation.id)
            except (MaintenanceSafetyError, OSError):
                # The committed journal remains discoverable by startup recovery.
                pass
        completed = completed_release_for(job.id)
        report = inspect_job_storage(
            runtime.job_root(job.id),
            jobs_root=runtime.jobs_root,
            completed_release=completed,
            completed_root=runtime.completed_root if completed is not None else None,
        )
        return {
            **report.to_dict(),
            "workspace_status": "CLEANED",
            "bytes_removed": removed,
            "release_present": completed is not None,
        }

    @application.delete(
        f"{API_PREFIX}/jobs/{{job_id}}/purge",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def purge_terminal_job(
        job_id: str,
        expected_version: Annotated[int | None, Query(ge=1)] = None,
        preserve_release: bool = True,
    ) -> Response:
        runtime = configured_settings()
        if not preserve_release:
            raise ConfigurationError(
                "job deletion never removes a completed release; use the separate "
                "release deletion action"
            )
        job = db.get_job(job_id)
        if job.state not in {
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.COMPLETED,
        }:
            raise StateConflictError(
                "only terminal jobs can be deleted", current=job.state
            )
        if expected_version is not None and job.version != expected_version:
            raise StateConflictError(
                f"job version is {job.version}, expected {expected_version}",
                current=job.state,
            )
        service = configured_release_service()
        preparations = service.store.list_for_job(job.id)
        for preparation in preparations:
            if (
                preparation.state
                in {
                    ReleasePreparationState.UNKNOWN,
                    ReleasePreparationState.PUBLISHED,
                }
                or (
                    preparation.qbittorrent_receipt is not None
                    and preparation.qbittorrent_receipt.get("outcome") != "REJECTED"
                )
                or (
                    preparation.publication_receipt is not None
                    and preparation.publication_receipt.get("outcome") != "REJECTED"
                )
            ):
                raise StateConflictError(
                    "release preparation has an external outcome; preserve its audit "
                    "or explicitly delete the completed release first",
                    current=job.state,
                )
        preparation_versions = {
            preparation.id: preparation.version for preparation in preparations
        }
        targets: list[MaintenanceTargetSpec] = []
        for preparation in preparations:
            kit = service._verified_maintenance_kit(preparation)
            if kit is not None:
                targets.append(
                    MaintenanceTargetSpec(
                        kit,
                        runtime.release_kits_root,
                        "private release kit",
                    )
                )
        workspace = runtime.job_root(job.id)
        if os.path.lexists(workspace):
            targets.append(
                MaintenanceTargetSpec(
                    workspace,
                    runtime.jobs_root,
                    "job workspace",
                )
            )
        journal = configured_maintenance_journal()
        operation = (
            journal.begin(
                "terminal-job-purge",
                job.id,
                targets,
                guard=MaintenanceDomainGuard(
                    job_id=job.id,
                    expected_job_version=job.version,
                    allowed_job_states=(
                        JobState.FAILED.value,
                        JobState.CANCELLED.value,
                        JobState.COMPLETED.value,
                    ),
                    expected_preparation_versions=preparation_versions,
                    forbid_active_preparations=True,
                ),
            )
            if targets
            else None
        )

        try:
            if operation is not None:
                journal.stage(operation.id)
            db.delete_terminal_job(
                job.id,
                expected_version=expected_version,
                allow_completed=True,
                expected_release_versions=preparation_versions,
                maintenance_operation_id=(
                    operation.id if operation is not None else None
                ),
            )
        except BaseException:
            if operation is not None:
                journal.rollback(operation.id)
            raise
        if operation is not None:
            try:
                journal.finalize(operation.id)
            except (MaintenanceSafetyError, OSError):
                # The target is detached and the durable committed journal lets a
                # startup sweep safely retry removal without touching the release.
                pass
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.delete(
        f"{API_PREFIX}/jobs/{{job_id}}/release",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_completed_release(
        job_id: str, request: CompletedReleaseDeleteRequest
    ) -> Response:
        runtime = configured_settings()
        service = configured_release_service()
        job = db.get_job(job_id)
        if job.state is not JobState.COMPLETED:
            raise StateConflictError(
                "only completed jobs can delete their public release",
                current=job.state,
            )
        payload, _size, digest = service._payload_artifact(job.id)
        if request.confirmation != payload.stem:
            raise StateConflictError("release-name confirmation does not match")
        if request.expected_sha256 != digest:
            raise StateConflictError("release hash confirmation does not match")
        preparations = service.store.list_for_job(job.id)
        current_versions = {item.id: item.version for item in preparations}
        if request.preparation_versions != current_versions:
            raise StateConflictError(
                "release preparation snapshot changed after confirmation"
            )
        if not request.force_if_seeded and any(
            item.state
            in {
                ReleasePreparationState.UNKNOWN,
                ReleasePreparationState.PUBLISHED,
            }
            or (
                item.qbittorrent_receipt is not None
                and item.qbittorrent_receipt.get("outcome") != "REJECTED"
            )
            or (
                item.publication_receipt is not None
                and item.publication_receipt.get("outcome") != "REJECTED"
            )
            for item in preparations
        ):
            raise StateConflictError(
                "release has a remote or uncertain outcome; explicit force is required"
            )
        targets: list[MaintenanceTargetSpec] = []
        for preparation in preparations:
            kit = service._verified_maintenance_kit(preparation)
            if kit is not None:
                targets.append(
                    MaintenanceTargetSpec(
                        kit,
                        runtime.release_kits_root,
                        "private release kit",
                    )
                )
        targets.append(
            MaintenanceTargetSpec(
                payload.parent,
                runtime.completed_root,
                "completed release",
            )
        )
        journal = configured_maintenance_journal()
        operation = journal.begin(
            "completed-release-delete",
            job.id,
            targets,
            guard=MaintenanceDomainGuard(
                job_id=job.id,
                expected_job_version=job.version,
                allowed_job_states=(JobState.COMPLETED.value,),
                expected_preparation_versions=request.preparation_versions,
                forbid_active_preparations=True,
            ),
        )
        release_path = os.path.normcase(os.path.abspath(payload.parent))
        release_target = next(
            target
            for target in operation.targets
            if os.path.normcase(os.path.abspath(target["original_path"]))
            == release_path
        )

        event_payload: dict[str, object] = {
            "output_name": payload.stem,
            "sha256": digest,
            "bytes_removed": int(release_target["bytes_moved"]),
        }

        try:
            journal.stage(operation.id)
            service.store.delete_completed_release(
                job.id,
                expected_versions=request.preparation_versions,
                maintenance_operation_id=operation.id,
                payload=event_payload,
            )
        except BaseException:
            journal.rollback(operation.id)
            raise
        try:
            journal.finalize(operation.id)
        except (MaintenanceSafetyError, OSError):
            # Domain deletion and the COMMITTED journal phase are already
            # durable. Startup recovery can retry fail-closed finalization.
            pass
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.delete(f"{API_PREFIX}/jobs/{{job_id}}", response_model=Job)
    def cancel_job(job_id: str) -> Job:
        # Compatibility alias from API v1.  New clients use POST /cancel;
        # permanent deletion is the explicit /purge operation.
        return queue.cancel(job_id)

    @application.get(f"{API_PREFIX}/release-profiles")
    def list_release_profiles() -> dict[str, object]:
        service = configured_release_service()
        items = service.profiles()
        return {"items": items, "count": len(items)}

    @application.post(
        f"{API_PREFIX}/jobs/{{job_id}}/release-preparations",
        response_model=ReleasePreparationView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_release_preparation(
        job_id: str, request: ReleasePreparationCreateRequest
    ) -> ReleasePreparationView:
        service = configured_release_service()
        return service.create(
            job_id,
            profile_id=request.profile_id,
            metadata=request.metadata,
        )

    @application.get(
        f"{API_PREFIX}/jobs/{{job_id}}/release-preparations",
        response_model=list[ReleasePreparationView],
    )
    def list_release_preparations(job_id: str) -> list[ReleasePreparationView]:
        service = configured_release_service()
        return list(service.list_for_job(job_id))

    @application.get(
        f"{API_PREFIX}/release-preparations/{{preparation_id}}",
        response_model=ReleasePreparationView,
    )
    def get_release_preparation(preparation_id: str) -> ReleasePreparationView:
        service = configured_release_service()
        return service.get(preparation_id)

    @application.post(f"{API_PREFIX}/release-preparations/{{preparation_id}}/validate")
    def validate_release_preparation(
        preparation_id: str, request: ReleaseActionRequest
    ) -> dict[str, object]:
        service = configured_release_service()
        record = service.store.get(preparation_id)
        if record.version != request.expected_version:
            raise StateConflictError(
                f"release preparation version is {record.version}, "
                f"expected {request.expected_version}"
            )
        return service.validate(preparation_id)

    @application.post(
        f"{API_PREFIX}/release-preparations/{{preparation_id}}/build",
        response_model=ReleasePreparationView,
    )
    def build_release_preparation(
        preparation_id: str, request: ReleaseActionRequest
    ) -> ReleasePreparationView:
        service = configured_release_service()
        return service.build(preparation_id, expected_version=request.expected_version)

    @application.post(
        f"{API_PREFIX}/release-preparations/{{preparation_id}}/export",
        response_class=Response,
    )
    def export_release_torrent(
        preparation_id: str, request: ReleaseActionRequest
    ) -> Response:
        service = configured_release_service()
        record = service.store.get(preparation_id)
        if record.version != request.expected_version:
            raise StateConflictError(
                f"release preparation version is {record.version}, "
                f"expected {request.expected_version}"
            )
        torrent, filename = service.torrent_bytes(preparation_id)
        return Response(
            content=torrent,
            media_type="application/x-bittorrent",
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(filename, safe="")
                ),
            },
        )

    @application.post(
        f"{API_PREFIX}/release-preparations/{{preparation_id}}/dupe-check",
        response_model=ReleasePreparationView,
    )
    def dupe_check_release(
        preparation_id: str, request: ReleaseActionRequest
    ) -> ReleasePreparationView:
        service = configured_release_service()
        return service.dupe_check(
            preparation_id, expected_version=request.expected_version
        )

    @application.post(
        f"{API_PREFIX}/release-preparations/{{preparation_id}}/seed",
        response_model=ReleasePreparationView,
    )
    def seed_release(
        preparation_id: str, request: ReleaseActionRequest
    ) -> ReleasePreparationView:
        service = configured_release_service()
        return service.seed(preparation_id, expected_version=request.expected_version)

    def require_publication_confirmation(http_request: Request, digest: str) -> None:
        if http_request.headers.get("x-bdencode-manifest") != digest:
            raise StateConflictError(
                "publication confirmation header does not match the manifest"
            )

    def trusted_operator(http_request: Request) -> str:
        """Return the reverse-proxy authenticated operator identity.

        Nginx overwrites ``X-Remote-User`` with ``$remote_user``.  Direct
        loopback automation must set the same header explicitly; the client
        request body can never choose the audit identity.
        """

        values = http_request.headers.getlist("x-remote-user")
        if len(values) != 1 or not _TRUSTED_OPERATOR_RE.fullmatch(values[0]):
            raise StateConflictError(
                "tracker publication requires an authenticated operator identity"
            )
        return values[0]

    @application.post(
        f"{API_PREFIX}/release-preparations/{{preparation_id}}/upload",
        response_model=ReleasePreparationView,
    )
    def publish_release(
        preparation_id: str,
        request: ReleasePublishRequest,
        http_request: Request,
    ) -> ReleasePreparationView:
        service = configured_release_service()
        require_publication_confirmation(http_request, request.manifest_sha256)
        return service.publish(
            preparation_id,
            expected_version=request.expected_version,
            manifest_sha256=request.manifest_sha256,
            approved_by=trusted_operator(http_request),
        )

    @application.delete(
        f"{API_PREFIX}/release-preparations/{{preparation_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    def delete_release_preparation(
        preparation_id: str,
        expected_version: Annotated[int, Query(ge=1)],
    ) -> Response:
        service = configured_release_service()
        service.delete(preparation_id, expected_version=expected_version)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
