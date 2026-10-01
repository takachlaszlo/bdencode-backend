from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from bdencode.models import JobState
from bdencode.queue import JobQueue
from bdencode.worker import JobPaths, ReviewRequired, parse_selection

from test_worker import FakeRunner, _enqueue, _selection, context  # noqa: F401


class VmafRunner(FakeRunner):
    """FakeRunner whose libvmaf answers follow a synthetic CRF -> VMAF curve."""

    def __init__(self, model: Callable[[float], float]) -> None:
        super().__init__()
        self.model = model
        self.scored: list[float] = []
        self.fail_after: int | None = None

    def run(self, argv: Any, **kwargs: Any) -> None:
        command = tuple(os.fspath(item) for item in argv)
        if command[0] != "bdencode-vmaf":
            super().run(argv, **kwargs)
            return
        if self.fail_after is not None and len(self.scored) >= self.fail_after:
            raise RuntimeError("simulated crash during a CRF probe")
        self.commands.append(command)
        encoded = Path(command[command.index("--encoded") + 1])
        match = re.fullmatch(r"probe-(\d+)_(\d+)\.mkv", encoded.name)
        assert match, encoded.name
        crf = float(f"{match[1]}.{match[2]}")
        self.scored.append(crf)
        score = self.model(crf)
        self._write(
            Path(command[command.index("--output") + 1]),
            json.dumps(
                {
                    "version": "mock",
                    "pooled_metrics": {
                        "vmaf": {"mean": score, "harmonic_mean": score - 0.4}
                    },
                }
            ),
        )


def auto_selection(**auto_crf: Any) -> dict[str, Any]:
    selection = _selection()
    selection["video"]["auto_crf"] = {"enabled": True, **auto_crf}
    return selection


def ready_job(context, selection: dict[str, Any], model):
    database, settings, scan, _scanner, _runner, worker = context
    runner = VmafRunner(model)
    worker.runner_factory = lambda _paths: runner
    worker._runners.clear()
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)
    ready = database.set_selection(job.id, selection)
    return database, settings, worker, runner, ready


def probe_encodes(runner: VmafRunner) -> list[tuple[str, ...]]:
    return [
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and "crf-search" in command[-1]
    ]


def test_selection_accepts_and_validates_the_auto_crf_block(context) -> None:
    database, _settings, scan, _scanner, _runner, worker = context
    job = _enqueue(database, scan.source)
    claimed = JobQueue(database).claim_next()
    assert claimed is not None
    worker.process_one_stage(claimed)

    base = database.set_selection(job.id, _selection())

    def parsed(selection: dict[str, Any]):
        return parse_selection(base.model_copy(update={"selection": selection}), scan)

    assert parsed(_selection()).auto_crf.enabled is False
    enabled = parsed(auto_selection(target_vmaf=94.0)).auto_crf
    assert enabled.enabled and enabled.target_vmaf == 94.0
    with pytest.raises(ReviewRequired, match="automatic CRF"):
        parsed(auto_selection(target_vmaf=50))


def test_prepare_searches_crf_and_encodes_with_the_chosen_value(context) -> None:
    database, settings, worker, runner, ready = ready_job(
        context, auto_selection(target_vmaf=95.0), lambda crf: 120.0 - 1.4 * crf
    )

    prepared = worker.process_one_stage(ready)

    assert prepared.state is JobState.ENCODING
    paths = JobPaths.create(settings, ready.id)
    report = json.loads((paths.analysis / "crf-search.json").read_text("utf-8"))
    assert report["status"] == "converged"
    chosen = report["chosen_crf"]
    assert 17.0 <= chosen <= 18.0
    assert 95.0 <= report["chosen_score"] <= 95.5
    assert report["sampled_frames"] > 0 and report["model"] == "vmaf_v0.6.1"

    sample_script = (paths.work / "crf-search" / "sample.vpy").read_text("utf-8")
    assert "core.std.Splice" in sample_script
    assert "Splice" not in paths.script.read_text("utf-8")

    plan = json.loads(paths.plan_json.read_text("utf-8"))
    assert plan["decisions"]["encoder"]["crf"] == chosen

    probes = probe_encodes(runner)
    assert len(probes) == len(report["probes"]) >= 2
    assert all("-crf" in command for command in probes)

    _scan, effective = worker._load_prepared_scan_and_selection(prepared, paths)
    assert effective.settings.crf == chosen

    encoded = worker.process_one_stage(prepared)
    assert encoded.state is JobState.MUXING
    release_encode = next(
        command
        for command in runner.commands
        if command[0] == "ffmpeg" and command[-1].endswith("video-encoded.partial.mkv")
    )
    assert release_encode[release_encode.index("-crf") + 1] == f"{chosen:g}"

    events = [event.kind for event in database.list_events(job_id=ready.id)]
    assert events.count("worker.auto-crf") == 1
    assert events.count("worker.auto-crf-probe") == len(report["probes"])


