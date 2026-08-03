"""Deterministic Matroska chapter documents from the reviewed BD playlist."""

from __future__ import annotations

import math
from collections.abc import Iterable


def _matroska_timestamp(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("chapter timestamp must be a finite non-negative number")
    nanoseconds = round(seconds * 1_000_000_000)
    hours, remainder = divmod(nanoseconds, 3_600_000_000_000)
    minutes, remainder = divmod(remainder, 60_000_000_000)
    whole_seconds, fraction = divmod(remainder, 1_000_000_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{fraction:09d}"


def render_matroska_chapters(chapter_starts: Iterable[float]) -> str:
    """Render ordered BD chapter starts as mkvmerge-compatible XML.

    Blu-ray playlists carry chapter timestamps even when FFmpeg's libbluray
    input does not expose them as container chapters.  Building the document
    from the reviewed scan therefore keeps retries deterministic and avoids a
    missing ``mkvextract`` output for otherwise valid sources.
    """

    timestamps: list[str] = []
    previous_ns = -1
    for raw in chapter_starts:
        seconds = float(raw)
        timestamp = _matroska_timestamp(seconds)
        current_ns = round(seconds * 1_000_000_000)
        if current_ns < previous_ns:
            raise ValueError("chapter timestamps must be ordered")
        if current_ns == previous_ns:
            continue
        timestamps.append(timestamp)
        previous_ns = current_ns

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<Chapters>",
        "  <EditionEntry>",
        "    <EditionFlagHidden>0</EditionFlagHidden>",
        "    <EditionFlagDefault>1</EditionFlagDefault>",
    ]
    for number, timestamp in enumerate(timestamps, start=1):
        lines.extend(
            (
                "    <ChapterAtom>",
                f"      <ChapterTimeStart>{timestamp}</ChapterTimeStart>",
                "      <ChapterFlagHidden>0</ChapterFlagHidden>",
                "      <ChapterFlagEnabled>1</ChapterFlagEnabled>",
                "      <ChapterDisplay>",
                f"        <ChapterString>Chapter {number:02d}</ChapterString>",
                "        <ChapterLanguage>und</ChapterLanguage>",
                "      </ChapterDisplay>",
                "    </ChapterAtom>",
            )
        )
    lines.extend(("  </EditionEntry>", "</Chapters>"))
    return "\n".join(lines) + "\n"
