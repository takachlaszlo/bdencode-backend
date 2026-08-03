from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from bdencode.chapters import render_matroska_chapters


def test_render_matroska_chapters_preserves_bd_timestamps() -> None:
    document = render_matroska_chapters((0.0, 440.08, 1007.6))
    root = ET.fromstring(document)

    assert root.tag == "Chapters"
    assert [item.text for item in root.findall(".//ChapterTimeStart")] == [
        "00:00:00.000000000",
        "00:07:20.080000000",
        "00:16:47.600000000",
    ]
    assert [item.text for item in root.findall(".//ChapterString")] == [
        "Chapter 01",
        "Chapter 02",
        "Chapter 03",
    ]


def test_render_matroska_chapters_deduplicates_equal_timestamps() -> None:
    document = render_matroska_chapters((0.0, 0.0, 60.0))
    root = ET.fromstring(document)

    assert len(root.findall(".//ChapterAtom")) == 2


@pytest.mark.parametrize("chapters", [(0.0, -1.0), (0.0, float("nan")), (5.0, 4.0)])
def test_render_matroska_chapters_rejects_invalid_timestamps(
    chapters: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError):
        render_matroska_chapters(chapters)
