"""Dynamic HDR (HDR10+ and Dolby Vision profile 8.1) retention policy.

The default remains the long-standing release rule: static HDR10 only, every
dynamic layer discarded and any dynamic side data in the output a hard failure.
Retention is opt-in per job.  This module only decides *whether* a source can
be retained safely, builds the external-tool commands, parses their output and
produces the x265 parameters; the worker runs the tools and checkpoints.

Everything here fails closed.  A retained mode is refused unless the encoder,
picture timeline and source metadata make a frame-exact transfer possible, and
the worker independently verifies the finished MKV before it may continue.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

HDR10PLUS_TOOL = "hdr10plus_tool"
DOVI_TOOL = "dovi_tool"

# Dolby Vision profile 8.1 output limits (level 5.1, high tier) that x265 does
# not enforce on its own; applied when the operator did not choose a VBV.
DOLBY_VISION_VBV_KBPS = 160_000

_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./+\-]+$")
_FRAMES_RE = re.compile(r"(?im)^\s*Frames:\s*(\d+)\s*$")
_PROFILE_RE = re.compile(r"(?im)^\s*Profile:\s*(\d+)")


class DynamicHdrError(ValueError):
    """Retention was requested but cannot be performed safely.

    ``code`` is stable for manifests and API clients.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DynamicHdrMode(StrEnum):
    DISCARD = "discard"
    AUTO = "auto"
    HDR10PLUS = "hdr10plus"
    DOLBY_VISION = "dolby_vision"


def parse_mode(raw: object) -> DynamicHdrMode:
    if raw is None:
        return DynamicHdrMode.DISCARD
    if type(raw) is not str:
        raise DynamicHdrError("invalid_mode", "video.dynamic_hdr must be a string")
    try:
        return DynamicHdrMode(raw)
    except ValueError as exc:
        raise DynamicHdrError(
            "invalid_mode",
            "video.dynamic_hdr must be one of "
            + ", ".join(item.value for item in DynamicHdrMode),
        ) from exc


@dataclass(frozen=True, slots=True)
class DynamicHdrPlan:
    """The resolved decision for one job."""

    requested: DynamicHdrMode
    mode: DynamicHdrMode
    reason: str
    source_profile: int | None = None
    # ``dovi_tool -m``: 2 converts a profile 7 RPU to profile 8.1.
    convert_mode: int | None = None
    # Zero the RPU active-area offsets when the encode removes the letterbox.
    crop_active_area: bool = False
    # Profile 7 discs keep the RPUs in a secondary video stream: its ordinal among the video streams of
    # the reference (None: the base layer, ordinal 0).
    el_video_ordinal: int | None = None

    @property
    def retained(self) -> bool:
        return self.mode in {DynamicHdrMode.HDR10PLUS, DynamicHdrMode.DOLBY_VISION}

    @property
    def required_tool(self) -> str | None:
        return {
            DynamicHdrMode.HDR10PLUS: HDR10PLUS_TOOL,
            DynamicHdrMode.DOLBY_VISION: DOVI_TOOL,
        }.get(self.mode)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested.value,
            "mode": self.mode.value,
            "reason": self.reason,
            "source_profile": self.source_profile,
            "convert_mode": self.convert_mode,
            "crop_active_area": self.crop_active_area,
            "el_video_ordinal": self.el_video_ordinal,
        }

    @classmethod
    def from_dict(cls, raw: object) -> DynamicHdrPlan:
        if not isinstance(raw, Mapping):
            raise DynamicHdrError("invalid_report", "dynamic HDR plan is not an object")
        try:
            profile = raw.get("source_profile")
            convert = raw.get("convert_mode")
            crop = raw.get("crop_active_area", False)
            ordinal = raw.get("el_video_ordinal")
            if (
                profile is not None
                and type(profile) is not int
                or convert is not None
                and type(convert) is not int
                or ordinal is not None
                and type(ordinal) is not int
                or type(crop) is not bool
            ):
                raise ValueError("wrong field type")
            return cls(
                requested=DynamicHdrMode(raw["requested"]),
                mode=DynamicHdrMode(raw["mode"]),
                reason=str(raw["reason"]),
                source_profile=profile,
                convert_mode=convert,
                crop_active_area=crop,
                el_video_ordinal=ordinal,
            )
        except (KeyError, ValueError) as exc:
            raise DynamicHdrError(
                "invalid_report", f"dynamic HDR plan is invalid: {exc}"
            ) from exc


