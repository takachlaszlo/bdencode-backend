"""Named grain/noise presets expressed as concrete encoder settings.

A preset is only a convenience for the operator: it expands to ordinary
``EncoderSettings`` field values, so the selection, the manifest and the FFmpeg
command always describe the exact numbers that were used.  Nothing here touches
the reference picture; noise handling is performed by the encoder itself, which
keeps the SSIM/PSNR/VMAF evidence a statement about the codec rather than about
a pre-filter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .profiles import EncoderSettings, VideoEncoder, recommended_profile


@dataclass(frozen=True, slots=True)
class NoiseProfile:
    id: str
    label: str
    description: str
    settings: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "settings": dict(self.settings),
        }


_X264: tuple[NoiseProfile, ...] = (
    NoiseProfile(
        "off",
        "Nincs",
        "A forrás zaját és szemcséjét az alapértelmezett profil kezeli.",
        {},
    ),
    NoiseProfile(
        "preserve_grain",
        "Szemcse megtartása",
        "Filmszemcsés forráshoz: grain tune, magasabb qcomp és enyhébb deblock, "
        "hogy a szemcse ne simuljon el. Nagyobb fájlméretre számíts.",
        {
            "tune": "grain",
            "qcomp": 0.75,
            "aq_strength": 0.65,
            "deblock_alpha": -2,
            "deblock_beta": -2,
            "psy_rdoq": 0.15,
            "noise_reduction": 0,
        },
    ),
    NoiseProfile(
        "light_denoise",
        "Enyhe zajszűrés",
        "Kódolóba épített zajcsökkentés (nr 40): a finom digitális zajt "
        "csökkenti, a részleteket érintetlenül hagyja.",
        {"noise_reduction": 40},
    ),
    NoiseProfile(
        "medium_denoise",
        "Közepes zajszűrés",
        "Kódolóba épített zajcsökkentés (nr 120): látható zajos vagy szemcsés "
        "forráshoz, mérhető bitrate-megtakarítással.",
        {"noise_reduction": 120},
    ),
    NoiseProfile(
        "strong_denoise",
        "Erős zajszűrés",
        "Kódolóba épített zajcsökkentés (nr 300): nagyon zajos forráshoz. "
        "Részletvesztést okozhat, a QC-kapuk ezt jelzik.",
        {"noise_reduction": 300},
    ),
)

_X265: tuple[NoiseProfile, ...] = (
    _X264[0],
    NoiseProfile(
        "preserve_grain",
        "Szemcse megtartása",
        "Filmszemcsés UHD forráshoz: grain tune, magasabb psy-rd/psy-rdoq, "
        "kikapcsolt SAO és enyhébb deblock. Nagyobb fájlméretre számíts.",
        {
            "tune": "grain",
            "psy_rd": 2.5,
            "psy_rdoq": 3.0,
            "qcomp": 0.75,
            "aq_strength": 0.8,
            "deblock_alpha": -2,
            "deblock_beta": -2,
            "sao": False,
            "noise_reduction": 0,
        },
    ),
    NoiseProfile(
        "light_denoise",
        "Enyhe zajszűrés",
        "Kódolóba épített zajcsökkentés (nr-intra/nr-inter 100): a finom "
        "digitális zajt csökkenti.",
        {"noise_reduction": 100},
    ),
    NoiseProfile(
        "medium_denoise",
        "Közepes zajszűrés",
        "Kódolóba épített zajcsökkentés (nr-intra/nr-inter 250): látható zajos "
        "vagy szemcsés forráshoz.",
        {"noise_reduction": 250},
    ),
    NoiseProfile(
        "strong_denoise",
        "Erős zajszűrés",
        "Kódolóba épített zajcsökkentés (nr-intra/nr-inter 500): nagyon zajos "
        "forráshoz. Részletvesztést okozhat, a QC-kapuk ezt jelzik.",
        {"noise_reduction": 500},
    ),
)


def noise_profiles(encoder: VideoEncoder | str) -> tuple[NoiseProfile, ...]:
    return _X264 if VideoEncoder(encoder) is VideoEncoder.X264 else _X265


def noise_profile(encoder: VideoEncoder | str, profile_id: str) -> NoiseProfile:
    for item in noise_profiles(encoder):
        if item.id == profile_id:
            return item
    raise ValueError(f"unknown noise profile: {profile_id}")


def preset_settings(
    encoder: VideoEncoder | str,
    profile_id: str,
    *,
    content_type: str = "film",
) -> dict[str, Any]:
    """Return concrete values for every field the preset family manages.

    Presets are mutually exclusive choices.  Each result therefore starts from
    the encoder's recommended defaults for ``content_type`` and only then
    applies the preset, so switching from a grain preset to a denoise preset
    cannot leave a grain ``qcomp`` behind.
    """

    encoder = VideoEncoder(encoder)
    preset = noise_profile(encoder, profile_id)
    baseline = recommended_profile(encoder, content_type=content_type)
    managed = sorted({key for item in noise_profiles(encoder) for key in item.settings})
    values: dict[str, Any] = {}
    for key in managed:
        current = getattr(baseline, key)
        values[key] = getattr(current, "value", current)
    values.update(preset.settings)
    return values


def apply_noise_profile(
    settings: EncoderSettings, profile_id: str, *, content_type: str = "film"
) -> EncoderSettings:
    """Return ``settings`` with the preset applied; other fields are untouched."""

    return replace(
        settings,
        **preset_settings(settings.encoder, profile_id, content_type=content_type),
    )
