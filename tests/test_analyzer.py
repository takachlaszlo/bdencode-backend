from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from bdencode.analyzer import MkvAnalyzer


class FakeRunner:
    def capture(self, argv, *, timeout=30, check=True):
        name = str(argv[0])
        if name == "mkvmerge":
            value = {
                "container": {"type": "Matroska"},
                "tracks": [{"id": 0, "type": "video"}, {"id": 1, "type": "audio"}],
                "attachments": [
                    {"id": 1, "file_name": "encode.log", "content_type": "text/plain"}
                ],
            }
        elif name == "mediainfo":
            value = {
                "media": {
                    "track": [
                        {
                            "@type": "Video",
                            "Encoded_Library": "x264 core 164",
                            "Encoded_Library_Settings": "crf=18 / ref=5",
                        }
                    ]
                }
            }
        else:
            value = {
                "streams": [
                    {"index": 0, "codec_type": "video", "tags": {"ENCODER": "x264"}}
                ],
                "format": {"format_name": "matroska"},
            }
        return SimpleNamespace(stdout=json.dumps(value), stderr="", returncode=0)


def test_mkv_analyzer_extracts_settings_without_comparison_attachment(
    tmp_path: Path,
) -> None:
    target = tmp_path / "encode.mkv"
    target.write_bytes(b"mkv")
    result = MkvAnalyzer(FakeRunner()).analyze(target)
    assert result.encoder_settings[0]["fields"]["Encoded_Library_Settings"].startswith(
        "crf=18"
    )
    assert not result.comparison_attachment_violation
    assert result.attachments[0]["name"] == "encode.log"