DISCARD_PLAN = DynamicHdrPlan(
    DynamicHdrMode.DISCARD, DynamicHdrMode.DISCARD, "dynamic HDR is discarded by policy"
)


def resolve_dynamic_hdr(
    requested: DynamicHdrMode,
    *,
    encoder: str,
    hdr10_enabled: bool,
    progressive: bool,
    crop_enabled: bool,
    dolby_vision: bool,
    dolby_vision_profile: int | None,
    hdr10_base_layer: bool,
    hdr10_plus: bool,
    dolby_vision_el_ordinal: int | None = None,
    dolby_vision_el_type: str | None = None,
) -> DynamicHdrPlan:
    """Decide what to retain, or refuse with a coded :class:`DynamicHdrError`.

    ``AUTO`` never raises: it retains what it safely can (HDR10+ first, then
    Dolby Vision) and otherwise falls back to the discard policy.
    """

    if requested is DynamicHdrMode.DISCARD:
        return DISCARD_PLAN

    def refuse(code: str, message: str) -> DynamicHdrPlan:
        if requested is DynamicHdrMode.AUTO:
            return DynamicHdrPlan(requested, DynamicHdrMode.DISCARD, message)
        raise DynamicHdrError(code, message)

    if encoder != "x265" or not hdr10_enabled:
        return refuse(
            "unsupported_output",
            "dynamic HDR retention needs an x265 HDR10 (Main 10) output",
        )
    if not progressive:
        return refuse(
            "temporal_filter",
            "dynamic HDR metadata is per source frame; it cannot follow IVTC or "
            "deinterlacing, so retention needs a progressive timeline",
        )

    def hdr10plus_plan() -> DynamicHdrPlan | None:
        if hdr10_plus:
            return DynamicHdrPlan(
                requested,
                DynamicHdrMode.HDR10PLUS,
                "source carries HDR10+ dynamic metadata",
            )
        return None

    def dolby_plan() -> DynamicHdrPlan | None:
        if not dolby_vision:
            return None
        if not hdr10_base_layer:
            raise DynamicHdrError(
                "no_hdr10_base",
                "the Dolby Vision source has no confirmed HDR10 base layer, so a "
                "profile 8.1 output is not possible",
            )
        if dolby_vision_profile not in {7, 8}:
            raise DynamicHdrError(
                "unsupported_profile",
                f"Dolby Vision profile {dolby_vision_profile} cannot be converted "
                "to profile 8.1 (only profiles 7 and 8 can)",
            )
        reason = "source carries a Dolby Vision RPU convertible to profile 8.1"
        if dolby_vision_profile == 7 and dolby_vision_el_type == "FEL":
            reason += "; the full enhancement layer (FEL) is not part of profile 8.1 and is dropped"
        return DynamicHdrPlan(
            requested,
            DynamicHdrMode.DOLBY_VISION,
            reason,
            source_profile=dolby_vision_profile,
            convert_mode=2 if dolby_vision_profile == 7 else None,
            crop_active_area=crop_enabled,
            el_video_ordinal=dolby_vision_el_ordinal if dolby_vision_profile == 7 else None,
        )

    if requested is DynamicHdrMode.HDR10PLUS:
        return hdr10plus_plan() or refuse(
            "source_missing", "the source carries no HDR10+ dynamic metadata"
        )
    if requested is DynamicHdrMode.DOLBY_VISION:
        return dolby_plan() or refuse(
            "source_missing", "the source carries no Dolby Vision metadata"
        )
    # AUTO
    chosen = hdr10plus_plan()
    if chosen is None:
        try:
            chosen = dolby_plan()
        except DynamicHdrError as exc:
            return DynamicHdrPlan(requested, DynamicHdrMode.DISCARD, str(exc))
    return chosen or DynamicHdrPlan(
        requested, DynamicHdrMode.DISCARD, "the source carries no dynamic HDR metadata"
    )


