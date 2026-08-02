"""Quality-control and comparison primitives."""

from .artifacts import Artifact, ArtifactKind, PngInfo, inspect_png
from .audio import AudioComparison, AudioProbe, compare_audio_probes
from .video import FramePair, FrameRecord, select_frame_pairs

__all__ = [
    "Artifact",
    "ArtifactKind",
    "AudioComparison",
    "AudioProbe",
    "FramePair",
    "FrameRecord",
    "PngInfo",
    "compare_audio_probes",
    "inspect_png",
    "select_frame_pairs",
]