def test_probe_scoring_is_fast_and_keeps_named_pipes_out_of_the_job_tree(
    context,
) -> None:
    database, settings, worker, runner, ready = ready_job(
        context, auto_selection(target_vmaf=95.0), lambda crf: 120.0 - 1.4 * crf
    )

    worker.process_one_stage(ready)

    scoring = [command for command in runner.commands if command[0] == "bdencode-vmaf"]
    assert scoring, "no probe was scored"
    job_root = settings.job_root(ready.id)
    for command in scoring:
        # Only the VMAF score steers the search; libvmaf gets a thread pool.
        assert "--vmaf-only" in command
        assert int(command[command.index("--threads") + 1]) >= 1
        # A FIFO inside the job tree makes the storage accounting refuse the
        # whole job while a probe is being scored.
        fifo_root = Path(command[command.index("--fifo-root") + 1])
        assert fifo_root == settings.cache_root / "vmaf"
        assert job_root not in fifo_root.parents


def test_probe_checkpoints_survive_a_crash_and_are_not_repeated(context) -> None:
    database, settings, worker, runner, ready = ready_job(
        context, auto_selection(target_vmaf=95.0), lambda crf: 120.0 - 1.4 * crf
    )
    runner.fail_after = 1
    with pytest.raises(RuntimeError, match="simulated crash"):
        worker._prepare(ready, JobPaths.create(settings, ready.id), advance=False)
    assert len(runner.scored) == 1

    runner.fail_after = None
    worker._prepare(ready, JobPaths.create(settings, ready.id), advance=False)

    assert len(runner.scored) == len(set(runner.scored)), "a CRF was probed twice"
    assert (
        JobPaths.create(settings, ready.id).analysis / "crf-search.json"
    ).is_file()


def test_unreachable_target_sends_the_job_to_review(context) -> None:
    database, settings, worker, runner, ready = ready_job(
        context,
        auto_selection(target_vmaf=99.0, min_crf=14, max_crf=24),
        lambda crf: 80.0,
    )

    result = worker.process_job(ready)

    assert result.state is JobState.NEEDS_REVIEW
    paths = JobPaths.create(settings, ready.id)
    report = json.loads((paths.analysis / "crf-search.json").read_text("utf-8"))
    assert report["status"] == "target_unreachable" and report["chosen_crf"] is None
    assert not (paths.stages / "crf-search.json").exists()
    assert not any(
        command[0] == "ffmpeg" and command[-1].endswith("video-encoded.partial.mkv")
        for command in runner.commands
    )


def test_ceiling_outcome_explains_itself_in_the_event(context) -> None:
    # A very easy title: the target is met even at the highest allowed CRF.
    database, settings, worker, runner, ready = ready_job(
        context, auto_selection(target_vmaf=95.0), lambda crf: 99.5
    )

    prepared = worker.process_one_stage(ready)

    assert prepared.state is JobState.ENCODING
    paths = JobPaths.create(settings, ready.id)
    report = json.loads((paths.analysis / "crf-search.json").read_text("utf-8"))
    assert report["status"] == "max_crf_reached" and report["chosen_crf"] == 26.0
    message = next(
        event.message
        for event in database.list_events(job_id=ready.id)
        if event.kind == "worker.auto-crf"
    )
    assert "CRF 26" in message
    assert "highest allowed CRF" in message and "max_crf" in message


def test_changing_the_selection_invalidates_a_stale_crf_report(context) -> None:
    database, settings, worker, runner, ready = ready_job(
        context, auto_selection(target_vmaf=95.0), lambda crf: 120.0 - 1.4 * crf
    )
    worker.process_one_stage(ready)
    paths = JobPaths.create(settings, ready.id)
    assert worker._preparation_is_current(ready, paths)

    changed = ready.model_copy(
        update={"selection": auto_selection(target_vmaf=93.0)}
    )
    assert not worker._preparation_is_current(changed, paths)
    with pytest.raises(ReviewRequired, match="stale"):
        worker._load_prepared_scan_and_selection(changed, paths)