# -- external tool commands ----------------------------------------------------


def _base_layer_command(reference: Path, *, ffmpeg: str, video_ordinal: int = 0) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(reference),
        "-map",
        f"0:v:{video_ordinal}",
        "-c",
        "copy",
        "-bsf:v",
        "hevc_mp4toannexb",
        "-f",
        "hevc",
        "-",
    ]


def hdr10plus_extract_commands(
    reference: Path,
    metadata_json: Path,
    *,
    ffmpeg: str = "ffmpeg",
    tool: str = HDR10PLUS_TOOL,
) -> list[list[str]]:
    """Pipe the untouched HEVC stream into ``hdr10plus_tool extract``."""

    return [
        _base_layer_command(reference, ffmpeg=ffmpeg),
        [tool, "extract", "-o", str(metadata_json), "-"],
    ]


def dovi_extract_commands(
    reference: Path,
    rpu: Path,
    plan: DynamicHdrPlan,
    *,
    ffmpeg: str = "ffmpeg",
    tool: str = DOVI_TOOL,
) -> list[list[str]]:
    """Pipe the HEVC stream into ``dovi_tool extract-rpu`` (converting to 8.1)."""

    if plan.mode is not DynamicHdrMode.DOLBY_VISION:
        raise DynamicHdrError("wrong_plan", "the plan does not retain Dolby Vision")
    command = [tool]
    if plan.convert_mode is not None:
        command.extend(("-m", str(plan.convert_mode)))
    if plan.crop_active_area:
        command.append("-c")
    command.extend(("extract-rpu", "-o", str(rpu), "-"))
    # On a profile 7 disc the RPUs are in the secondary (enhancement layer) video stream.
    return [
        _base_layer_command(reference, ffmpeg=ffmpeg, video_ordinal=plan.el_video_ordinal or 0),
        command,
    ]


def dovi_summary_command(rpu: Path, *, tool: str = DOVI_TOOL) -> list[str]:
    return [tool, "info", "-i", str(rpu), "--summary"]


# The encoder runs through FFmpeg's libx265. For Dolby Vision it cannot read an RPU file
# (``--dolby-vision-rpu`` belongs to the x265 command-line program, not the library), and Debian's
# libx265 is built without HDR10+ (``--dhdr10-info``), so the encoded stream carries neither. Retention
# therefore injects the verified metadata into the finished HEVC stream (dovi_tool and hdr10plus_tool
# match it to frames by display order) and rebuilds the Matroska track from the elementary stream with
# the original timestamps; for Dolby Vision mkvmerge then also writes the configuration record that the
# output validation looks for.


def dovi_base_stream_command(video: Path, hevc: Path, *, ffmpeg: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(video),
        "-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(hevc),
    ]


def dovi_inject_command(hevc: Path, rpu: Path, injected: Path, *, tool: str = DOVI_TOOL) -> list[str]:
    return [tool, "inject-rpu", "-i", str(hevc), "--rpu-in", str(rpu), "-o", str(injected)]


def hdr10plus_inject_command(
    hevc: Path, metadata_json: Path, injected: Path, *, tool: str = HDR10PLUS_TOOL
) -> list[str]:
    """Interleave the HDR10+ SEI messages into the finished HEVC stream (display order)."""

    return [tool, "inject", "-i", str(hevc), "-j", str(metadata_json), "-o", str(injected)]


def video_timestamps_command(video: Path, timestamps: Path, *, mkvextract: str = "mkvextract") -> list[str]:
    return [mkvextract, str(video), "timestamps_v2", f"0:{timestamps}"]


# Matroska track properties that the rebuilt track must keep (mkvmerge name -> its option).
_TRACK_COLOUR_OPTIONS = {
    "color_range": "--colour-range",
    "color_matrix_coefficients": "--colour-matrix-coefficients",
    "color_transfer_characteristics": "--colour-transfer-characteristics",
    "color_primaries": "--colour-primaries",
}


def video_track_command(video: Path, *, mkvmerge: str = "mkvmerge") -> list[str]:
    return [mkvmerge, "--identify", "--identification-format", "json", str(video)]


