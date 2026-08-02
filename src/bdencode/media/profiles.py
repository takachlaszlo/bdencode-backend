"""Validated x264/x265 profiles and frontend-friendly field schemas."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
import math
import re
from typing import Any, Mapping


class VideoEncoder(StrEnum):
    X264 = "x264"
    X265 = "x265"


class DetailLevel(StrEnum):
    BEGINNER = "beginner"
    ADVANCED = "advanced"
    PRO = "pro"


class Tune(StrEnum):
    NONE = "none"
    FILM = "film"
    ANIMATION = "animation"
    GRAIN = "grain"
    STILL_IMAGE = "stillimage"
    FAST_DECODE = "fastdecode"
    ZERO_LATENCY = "zerolatency"
    PSNR = "psnr"
    SSIM = "ssim"


_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
)
_ME = {"dia", "hex", "umh", "esa", "tesa", "star", "sea", "full"}
_PRIMARIES = {
    "bt709",
    "bt2020",
    "smpte170m",
    "smpte240m",
    "bt470m",
    "bt470bg",
}
_TRANSFERS = {
    "bt709",
    "smpte2084",
    "arib-std-b67",
    "smpte170m",
    "smpte240m",
    "bt470m",
    "bt470bg",
    "linear",
}
_MATRICES = {"bt709", "bt2020nc", "bt2020c", "smpte170m", "bt470bg", "rgb"}
_MASTER_DISPLAY_RE = re.compile(
    r"^G\(\d+,\d+\)B\(\d+,\d+\)R\(\d+,\d+\)WP\(\d+,\d+\)L\(\d+,\d+\)$"
)
_LEVEL_RE = re.compile(r"^\d(?:\.\d)?$")
_PARTITIONS = {"none", "all", "p8x8", "p4x4", "b8x8", "i8x8", "i4x4"}


@dataclass(frozen=True, slots=True)
class ColorMetadata:
    primaries: str = "bt709"
    transfer: str = "bt709"
    matrix: str = "bt709"
    range: str = "limited"
    chroma_location: str = "left"

    def __post_init__(self) -> None:
        if self.primaries not in _PRIMARIES:
            raise ValueError(f"unsupported color primaries: {self.primaries}")
        if self.transfer not in _TRANSFERS:
            raise ValueError(f"unsupported transfer characteristic: {self.transfer}")
        if self.matrix not in _MATRICES:
            raise ValueError(f"unsupported matrix coefficients: {self.matrix}")
        if self.range not in {"limited", "full"}:
            raise ValueError("color range must be limited or full")
        if self.chroma_location not in {
            "left",
            "center",
            "topleft",
            "top",
            "bottomleft",
            "bottom",
        }:
            raise ValueError("unsupported chroma sample location")


@dataclass(frozen=True, slots=True)
class VbvSettings:
    maxrate_kbps: int
    bufsize_kbps: int
    initial_fullness: float = 0.9

    def __post_init__(self) -> None:
        if self.maxrate_kbps <= 0 or self.bufsize_kbps <= 0:
            raise ValueError("VBV maxrate and bufsize must be positive")
        if not 0.0 <= self.initial_fullness <= 1.0:
            raise ValueError("VBV initial fullness must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class Hdr10Metadata:
    """Static HDR10 metadata only; no dynamic HDR10+ or Dolby Vision payload."""

    enabled: bool = False
    mastering_display: str | None = None
    max_cll: int | None = None
    max_fall: int | None = None
    hdr10_opt: bool = True

    def __post_init__(self) -> None:
        if not self.enabled:
            if any(
                value is not None
                for value in (self.mastering_display, self.max_cll, self.max_fall)
            ):
                raise ValueError("HDR10 metadata values require enabled=True")
            return
        if not self.mastering_display or not _MASTER_DISPLAY_RE.fullmatch(
            self.mastering_display
        ):
            raise ValueError("HDR10 mastering_display has an invalid x265 format")
        if self.max_cll is None or self.max_fall is None:
            raise ValueError("HDR10 requires both MaxCLL and MaxFALL")
        if not (0 <= self.max_fall <= self.max_cll <= 10000):
            raise ValueError(
                "HDR10 luminance must satisfy 0 <= MaxFALL <= MaxCLL <= 10000"
            )


@dataclass(frozen=True, slots=True)
class EncoderSettings:
    encoder: VideoEncoder
    detail_level: DetailLevel = DetailLevel.BEGINNER
    crf: float = 18.0
    preset: str = "slow"
    tune: Tune = Tune.NONE
    profile: str = "high"
    level: str | None = None
    bit_depth: int = 8
    pixel_format: str = "yuv420p"
    color: ColorMetadata = field(default_factory=ColorMetadata)
    vbv: VbvSettings | None = None

    # GOP and inter prediction
    keyint: int = 240
    min_keyint: int = 23
    scenecut: int = 40
    open_gop: bool = True
    bframes: int = 8
    b_adapt: int = 2
    b_pyramid: bool = True
    ref: int = 5
    rc_lookahead: int = 60
    weightp: int = 1
    weightb: bool = True

    # Motion search / transform
    me: str = "umh"
    merange: int = 24
    subme: int = 7
    trellis: int = 2
    partitions: str = "all"
    direct: str = "auto"

    # Perceptual tools
    aq_mode: int = 3
    aq_strength: float = 0.8
    qcomp: float = 0.65
    psy_rd: float = 1.0
    psy_rdoq: float = 0.0
    deblock_alpha: int = -1
    deblock_beta: int = -1
    chroma_qp_offset: int = 0

    # x265-specific tools.  They remain explicit in the pro schema so an
    # operator sees every material choice instead of receiving hidden defaults.
    sao: bool = True
    limit_sao: bool = False
    strong_intra_smoothing: bool = True
    rect: bool = False
    amp: bool = False
    early_skip: bool = True
    rskip: int = 1
    hdr10: Hdr10Metadata = field(default_factory=Hdr10Metadata)

    # Bitstream/audit policy
    aud: bool = False
    repeat_headers: bool = False
    annexb: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.encoder, str):
            object.__setattr__(self, "encoder", VideoEncoder(self.encoder))
        if isinstance(self.detail_level, str):
            object.__setattr__(self, "detail_level", DetailLevel(self.detail_level))
        if isinstance(self.tune, str):
            object.__setattr__(self, "tune", Tune(self.tune))
        self.validate()

    def validate(self) -> None:
        if not math.isfinite(self.crf) or not 0 <= self.crf <= 51:
            raise ValueError("CRF must be a finite value between 0 and 51")
        if self.preset not in _PRESETS:
            raise ValueError(f"unsupported encoder preset: {self.preset}")
        if self.level is not None and not _LEVEL_RE.fullmatch(self.level):
            raise ValueError("level must use a numeric codec level such as 4.1")
        if not 1 <= self.keyint <= 1000:
            raise ValueError("keyint must be between 1 and 1000")
        if not 0 <= self.min_keyint <= self.keyint:
            raise ValueError("min_keyint must be between 0 and keyint")
        if not -1 <= self.scenecut <= 100:
            raise ValueError("scenecut must be between -1 and 100")
        if not 0 <= self.bframes <= 16:
            raise ValueError("bframes must be between 0 and 16")
        if self.b_adapt not in {0, 1, 2}:
            raise ValueError("b_adapt must be 0, 1 or 2")
        if not 1 <= self.ref <= 16:
            raise ValueError("ref must be between 1 and 16")
        if not 0 <= self.rc_lookahead <= 250:
            raise ValueError("rc_lookahead must be between 0 and 250")
        if self.me not in _ME:
            raise ValueError(f"unsupported motion estimation method: {self.me}")
        if not 4 <= self.merange <= 32768:
            raise ValueError("merange must be between 4 and 32768")
        if not 0 <= self.subme <= 11:
            raise ValueError("subme must be between 0 and 11")
        if self.trellis not in {0, 1, 2}:
            raise ValueError("trellis must be 0, 1 or 2")
        partition_values = self.partitions.split(",")
        if not partition_values or any(
            value not in _PARTITIONS for value in partition_values
        ):
            raise ValueError("partitions contains an unsupported x264 partition")
        if self.direct not in {"none", "spatial", "temporal", "auto"}:
            raise ValueError("direct must be none, spatial, temporal or auto")
        if self.aq_mode not in {0, 1, 2, 3, 4}:
            raise ValueError("aq_mode must be between 0 and 4")
        if not 0 <= self.aq_strength <= 3:
            raise ValueError("aq_strength must be between 0 and 3")
        if not 0 <= self.qcomp <= 1:
            raise ValueError("qcomp must be between 0 and 1")
        if not 0 <= self.psy_rd <= 5 or not 0 <= self.psy_rdoq <= 10:
            raise ValueError("psy values are outside their supported range")
        if not -6 <= self.deblock_alpha <= 6 or not -6 <= self.deblock_beta <= 6:
            raise ValueError("deblock offsets must be between -6 and 6")
        if not -12 <= self.chroma_qp_offset <= 12:
            raise ValueError("chroma_qp_offset must be between -12 and 12")
        if self.rskip not in {0, 1, 2}:
            raise ValueError("rskip must be 0, 1 or 2")
        if self.encoder is VideoEncoder.X264:
            self._validate_x264()
        else:
            self._validate_x265()

    def _validate_x264(self) -> None:
        if self.bit_depth not in {8, 10}:
            raise ValueError("x264 bit depth must be 8 or 10")
        expected_pix_fmt = "yuv420p10le" if self.bit_depth == 10 else "yuv420p"
        if self.pixel_format != expected_pix_fmt:
            raise ValueError(
                f"x264 {self.bit_depth}-bit output requires {expected_pix_fmt}"
            )
        allowed_profiles = {8: {"high"}, 10: {"high10"}}
        if self.profile not in allowed_profiles[self.bit_depth]:
            raise ValueError(
                f"x264 {self.bit_depth}-bit output has an incompatible profile"
            )
        if self.hdr10.enabled:
            raise ValueError("HDR10 output is supported only through x265")
        if self.me in {"star", "sea", "full"}:
            raise ValueError(f"motion estimation method {self.me} is x265-only")
        if self.aq_mode == 4:
            raise ValueError("x264 aq_mode must be between 0 and 3")

    def _validate_x265(self) -> None:
        if self.bit_depth not in {8, 10, 12}:
            raise ValueError("x265 bit depth must be 8, 10 or 12")
        formats = {8: "yuv420p", 10: "yuv420p10le", 12: "yuv420p12le"}
        if self.pixel_format != formats[self.bit_depth]:
            raise ValueError(
                f"x265 {self.bit_depth}-bit output requires {formats[self.bit_depth]}"
            )
        profiles = {8: {"main"}, 10: {"main10"}, 12: {"main12"}}
        if self.profile not in profiles[self.bit_depth]:
            raise ValueError(
                f"x265 {self.bit_depth}-bit output has an incompatible profile"
            )
        if self.tune in {Tune.FILM, Tune.STILL_IMAGE}:
            raise ValueError(f"tune {self.tune.value} is not supported by x265")
        if self.me in {"esa", "tesa"}:
            raise ValueError(f"motion estimation method {self.me} is x264-only")
        if self.subme > 7:
            raise ValueError("x265 subme must be between 0 and 7")
        if self.scenecut < 0:
            raise ValueError("x265 scenecut must be between 0 and 100")
        if self.weightp not in {0, 1}:
            raise ValueError("x265 weightp must be 0 or 1")
        if self.hdr10.enabled:
            if self.bit_depth != 10 or self.profile != "main10":
                raise ValueError("HDR10 requires 10-bit x265 Main 10 output")
            if (
                self.color.primaries != "bt2020"
                or self.color.transfer != "smpte2084"
                or self.color.matrix != "bt2020nc"
            ):
                raise ValueError(
                    "HDR10 requires BT.2020 primaries, PQ and BT.2020 non-constant matrix"
                )

    def private_params(self) -> dict[str, str | int | float]:
        """Return deterministic libx264/libx265 private options."""

        common: dict[str, str | int | float] = {
            "keyint": self.keyint,
            "min-keyint": self.min_keyint,
            "scenecut": self.scenecut,
            "open-gop": int(self.open_gop),
            "bframes": self.bframes,
            "b-adapt": self.b_adapt,
            "ref": self.ref,
            "rc-lookahead": self.rc_lookahead,
            "weightb": int(self.weightb),
            "me": self.me,
            "merange": self.merange,
            "subme": self.subme,
            "aq-mode": self.aq_mode,
            "aq-strength": self.aq_strength,
            "qcomp": self.qcomp,
            "deblock": f"{self.deblock_alpha},{self.deblock_beta}",
            "aud": int(self.aud),
            "repeat-headers": int(self.repeat_headers),
            "annexb": int(self.annexb),
            "colorprim": self.color.primaries,
            "transfer": self.color.transfer,
            "colormatrix": self.color.matrix,
        }
        if self.vbv:
            common.update(
                {
                    "vbv-maxrate": self.vbv.maxrate_kbps,
                    "vbv-bufsize": self.vbv.bufsize_kbps,
                    "vbv-init": self.vbv.initial_fullness,
                }
            )
        if self.encoder is VideoEncoder.X264:
            common.update(
                {
                    "b-pyramid": "normal" if self.b_pyramid else "none",
                    "weightp": self.weightp,
                    "direct": self.direct,
                    "partitions": self.partitions,
                    "trellis": self.trellis,
                    # Keep the emitted 8-bit bitstream at High even when a
                    # speed preset (notably ultrafast) disables this High-only
                    # coding tool before private overrides are applied.
                    "8x8dct": 1,
                    "psy-rd": f"{self.psy_rd},{self.psy_rdoq}",
                    "chroma-qp-offset": self.chroma_qp_offset,
                }
            )
        else:
            common.update(
                {
                    "b-pyramid": int(self.b_pyramid),
                    "weightp": int(self.weightp > 0),
                    "psy-rd": self.psy_rd,
                    "psy-rdoq": self.psy_rdoq,
                    "sao": int(self.sao),
                    "limit-sao": int(self.limit_sao),
                    "strong-intra-smoothing": int(self.strong_intra_smoothing),
                    "rect": int(self.rect),
                    "amp": int(self.amp),
                    "early-skip": int(self.early_skip),
                    "rskip": self.rskip,
                }
            )
            if self.hdr10.enabled:
                common.update(
                    {
                        "hdr10": 1,
                        "hdr10-opt": int(self.hdr10.hdr10_opt),
                        "master-display": self.hdr10.mastering_display or "",
                        "max-cll": f"{self.hdr10.max_cll},{self.hdr10.max_fall}",
                    }
                )
        return common

    def ffmpeg_video_args(self) -> tuple[str, ...]:
        codec = "libx264" if self.encoder is VideoEncoder.X264 else "libx265"
        private_name = (
            "-x264-params" if self.encoder is VideoEncoder.X264 else "-x265-params"
        )
        params = ":".join(
            f"{key}={value}" for key, value in self.private_params().items()
        )
        args = [
            "-c:v",
            codec,
            "-preset",
            self.preset,
            "-crf",
            _number(self.crf),
            "-profile:v",
            self.profile,
            "-pix_fmt",
            self.pixel_format,
            "-color_primaries",
            self.color.primaries,
            "-color_trc",
            _ffmpeg_transfer_name(self.color.transfer),
            "-colorspace",
            self.color.matrix,
            "-color_range",
            "pc" if self.color.range == "full" else "tv",
            "-chroma_sample_location",
            self.color.chroma_location,
        ]
        if self.level:
            args.extend(("-level:v", self.level))
        if self.tune is not Tune.NONE:
            args.extend(("-tune", self.tune.value))
        args.extend((private_name, params))
        return tuple(args)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["encoder"] = self.encoder.value
        value["detail_level"] = self.detail_level.value
        value["tune"] = self.tune.value
        return value


def _number(value: float | int) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def _ffmpeg_transfer_name(value: str) -> str:
    """Translate standards names to the aliases accepted by FFmpeg 5.x."""

    return {"bt470m": "gamma22", "bt470bg": "gamma28"}.get(value, value)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    group: str
    introduced_at: DetailLevel
    required: bool
    default: Any
    value_type: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    description: str = ""

    def visible_at(self, detail_level: DetailLevel) -> bool:
        order = {DetailLevel.BEGINNER: 0, DetailLevel.ADVANCED: 1, DetailLevel.PRO: 2}
        return order[detail_level] >= order[self.introduced_at]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["introduced_at"] = self.introduced_at.value
        return value


_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "encoder",
        "rate_control",
        DetailLevel.BEGINNER,
        True,
        None,
        "enum",
        choices=("x264", "x265"),
        description="Output codec; derived from BD/UHD scan.",
    ),
    FieldSpec(
        "crf",
        "rate_control",
        DetailLevel.BEGINNER,
        True,
        18.0,
        "number",
        0,
        51,
        description="Constant-quality target; lower is higher quality and larger.",
    ),
    FieldSpec(
        "preset",
        "rate_control",
        DetailLevel.BEGINNER,
        True,
        "slow",
        "enum",
        choices=_PRESETS,
        description="Compression-efficiency versus encoding-time tradeoff.",
    ),
    FieldSpec(
        "tune",
        "psychovisual",
        DetailLevel.BEGINNER,
        True,
        "film",
        "enum",
        choices=tuple(item.value for item in Tune),
        description="Content-sensitive psychovisual defaults.",
    ),
    FieldSpec(
        "profile",
        "format",
        DetailLevel.BEGINNER,
        True,
        None,
        "enum",
        choices=("high", "high10", "main", "main10", "main12"),
    ),
    FieldSpec(
        "bit_depth", "format", DetailLevel.BEGINNER, True, None, "integer", 8, 12
    ),
    FieldSpec("pixel_format", "format", DetailLevel.BEGINNER, True, None, "string"),
    FieldSpec(
        "color",
        "format",
        DetailLevel.BEGINNER,
        True,
        None,
        "object",
        description="Scan-derived primaries, transfer, matrix and range; confirmation is mandatory.",
    ),
    FieldSpec(
        "vbv",
        "rate_control",
        DetailLevel.ADVANCED,
        False,
        None,
        "object",
        description="Optional level/device constrained VBV model.",
    ),
    FieldSpec(
        "level",
        "format",
        DetailLevel.PRO,
        False,
        None,
        "string",
        description="Optional numeric H.264/H.265 decoder level constraint.",
    ),
    FieldSpec("keyint", "gop", DetailLevel.ADVANCED, True, 240, "integer", 1, 1000),
    FieldSpec("min_keyint", "gop", DetailLevel.ADVANCED, True, 23, "integer", 0, 1000),
    FieldSpec("scenecut", "gop", DetailLevel.ADVANCED, True, 40, "integer", -1, 100),
    FieldSpec("open_gop", "gop", DetailLevel.ADVANCED, True, True, "boolean"),
    FieldSpec(
        "bframes",
        "gop",
        DetailLevel.ADVANCED,
        True,
        8,
        "integer",
        1,
        16,
        description="At least one is required for the mandatory B-frame comparison category.",
    ),
    FieldSpec("b_adapt", "gop", DetailLevel.ADVANCED, True, 2, "integer", 0, 2),
    FieldSpec("b_pyramid", "gop", DetailLevel.ADVANCED, True, True, "boolean"),
    FieldSpec("ref", "motion", DetailLevel.ADVANCED, True, 5, "integer", 1, 16),
    FieldSpec(
        "rc_lookahead",
        "rate_control",
        DetailLevel.ADVANCED,
        True,
        60,
        "integer",
        0,
        250,
    ),
    FieldSpec(
        "aq_mode", "psychovisual", DetailLevel.ADVANCED, True, 3, "integer", 0, 4
    ),
    FieldSpec(
        "aq_strength", "psychovisual", DetailLevel.ADVANCED, True, 0.8, "number", 0, 3
    ),
    FieldSpec(
        "hdr10",
        "hdr",
        DetailLevel.ADVANCED,
        False,
        None,
        "object",
        description="Static HDR10 only; scan-derived mastering data and MaxCLL/MaxFALL.",
    ),
    FieldSpec(
        "me", "motion", DetailLevel.PRO, True, "umh", "enum", choices=tuple(sorted(_ME))
    ),
    FieldSpec("merange", "motion", DetailLevel.PRO, True, 24, "integer", 4, 32768),
    FieldSpec("subme", "motion", DetailLevel.PRO, True, 10, "integer", 0, 11),
    FieldSpec("trellis", "x264", DetailLevel.PRO, True, 2, "integer", 0, 2),
    FieldSpec(
        "partitions",
        "x264",
        DetailLevel.PRO,
        True,
        "all",
        "string",
        description="x264 partition set: all, none, or a comma-separated explicit set.",
    ),
    FieldSpec(
        "direct",
        "x264",
        DetailLevel.PRO,
        True,
        "auto",
        "enum",
        choices=("none", "spatial", "temporal", "auto"),
    ),
    FieldSpec("psy_rd", "psychovisual", DetailLevel.PRO, True, 1.0, "number", 0, 5),
    FieldSpec("psy_rdoq", "psychovisual", DetailLevel.PRO, True, 0.0, "number", 0, 10),
    FieldSpec("qcomp", "rate_control", DetailLevel.PRO, True, 0.65, "number", 0, 1),
    FieldSpec("deblock_alpha", "filter", DetailLevel.PRO, True, -1, "integer", -6, 6),
    FieldSpec("deblock_beta", "filter", DetailLevel.PRO, True, -1, "integer", -6, 6),
    FieldSpec("chroma_qp_offset", "x264", DetailLevel.PRO, True, 0, "integer", -12, 12),
    FieldSpec("weightp", "motion", DetailLevel.PRO, True, 2, "integer", 0, 2),
    FieldSpec("weightb", "motion", DetailLevel.PRO, True, True, "boolean"),
    FieldSpec("sao", "x265", DetailLevel.PRO, True, True, "boolean"),
    FieldSpec("limit_sao", "x265", DetailLevel.PRO, True, False, "boolean"),
    FieldSpec("strong_intra_smoothing", "x265", DetailLevel.PRO, True, True, "boolean"),
    FieldSpec("rect", "x265", DetailLevel.PRO, True, False, "boolean"),
    FieldSpec("amp", "x265", DetailLevel.PRO, True, False, "boolean"),
    FieldSpec("early_skip", "x265", DetailLevel.PRO, True, True, "boolean"),
    FieldSpec("rskip", "x265", DetailLevel.PRO, True, 1, "integer", 0, 2),
    FieldSpec("aud", "bitstream", DetailLevel.PRO, True, False, "boolean"),
    FieldSpec("repeat_headers", "bitstream", DetailLevel.PRO, True, False, "boolean"),
    FieldSpec("annexb", "bitstream", DetailLevel.PRO, True, True, "boolean"),
)


def profile_schema(
    encoder: VideoEncoder | str, detail_level: DetailLevel | str
) -> list[dict[str, Any]]:
    """Return fields visible to one frontend detail level.

    Codec-inapplicable knobs are excluded even in pro mode; the serialized
    settings still have deterministic defaults for reproducibility.
    """

    encoder = VideoEncoder(encoder)
    detail_level = DetailLevel(detail_level)
    result: list[dict[str, Any]] = []
    for spec in _FIELD_SPECS:
        if not spec.visible_at(detail_level):
            continue
        if spec.group == "x265" and encoder is not VideoEncoder.X265:
            continue
        if spec.group == "x264" and encoder is not VideoEncoder.X264:
            continue
        if spec.name == "hdr10" and encoder is not VideoEncoder.X265:
            continue
        item = spec.to_dict()
        if spec.name == "profile":
            item["choices"] = (
                ("high", "high10")
                if encoder is VideoEncoder.X264
                else ("main", "main10", "main12")
            )
        elif spec.name == "tune" and encoder is VideoEncoder.X265:
            item["choices"] = tuple(
                tune.value for tune in Tune if tune not in {Tune.FILM, Tune.STILL_IMAGE}
            )
        elif spec.name == "me":
            item["choices"] = (
                ("dia", "hex", "umh", "esa", "tesa")
                if encoder is VideoEncoder.X264
                else ("dia", "hex", "umh", "star", "sea", "full")
            )
        elif spec.name == "subme" and encoder is VideoEncoder.X265:
            item["default"] = 5
            item["maximum"] = 7
        elif spec.name == "scenecut" and encoder is VideoEncoder.X265:
            item["minimum"] = 0
        elif spec.name == "aq_mode" and encoder is VideoEncoder.X264:
            item["maximum"] = 3
        elif spec.name == "weightp" and encoder is VideoEncoder.X265:
            item["default"] = 1
            item["maximum"] = 1
        result.append(item)
    return result


def recommended_profile(
    encoder: VideoEncoder | str,
    *,
    detail_level: DetailLevel | str = DetailLevel.BEGINNER,
    content_type: str = "film",
    hdr10: Hdr10Metadata | None = None,
    color: ColorMetadata | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> EncoderSettings:
    """Build conservative deterministic defaults; recommendations stay editable."""

    encoder = VideoEncoder(encoder)
    detail_level = DetailLevel(detail_level)
    content = content_type.strip().lower()
    tune = Tune.ANIMATION if content == "anime" else Tune.FILM
    if encoder is VideoEncoder.X264:
        settings = EncoderSettings(
            encoder=encoder,
            detail_level=detail_level,
            tune=tune,
            color=color or ColorMetadata(),
            subme=10,
            weightp=2,
        )
    else:
        hdr = hdr10 or Hdr10Metadata()
        default_color = (
            ColorMetadata("bt2020", "smpte2084", "bt2020nc", "limited", "left")
            if hdr.enabled
            else ColorMetadata()
        )
        settings = EncoderSettings(
            encoder=encoder,
            detail_level=detail_level,
            tune=Tune.ANIMATION if content == "anime" else Tune.NONE,
            profile="main10" if hdr.enabled else "main",
            bit_depth=10 if hdr.enabled else 8,
            pixel_format="yuv420p10le" if hdr.enabled else "yuv420p",
            color=color or default_color,
            hdr10=hdr,
            subme=5,
            psy_rdoq=1.0,
        )
    if not overrides:
        return settings
    unknown = set(overrides) - {
        item.name for item in settings.__dataclass_fields__.values()
    }
    if unknown:
        raise ValueError(f"unknown encoder setting(s): {', '.join(sorted(unknown))}")
    return replace(settings, **dict(overrides))
