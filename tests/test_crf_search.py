from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from bdencode.crf_search import (
    STATUS_BEST_EFFORT,
    STATUS_CONVERGED,
    STATUS_MAX_CRF,
    STATUS_UNREACHABLE,
    AutoCrfConfig,
    CrfSearch,
    CrfSearchError,
    SampleWindow,
    plan_sample_windows,
    quantize_crf,
    vmaf_score,
)
from bdencode.vapoursynth import ReferenceScriptPlan, render_reference_script, script_record


def run_search(
    config: AutoCrfConfig, initial: float, model
) -> tuple[CrfSearch, list[float]]:
    search = CrfSearch(config, initial)
    visited: list[float] = []
    while (crf := search.next_crf()) is not None:
        visited.append(crf)
        search.record(crf, model(crf))
        assert len(visited) <= config.max_iterations
    return search, visited


def linear(intercept: float, slope: float):
    return lambda crf: min(100.0, intercept + slope * crf)


def test_config_defaults_are_disabled_and_valid() -> None:
    config = AutoCrfConfig()
    assert config.enabled is False
    assert AutoCrfConfig.from_mapping(None) == config
    assert AutoCrfConfig.from_mapping({"enabled": True, "target_vmaf": 94}).target_vmaf == 94


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": 1},
        {"target_vmaf": 70},
        {"target_vmaf": float("nan")},
        {"min_crf": 20, "max_crf": 20},
        {"min_crf": 0},
        {"max_crf": 60},
        {"samples": 2},
        {"samples": True},
        {"sample_seconds": 0.5},
        {"max_iterations": 1},
        {"tolerance": 0},
        {"metric": "median"},
        {"probe_preset": ""},
        {"surprise": 1},
    ],
)
def test_config_rejects_unsafe_values(raw: dict[str, object]) -> None:
    with pytest.raises(CrfSearchError):
        AutoCrfConfig.from_mapping(raw)


def test_config_rejects_non_object() -> None:
    with pytest.raises(CrfSearchError):
        AutoCrfConfig.from_mapping("yes")


def test_sample_windows_are_ordered_disjoint_and_skip_the_edges() -> None:
    total = 172_627
    windows = plan_sample_windows(
        total, Fraction(24000, 1001), samples=12, sample_seconds=3
    )
    assert len(windows) == 12
    assert all(window.frame_count == 72 for window in windows)
    edge = int(total * 0.03)
    assert windows[0].start_frame >= edge
    assert windows[-1].end_frame <= total - edge
    for left, right in zip(windows, windows[1:]):
        assert left.end_frame <= right.start_frame
    assert windows == plan_sample_windows(
        total, Fraction(24000, 1001), samples=12, sample_seconds=3
    )


def test_sample_windows_shrink_for_short_titles() -> None:
    windows = plan_sample_windows(
        600, Fraction(25), samples=12, sample_seconds=3
    )
    assert 2 <= len(windows) < 12
    assert windows[-1].end_frame <= 600
    with pytest.raises(CrfSearchError):
        plan_sample_windows(100, Fraction(25), samples=12, sample_seconds=3)
    with pytest.raises(CrfSearchError):
        plan_sample_windows(0, Fraction(25), samples=12, sample_seconds=3)


def test_sample_script_splices_windows_after_all_other_filters(tmp_path: Path) -> None:
    plan = ReferenceScriptPlan(
        source=tmp_path / "reference.mkv",
        cache_path=tmp_path / "cache",
        script_path=tmp_path / "sample.vpy",
        sample_windows=((100, 72), (500, 72)),
    )
    content = render_reference_script(plan)
    assert "core.std.Splice([src[100:172], src[500:572]], mismatch=False)" in content
    assert content.index("Splice") < content.index("src.set_output()")
    assert script_record(plan, content)["sample_windows"] == [[100, 72], [500, 72]]

    single = render_reference_script(
        ReferenceScriptPlan(
            source=tmp_path / "reference.mkv",
            cache_path=tmp_path / "cache",
            script_path=tmp_path / "sample.vpy",
            sample_windows=((100, 72),),
        )
    )
    assert "src = src[100:172]" in single


def test_ordinary_reference_script_record_is_unchanged(tmp_path: Path) -> None:
    plan = ReferenceScriptPlan(
        source=tmp_path / "reference.mkv",
        cache_path=tmp_path / "cache",
        script_path=tmp_path / "script.vpy",
    )
    content = render_reference_script(plan)
    assert "sample_windows" not in script_record(plan, content)
    assert "Splice" not in content


@pytest.mark.parametrize(
    "windows",
    [((10, 5), (12, 5)), ((10, 0),), ((-1, 5),), ((True, 5),)],
)
def test_sample_script_rejects_overlapping_or_invalid_windows(
    tmp_path: Path, windows: tuple[tuple[int, int], ...]
) -> None:
    with pytest.raises(ValueError):
        ReferenceScriptPlan(
            source=tmp_path / "reference.mkv",
            cache_path=tmp_path / "cache",
            script_path=tmp_path / "sample.vpy",
            sample_windows=windows,
        )


def test_quantize_uses_quarter_steps() -> None:
    assert quantize_crf(18.1) == 18.0
    assert quantize_crf(18.13) == 18.25
    assert quantize_crf(21.9) == 22.0


