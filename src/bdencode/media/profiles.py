"""Validated x264/x265 profiles and frontend-friendly field schemas."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from fractions import Fraction
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
    r"^G\((?P<gx>\d+),(?P<gy>\d+)\)"
    r"B\((?P<bx>\d+),(?P<by>\d+)\)"
    r"R\((?P<rx>\d+),(?P<ry>\d+)\)"
    r"WP\((?P<wpx>\d+),(?P<wpy>\d+)\)"
    r"L\((?P<lmax>\d+),(?P<lmin>\d+)\)$"
)
_LEVEL_RE = re.compile(r"^\d(?:\.\d)?$")
_PARTITIONS = {"none", "all", "p8x8", "p4x4", "b8x8", "i8x8", "i4x4"}

# H.264 Annex A, level 4.1.  A frame's luma dimensions are rounded up to
# 16x16 macroblocks before the frame-size, macroblock-rate and decoded-picture
# buffer limits are evaluated.
H264_LEVEL_4_1_MAX_FRAME_MBS = 8192
H264_LEVEL_4_1_MAX_MBPS = 245_760
H264_LEVEL_4_1_MAX_DPB_MBS = 32_768
H264_LEVEL_4_1_MAX_DIMENSION_MBS = 256
H264_HIGH_LEVEL_4_1_MAXRATE_KBPS = 62_500
H264_HIGH_LEVEL_4_1_BUFSIZE_KBPS = 78_125


def parse_frame_rate(value: str | int | float | Fraction) -> Fraction:
    """Return an exact, positive frame rate.

    ffprobe normally supplies a rational string (for example ``24000/1001``).
    Decimal strings and numeric values are accepted for test fixtures and
    operator input, but booleans and non-finite values are rejected.
    """

    if isinstance(value, bool):
        raise ValueError("frame rate must be a positive number or rational")
    try:
        rate = value if isinstance(value, Fraction) else Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("frame rate must be a positive number or rational") from exc
    if rate <= 0:
        raise ValueError("frame rate must be positive")
    return rate


def format_frame_rate(value: str | int | float | Fraction) -> str:
    """Serialize a frame rate without losing NTSC rational precision."""

    rate = parse_frame_rate(value)
    return (
        str(rate.numerator)
        if rate.denominator == 1
        else f"{rate.numerator}/{rate.denominator}"
    )


def _round_positive_fraction(value: Fraction) -> int:
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def gop_for_frame_rate(
    frame_rate: str | int | float | Fraction,
    *,
    keyint_seconds: int = 10,
    min_keyint_seconds: int = 1,
) -> tuple[int, int]:
    """Derive stable GOP lengths from presentation frame rate.

    The default is one maximum keyframe interval per ten seconds and one
    minimum interval per second.  Thus 25 fps becomes 250/25 and
    24000/1001 becomes 240/24.  Values are rounded to the nearest frame and
    constrained to the encoder schema's supported range.
    """

    if keyint_seconds <= 0 or min_keyint_seconds < 0:
        raise ValueError("GOP intervals must use positive durations")
    if min_keyint_seconds > keyint_seconds:
        raise ValueError("minimum GOP duration cannot exceed maximum duration")
    rate = parse_frame_rate(frame_rate)
    keyint = min(1000, max(1, _round_positive_fraction(rate * keyint_seconds)))
    min_keyint = min(
        keyint,
        max(0, _round_positive_fraction(rate * min_keyint_seconds)),
    )
    return keyint, min_keyint


@dataclass(frozen=True, slots=True)
class H264Level41Compatibility:
    """Structural H.264 level 4.1 result for one output picture geometry."""

    width: int
    height: int
    frame_rate: str
    macroblock_width: int
    macroblock_height: int
    frame_macroblocks: int
    macroblocks_per_second: float
    requested_reference_frames: int
    effective_reference_frames: int
    max_reference_frames: int
    dimension_compatible: bool
    frame_size_compatible: bool
    macroblock_rate_compatible: bool
    requested_compatible: bool
    compatible: bool

    @property
    def reference_frames_adjusted(self) -> bool:
        return self.requested_reference_frames != self.effective_reference_frames

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reference_frames_adjusted"] = self.reference_frames_adjusted
        return value


def h264_level_4_1_compatibility(
    width: int,
    height: int,
    frame_rate: str | int | float | Fraction,
    requested_reference_frames: int,
) -> H264Level41Compatibility:
    """Calculate level 4.1 picture/DPB compatibility and a safe ref count."""

    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise ValueError("video dimensions must be positive integers")
    if not 1 <= requested_reference_frames <= 16:
        raise ValueError("reference frames must be between 1 and 16")
    rate = parse_frame_rate(frame_rate)
    macroblock_width = (width + 15) // 16
    macroblock_height = (height + 15) // 16
    frame_macroblocks = macroblock_width * macroblock_height
    max_reference_frames = min(16, H264_LEVEL_4_1_MAX_DPB_MBS // frame_macroblocks)
    dimension_compatible = (
        macroblock_width <= H264_LEVEL_4_1_MAX_DIMENSION_MBS
        and macroblock_height <= H264_LEVEL_4_1_MAX_DIMENSION_MBS
    )
    frame_size_compatible = frame_macroblocks <= H264_LEVEL_4_1_MAX_FRAME_MBS
    macroblock_rate_compatible = frame_macroblocks * rate <= H264_LEVEL_4_1_MAX_MBPS
    structurally_compatible = (
        dimension_compatible
        and frame_size_compatible
        and macroblock_rate_compatible
        and max_reference_frames >= 1
    )
    effective_reference_frames = (
        min(requested_reference_frames, max_reference_frames)
        if structurally_compatible
        else requested_reference_frames
    )
    return H264Level41Compatibility(
        width=width,
        height=height,
        frame_rate=format_frame_rate(rate),
        macroblock_width=macroblock_width,
        macroblock_height=macroblock_height,
        frame_macroblocks=frame_macroblocks,
        macroblocks_per_second=float(frame_macroblocks * rate),
        requested_reference_frames=requested_reference_frames,
        effective_reference_frames=effective_reference_frames,
        max_reference_frames=max_reference_frames,
        dimension_compatible=dimension_compatible,
        frame_size_compatible=frame_size_compatible,
        macroblock_rate_compatible=macroblock_rate_compatible,
        requested_compatible=(
            structurally_compatible
            and requested_reference_frames <= max_reference_frames
        ),
        compatible=(
            structurally_compatible
            and effective_reference_frames <= max_reference_frames
        ),
    )


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
        for name, value in (
            ("maxrate_kbps", self.maxrate_kbps),
            ("bufsize_kbps", self.bufsize_kbps),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"VBV {name} must be a non-boolean integer")
        if self.maxrate_kbps <= 0 or self.bufsize_kbps <= 0:
            raise ValueError("VBV maxrate and bufsize must be positive")
        if isinstance(self.initial_fullness, bool) or not isinstance(
            self.initial_fullness, (int, float)
        ):
            raise TypeError("VBV initial fullness must be a real number")
        if not math.isfinite(float(self.initial_fullness)):
            raise ValueError("VBV initial fullness must be finite")
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
        for name, value in (
            ("enabled", self.enabled),
            ("hdr10_opt", self.hdr10_opt),
        ):
            if type(value) is not bool:
                raise TypeError(f"HDR10 {name} must be a boolean")
        for name, value in (
            ("max_cll", self.max_cll),
            ("max_fall", self.max_fall),
        ):
            if value is not None and type(value) is not int:
                raise TypeError(f"HDR10 {name} must be a non-boolean integer")
        if not self.enabled:
            if any(
                value is not None
                for value in (self.mastering_display, self.max_cll, self.max_fall)
            ):
                raise ValueError("HDR10 metadata values require enabled=True")
            return
        if type(self.mastering_display) is not str:
            raise ValueError("HDR10 mastering_display has an invalid x265 format")
        mastering_match = _MASTER_DISPLAY_RE.fullmatch(self.mastering_display)
        if mastering_match is None:
            raise ValueError("HDR10 mastering_display has an invalid x265 format")
        for primary in ("g", "b", "r", "wp"):
            x = int(mastering_match[f"{primary}x"])
            y = int(mastering_match[f"{primary}y"])
            if not (0 <= x <= 50_000 and 0 <= y <= 50_000):
                raise ValueError(
                    "HDR10 mastering-display chromaticity coordinates must be "
                    "between 0 and 50000"
                )
            if not 0 < x + y <= 50_000:
                raise ValueError(
                    "HDR10 mastering-display chromaticity pairs must satisfy "
                    "0 < x + y <= 50000"
                )
        maximum_luminance = int(mastering_match["lmax"])
        minimum_luminance = int(mastering_match["lmin"])
        if not (
            0 <= minimum_luminance < maximum_luminance <= 100_000_000
        ):
            raise ValueError(
                "HDR10 mastering-display luminance must satisfy "
                "0 <= minimum < maximum <= 100000000"
            )
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
    min_keyint: int = 24
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
    # Effective value reported by x264 after its encoder-open psychovisual
    # compensation.  x264 subtracts from the caller-supplied value when Psy-RD
    # and/or Psy-Trellis are active; private_params() compensates for that so
    # the manifest and the encoded bitstream describe the same value.
    chroma_qp_offset: int = -2

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
        integer_fields = (
            "bit_depth",
            "keyint",
            "min_keyint",
            "scenecut",
            "bframes",
            "b_adapt",
            "ref",
            "rc_lookahead",
            "weightp",
            "merange",
            "subme",
            "trellis",
            "aq_mode",
            "deblock_alpha",
            "deblock_beta",
            "chroma_qp_offset",
            "rskip",
        )
        real_fields = ("crf", "aq_strength", "qcomp", "psy_rd", "psy_rdoq")
        boolean_fields = (
            "open_gop",
            "b_pyramid",
            "weightb",
            "sao",
            "limit_sao",
            "strong_intra_smoothing",
            "rect",
            "amp",
            "early_skip",
            "aud",
            "repeat_headers",
            "annexb",
        )
        for name in integer_fields:
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be a non-boolean integer")
        for name in real_fields:
            value = getattr(self, name)
            label = "CRF" if name == "crf" else name
            if type(value) not in {int, float}:
                raise TypeError(f"{label} must be a non-boolean real number")
            if not math.isfinite(value):
                raise ValueError(f"{label} must be finite")
        for name in boolean_fields:
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")

        if not 0 < self.crf <= 51:
            raise ValueError(
                "CRF must be a finite value above 0 and at most 51; "
                "lossless output requires a separate policy"
            )
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
        if self.color.transfer in {"smpte2084", "arib-std-b67"}:
            raise ValueError(
                "PQ and HLG output are supported only through the x265 HDR "
                "policy"
            )
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
        if self.level is not None:
            raise ValueError(
                "explicit x265 level constraints are disabled until profile/tier "
                "conformance can be proven"
            )
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

    def x264_psy_chroma_qp_adjustment(self) -> int:
        """Return x264's encoder-open adjustment to the supplied chroma QP.

        x264 lowers the configured offset by one or two for each enabled Psy-RD
        component.  This value is the adjustment made by x264 itself (normally
        ``-2`` with this project's defaults), not the pre-compensated value we
        pass to the library.
        """

        if self.encoder is not VideoEncoder.X264:
            return 0
        if self.tune in {Tune.PSNR, Tune.SSIM}:
            return 0
        adjustment = 0
        if self.subme >= 6 and self.psy_rd > 0:
            adjustment -= 1 if self.psy_rd < 0.25 else 2
        if self.trellis and self.psy_rdoq > 0:
            adjustment -= 1 if self.psy_rdoq < 0.25 else 2
        return adjustment

    def x264_emitted_chroma_qp_offset(self) -> int:
        """Return the caller value that produces ``chroma_qp_offset``.

        The configured field is intentionally the effective/logged value.
        Compensating here avoids the former manifest=0, encoder-log=-2 split.
        """

        return self.chroma_qp_offset - self.x264_psy_chroma_qp_adjustment()

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
                    "chroma-qp-offset": self.x264_emitted_chroma_qp_offset(),
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


def source_adapted_settings(
    settings: EncoderSettings,
    *,
    width: int | None,
    height: int | None,
    frame_rate: str | int | float | Fraction | None,
) -> tuple[EncoderSettings, dict[str, Any]]:
    """Apply deterministic source-derived GOP and H.264 level policy.

    ``width`` and ``height`` are the dimensions *after* crop.  The returned
    record is JSON-safe and intended to be stored alongside the effective
    settings in an encode plan/manifest.
    """

    replacements: dict[str, Any] = {}
    policy: dict[str, Any] = {
        "output_dimensions": {"width": width, "height": height},
        "source_frame_rate": None,
        "gop": {"applied": False, "reason": "frame-rate-unavailable"},
        "h264_level_4_1": {
            "applied": False,
            "reason": "not-an-sdr-x264-output",
        },
    }
    rate: Fraction | None = None
    if frame_rate is not None:
        rate = parse_frame_rate(frame_rate)
        keyint, min_keyint = gop_for_frame_rate(rate)
        replacements.update(keyint=keyint, min_keyint=min_keyint)
        policy["source_frame_rate"] = format_frame_rate(rate)
        policy["gop"] = {
            "applied": True,
            "keyint_seconds": 10,
            "min_keyint_seconds": 1,
            "requested_keyint": settings.keyint,
            "requested_min_keyint": settings.min_keyint,
            "keyint": keyint,
            "min_keyint": min_keyint,
        }

    is_sdr_x264 = (
        settings.encoder is VideoEncoder.X264
        and not settings.hdr10.enabled
        and settings.color.transfer not in {"smpte2084", "arib-std-b67"}
    )
    dimensions_available = width is not None and height is not None
    if is_sdr_x264 and dimensions_available and rate is not None:
        assert width is not None and height is not None
        if settings.level not in {None, "4.1"}:
            raise ValueError(
                "SDR x264 Blu-ray output permits only automatic level or level 4.1"
            )
        if settings.vbv is not None and (
            settings.vbv.maxrate_kbps > H264_HIGH_LEVEL_4_1_MAXRATE_KBPS
            or settings.vbv.bufsize_kbps > H264_HIGH_LEVEL_4_1_BUFSIZE_KBPS
        ):
            raise ValueError(
                "configured H.264 High@L4.1 VBV exceeds the permitted "
                f"{H264_HIGH_LEVEL_4_1_MAXRATE_KBPS}/"
                f"{H264_HIGH_LEVEL_4_1_BUFSIZE_KBPS} kb/s caps"
            )
        level_policy = h264_level_4_1_compatibility(
            width,
            height,
            rate,
            settings.ref,
        )
        level_record = level_policy.to_dict()
        if not level_policy.compatible:
            if settings.level == "4.1":
                raise ValueError(
                    "configured H.264 level 4.1 is incompatible with the output "
                    "dimensions, frame rate or decoded-picture buffer"
                )
            level_record.update(
                applied=False,
                reason="structurally-incompatible",
                configured_level=settings.level,
            )
        else:
            replacements.update(
                level="4.1",
                ref=level_policy.effective_reference_frames,
            )
            if settings.vbv is None:
                # H.264 High@L4.1 applies the high-profile 1.25 scaling to the
                # Annex A MaxBR/MaxCPB values.  Bound CRF peaks so the explicit
                # level is a decoder contract rather than metadata alone.
                replacements["vbv"] = VbvSettings(
                    maxrate_kbps=H264_HIGH_LEVEL_4_1_MAXRATE_KBPS,
                    bufsize_kbps=H264_HIGH_LEVEL_4_1_BUFSIZE_KBPS,
                )
            level_record.update(
                applied=True,
                reason="compatible",
                configured_level="4.1",
                vbv={
                    "maxrate_kbps": (
                        settings.vbv.maxrate_kbps
                        if settings.vbv is not None
                        else H264_HIGH_LEVEL_4_1_MAXRATE_KBPS
                    ),
                    "bufsize_kbps": (
                        settings.vbv.bufsize_kbps
                        if settings.vbv is not None
                        else H264_HIGH_LEVEL_4_1_BUFSIZE_KBPS
                    ),
                    "auto_applied": settings.vbv is None,
                },
            )
        policy["h264_level_4_1"] = level_record
    elif is_sdr_x264:
        policy["h264_level_4_1"] = {
            "applied": False,
            "reason": "dimensions-or-frame-rate-unavailable",
        }

    effective = replace(settings, **replacements) if replacements else settings
    if effective.encoder is VideoEncoder.X264:
        policy["x264_chroma_qp_offset"] = {
            "effective": effective.chroma_qp_offset,
            "emitted_before_psy_adjustment": (
                effective.x264_emitted_chroma_qp_offset()
            ),
            "encoder_open_psy_adjustment": (effective.x264_psy_chroma_qp_adjustment()),
        }
    return effective, policy


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
        0.1,
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
        description="Optional numeric H.264 decoder level constraint.",
    ),
    FieldSpec("keyint", "gop", DetailLevel.ADVANCED, True, 240, "integer", 1, 1000),
    FieldSpec("min_keyint", "gop", DetailLevel.ADVANCED, True, 24, "integer", 0, 1000),
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
    FieldSpec(
        "chroma_qp_offset",
        "x264",
        DetailLevel.PRO,
        True,
        -2,
        "integer",
        -12,
        12,
        description="Effective value reported by x264 after Psy-RD compensation.",
    ),
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
        if spec.name == "level" and encoder is VideoEncoder.X265:
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
            chroma_qp_offset=0,
        )
    if not overrides:
        return settings
    requested_tune = Tune(overrides.get("tune", settings.tune))
    if encoder is VideoEncoder.X264 and requested_tune is Tune.GRAIN:
        # Keep the tune coherent instead of combining x264's grain deadzones
        # with film-oriented qcomp/AQ/deblock overrides.  Explicit operator
        # values below still win field by field.
        settings = replace(
            settings,
            tune=Tune.GRAIN,
            qcomp=0.75,
            aq_strength=0.65,
            deblock_alpha=-2,
            deblock_beta=-2,
            psy_rdoq=0.15,
        )
    unknown = set(overrides) - {
        item.name for item in settings.__dataclass_fields__.values()
    }
    if unknown:
        raise ValueError(f"unknown encoder setting(s): {', '.join(sorted(unknown))}")
    return replace(settings, **dict(overrides))
