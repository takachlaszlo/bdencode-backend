from __future__ import annotations

import json
import os
from typing import Any

from bdencode.queue import JobQueue
from bdencode.worker import JobPaths

from test_worker import _enqueue, _selection, context  # noqa: F401


def _line(t: float, width: int, height: int, x: int, y: int) -> str:
    return (
        f"[Parsed_cropdetect_0 @ 0x1] x1:{x} x2:{x + width - 1} y1:{y} "
        f"y2:{y + height - 1} w:{width} h:{height} x:{x} y:{y} pts:{int(t * 1000)} "
        f"t:{t:.6f} limit:0.094118 crop={width}:{height}:{x}:{y}"
    )


def _prepare_with_cropdetect_log(context, log: str):
    database, settings, scan, _scanner, runner, worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, _selection())
    real_run = runner.run

    def run(*args: Any, **kwargs: Any) -> None:
        real_run(*args, **kwargs)
        command = tuple(os.fspath(item) for item in args[0])
        stderr_path = kwargs.get("stderr_path")
        if stderr_path is not None and any("cropdetect=" in item for item in command):
            runner._write(stderr_path, log)

    runner.run = run  # type: ignore[method-assign]
    paths = JobPaths.create(settings, job.id)
    worker._prepare(ready, paths, advance=False)
    return database, job, paths


def test_variable_aspect_title_records_a_profile_and_keeps_the_widest_canvas(context) -> None:
    lines = [_line(t, 1920, 800, 0, 140) for t in range(0, 3000)]
    lines += [_line(t, 1920, 1080, 0, 0) for t in range(3000, 7200)]
    database, job, paths = _prepare_with_cropdetect_log(context, "\n".join(lines) + "\n")

    report = json.loads((paths.analysis / "crop-policy.json").read_text("utf-8"))
    assert report["status"] == "passed"
    profile = report["aspect_profile"]
    assert profile["variable"] is True
    assert profile["widest"]["height"] == 1080
    assert profile["expansions"][0]["time_seconds"] == 3000.0
    assert report["evidence"]["variable_aspect"] is True
    # The widest canvas is the release-safe crop: nothing is trimmed.
    assert report["decision"]["requested"] == {
        "left": 0,
        "top": 0,
        "right": 0,
        "bottom": 0,
    }
    events = [e for e in database.list_events(job_id=job.id) if e.kind == "worker.variable-aspect"]
    assert len(events) == 1
    assert "variable aspect ratio" in events[0].message


def test_constant_aspect_title_emits_no_variable_aspect_event(context) -> None:
    lines = [_line(t, 1920, 800, 0, 140) for t in range(0, 7200)]
    database, job, paths = _prepare_with_cropdetect_log(context, "\n".join(lines) + "\n")

    report = json.loads((paths.analysis / "crop-policy.json").read_text("utf-8"))
    assert report["aspect_profile"]["variable"] is False
    assert report["decision"]["requested"]["top"] == 140
    assert not [
        e for e in database.list_events(job_id=job.id) if e.kind == "worker.variable-aspect"
    ]


def test_noisy_envelope_still_requires_crop_review(context) -> None:
    import pytest

    from bdencode.worker import ReviewRequired

    lines = []
    for step in range(8):
        for t in range(step * 800, (step + 1) * 800):
            height = 600 + 60 * step
            lines.append(_line(t, 1920, height, 0, (1080 - height) // 2))
    with pytest.raises(ReviewRequired, match="crop policy requires review"):
        _prepare_with_cropdetect_log(context, chr(10).join(lines) + chr(10))