def test_search_converges_close_to_the_target_on_a_linear_model() -> None:
    config = AutoCrfConfig(enabled=True, target_vmaf=95.0)
    search, visited = run_search(config, 18.0, linear(120.0, -1.4))
    outcome = search.outcome()
    assert outcome.status == STATUS_CONVERGED
    assert outcome.chosen_score is not None
    assert 0 <= outcome.chosen_score - 95.0 <= config.tolerance
    assert outcome.chosen_crf == pytest.approx(17.75, abs=0.5)
    assert len(visited) <= 5
    assert outcome.monotonic


def test_search_walks_up_when_the_first_probe_is_too_good() -> None:
    config = AutoCrfConfig(enabled=True, target_vmaf=93.0)
    search, visited = run_search(config, 16.0, linear(118.0, -1.2))
    outcome = search.outcome()
    assert visited[0] == 16.0 and visited[1] > 16.0
    assert outcome.chosen_score is not None and outcome.chosen_score >= 93.0
    assert outcome.chosen_crf is not None and outcome.chosen_crf > 16.0


def test_search_walks_down_when_the_first_probe_misses() -> None:
    config = AutoCrfConfig(enabled=True, target_vmaf=96.0)
    search, visited = run_search(config, 24.0, linear(125.0, -1.3))
    outcome = search.outcome()
    assert visited[1] < 24.0
    assert outcome.chosen_crf is not None and outcome.chosen_crf < 24.0
    assert outcome.chosen_score is not None and outcome.chosen_score >= 96.0


def test_search_never_selects_a_probe_that_missed_the_target() -> None:
    config = AutoCrfConfig(enabled=True, target_vmaf=95.0, max_iterations=3)
    search, _ = run_search(config, 22.0, linear(122.0, -1.1))
    outcome = search.outcome()
    for probe in outcome.probes:
        if probe.crf == outcome.chosen_crf:
            assert probe.score >= 95.0


def test_search_reports_unreachable_target_at_the_lower_bound() -> None:
    config = AutoCrfConfig(enabled=True, target_vmaf=99.0, min_crf=14, max_crf=24)
    search, visited = run_search(config, 18.0, linear(101.0, -0.5))
    outcome = search.outcome()
    assert outcome.status == STATUS_UNREACHABLE
    assert outcome.chosen_crf is None and not outcome.usable
    assert min(visited) == 14.0


def test_search_stops_at_max_crf_when_everything_passes() -> None:
    config = AutoCrfConfig(enabled=True, target_vmaf=90.0, min_crf=16, max_crf=22)
    search, visited = run_search(config, 18.0, lambda crf: 99.0)
    outcome = search.outcome()
    assert outcome.status == STATUS_MAX_CRF
    assert outcome.chosen_crf == 22.0
    assert max(visited) == 22.0


def test_search_is_best_effort_when_iterations_run_out() -> None:
    config = AutoCrfConfig(
        enabled=True, target_vmaf=95.0, max_iterations=3, tolerance=0.1
    )
    search, visited = run_search(config, 14.0, linear(140.0, -2.0))
    assert len(visited) == 3
    outcome = search.outcome()
    assert outcome.status in {STATUS_BEST_EFFORT, STATUS_CONVERGED}
    assert outcome.chosen_score is not None and outcome.chosen_score >= 95.0


def test_non_monotonic_measurements_do_not_pick_a_pass_above_a_fail() -> None:
    config = AutoCrfConfig(enabled=True, target_vmaf=95.0)
    search = CrfSearch(config, 20.0)
    search.record(20.0, 96.0)
    search.record(22.0, 94.0)
    search.record(24.0, 96.5)  # noisy: passes above a failing CRF
    outcome = search.outcome()
    assert outcome.chosen_crf == 20.0
    assert not outcome.monotonic


def test_initial_crf_is_clamped_to_the_bounds_and_duplicates_are_refused() -> None:
    config = AutoCrfConfig(enabled=True, min_crf=16, max_crf=22)
    assert CrfSearch(config, 5.0).next_crf() == 16.0
    assert CrfSearch(config, 40.0).next_crf() == 22.0
    search = CrfSearch(config, 18.0)
    search.record(18.0, 95.0)
    with pytest.raises(CrfSearchError):
        search.record(18.0, 95.0)
    with pytest.raises(CrfSearchError):
        search.record(19.0, float("nan"))


def test_vmaf_score_prefers_pooled_metrics_and_falls_back_to_frames() -> None:
    pooled = {"pooled_metrics": {"vmaf": {"mean": 94.5, "harmonic_mean": 93.0}}}
    assert vmaf_score(pooled, "mean") == 94.5
    assert vmaf_score(pooled, "harmonic_mean") == 93.0

    frames = {
        "frames": [
            {"frameNum": index, "metrics": {"vmaf": score}}
            for index, score in enumerate([90.0, 95.0, 100.0, 80.0])
        ]
    }
    assert vmaf_score(frames, "mean") == pytest.approx(91.25)
    assert vmaf_score(frames, "harmonic_mean") < 91.25
    assert vmaf_score(frames, "percentile_1") == 80.0


def test_vmaf_score_rejects_documents_without_scores() -> None:
    with pytest.raises(CrfSearchError):
        vmaf_score({"version": "mock", "frames": [{"frameNum": 0}]})
    with pytest.raises(CrfSearchError):
        vmaf_score({}, "percentile_1")
    with pytest.raises(CrfSearchError):
        vmaf_score({}, "median")


def test_sample_window_validation() -> None:
    with pytest.raises(CrfSearchError):
        SampleWindow(-1, 5)
    with pytest.raises(CrfSearchError):
        SampleWindow(0, 0)
    assert SampleWindow(10, 5).end_frame == 15