def parse_video_track_properties(text: str) -> dict[str, int]:
    """Frame duration and colour description of the first video track of an MKV."""

    try:
        tracks = json.loads(text)["tracks"]
        properties = next(track["properties"] for track in tracks if track.get("type") == "video")
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise DynamicHdrError("invalid_track", "the encoded video track cannot be identified") from exc
    wanted = ("default_duration", *_TRACK_COLOUR_OPTIONS)
    found = {key: properties[key] for key in wanted if type(properties.get(key)) is int}
    if "default_duration" not in found or found["default_duration"] < 1:
        raise DynamicHdrError("invalid_track", "the encoded video track has no frame duration")
    return found


def dovi_rebuild_command(
    injected: Path,
    timestamps: Path,
    output: Path,
    properties: Mapping[str, int] | None = None,
    *,
    mkvmerge: str = "mkvmerge",
) -> list[str]:
    command = [mkvmerge, "--quiet", "--output", str(output), "--timestamps", f"0:{timestamps}"]
    for key, option in _TRACK_COLOUR_OPTIONS.items():
        if properties and key in properties:
            command += [option, f"0:{properties[key]}"]
    return [*command, str(injected)]


def dovi_duration_command(output: Path, default_duration_ns: int, *, mkvpropedit: str = "mkvpropedit") -> list[str]:
    """mkvmerge derives the frame duration from the millisecond timestamps (42 ms for 24000/1001
    video), which makes FFmpeg reject the decode; restore the encode's own value."""

    return [mkvpropedit, str(output), "--edit", "track:v1", "--set", f"default-duration={default_duration_ns}"]


def dovi_verify_commands(
    video: Path, rpu: Path, *, ffmpeg: str = "ffmpeg", tool: str = DOVI_TOOL
) -> list[list[str]]:
    """Read the RPUs back out of the rebuilt stream, without any conversion."""

    return [
        _base_layer_command(video, ffmpeg=ffmpeg),
        [tool, "extract-rpu", "-o", str(rpu), "-"],
    ]


# -- tool output ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hdr10PlusSummary:
    frames: int
    scenes: int

    def to_dict(self) -> dict[str, int]:
        return {"frames": self.frames, "scenes": self.scenes}


def parse_hdr10plus_json(text: str) -> Hdr10PlusSummary:
    """Validate an ``hdr10plus_tool`` metadata document and count its frames."""

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DynamicHdrError(
            "invalid_metadata", f"HDR10+ metadata is not valid JSON: {exc}"
        ) from exc
    scene_info = document.get("SceneInfo") if isinstance(document, Mapping) else None
    if not isinstance(scene_info, list) or not scene_info:
        raise DynamicHdrError(
            "no_metadata", "the HDR10+ metadata contains no per-frame entries"
        )
    for index, entry in enumerate(scene_info):
        if not isinstance(entry, Mapping) or "LuminanceParameters" not in entry:
            raise DynamicHdrError(
                "invalid_metadata",
                f"HDR10+ frame entry {index} has no luminance parameters",
            )
    summary = document.get("SceneInfoSummary")
    firsts = summary.get("SceneFirstFrameIndex") if isinstance(summary, Mapping) else None
    scenes = len(firsts) if isinstance(firsts, list) and firsts else 1
    return Hdr10PlusSummary(frames=len(scene_info), scenes=scenes)


@dataclass(frozen=True, slots=True)
class DoviSummary:
    frames: int
    profile: int

    def to_dict(self) -> dict[str, int]:
        return {"frames": self.frames, "profile": self.profile}


def parse_dovi_summary(text: str) -> DoviSummary:
    frames = _FRAMES_RE.search(text)
    profile = _PROFILE_RE.search(text)
    if frames is None or profile is None:
        raise DynamicHdrError(
            "invalid_metadata", "dovi_tool did not report a frame count and profile"
        )
    count = int(frames.group(1))
    if count < 1:
        raise DynamicHdrError("no_metadata", "the Dolby Vision RPU has no frames")
    return DoviSummary(frames=count, profile=int(profile.group(1)))


def require_frame_alignment(
    metadata_frames: int, output_frames: int, *, what: str
) -> None:
    """The metadata must describe exactly the frames that are being encoded."""

    if metadata_frames != output_frames:
        raise DynamicHdrError(
            "frame_mismatch",
            f"{what} describes {metadata_frames} frames but the encoded timeline "
            f"has {output_frames}; it cannot be attached frame-exactly",
        )


def require_dolby_vision_profile(summary: DoviSummary) -> None:
    if summary.profile != 8:
        raise DynamicHdrError(
            "unexpected_profile",
            f"the extracted RPU is profile {summary.profile}, not the required 8.1",
        )


# -- x265 integration ------------------------------------------------------------


def checked_path(path: Path) -> str:
    """Return ``path`` as text safe to embed in an ``-x265-params`` value."""

    text = path.as_posix()
    if not _SAFE_PATH_RE.fullmatch(text):
        raise DynamicHdrError(
            "unsafe_path",
            "the metadata path contains characters that cannot be passed to x265",
        )
    return text


def x265_dynamic_params(plan: DynamicHdrPlan, metadata: Path) -> dict[str, str | int]:
    """x265 private parameters that accompany the retained metadata.

    Dolby Vision needs the profile signalling in the stream; the RPU itself is injected afterwards.
    HDR10+ needs none: its SEI messages are injected afterwards too (a libx265 without HDR10+ support
    would only warn about ``dhdr10-info`` and drop it, and one with support would double the SEI).
    """

    if plan.mode is DynamicHdrMode.DOLBY_VISION:
        return {
            "dolby-vision-profile": "8.1",
            "dolby-vision-rpu": checked_path(metadata),
            "aud": 1,
            "repeat-headers": 1,
            "hrd": 1,
        }
    return {}


# -- x265 build capability -----------------------------------------------------------


def x265_support_from_help(text: str) -> dict[str, bool]:
    """Read dynamic-HDR support from ``x265 --help`` output.

    FFmpeg's libx265 wrapper only *warns* about an unknown parameter and then
    encodes without it, so support must be established before the encode.
    """

    return {
        "hdr10plus": "--dhdr10-info" in text,
        "dolby_vision": "--dolby-vision-rpu" in text,
    }


# -- verification of the finished stream --------------------------------------------


def expected_side_data(plan: DynamicHdrPlan) -> tuple[str, ...]:
    """Lower-cased ffprobe side-data markers that must be present."""

    if plan.mode is DynamicHdrMode.HDR10PLUS:
        return ("smpte2094-40",)
    if plan.mode is DynamicHdrMode.DOLBY_VISION:
        return ("dovi configuration record",)
    return ()


def allowed_forbidden_tokens(plan: DynamicHdrPlan) -> frozenset[str]:
    """Tokens of the discard policy that this plan legitimately allows."""

    if plan.mode is DynamicHdrMode.HDR10PLUS:
        return frozenset({"hdr dynamic", "hdr10+"})
    if plan.mode is DynamicHdrMode.DOLBY_VISION:
        return frozenset({"dolby vision", "dovi"})
    return frozenset()


def validate_retained_side_data(
    side_data_types: list[str],
    side_data: list[Mapping[str, Any]],
    plan: DynamicHdrPlan,
) -> tuple[str, ...]:
    """Prove that the output carries the retained layer, correctly labelled."""

    errors: list[str] = []
    lowered = "\n".join(side_data_types).casefold()
    for marker in expected_side_data(plan):
        if marker not in lowered:
            errors.append(
                f"retained {plan.mode.value} metadata is missing from the output "
                f"(no '{marker}' side data)"
            )
    if plan.mode is DynamicHdrMode.DOLBY_VISION and not errors:
        records = [
            item
            for item in side_data
            if str(item.get("side_data_type", "")).casefold()
            == "dovi configuration record"
        ]
        for record in records:
            if record.get("dv_profile") != 8:
                errors.append(
                    f"Dolby Vision output is profile {record.get('dv_profile')}, "
                    "not 8"
                )
            if record.get("dv_bl_signal_compatibility_id") != 1:
                errors.append(
                    "Dolby Vision output is not HDR10-compatible "
                    "(bl signal compatibility id is not 1)"
                )
            if record.get("rpu_present_flag") not in {1, True}:
                errors.append("Dolby Vision configuration reports no RPU")
    return tuple(dict.fromkeys(errors))
