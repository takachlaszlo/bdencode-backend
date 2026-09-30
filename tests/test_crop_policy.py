from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from bdencode.qc.crop import (
    automatic_crop,
    CropDetectionEvidence,
    CropPolicyError,
    distributed_cropdetect_commands,
    full_title_cropdetect_command,
    parse_stable_cropdetect,
    plan_cropdetect_intervals,
    validate_operator_crop,
)
from bdencode.qc.video import CropMargins


def _crop_lines(value: str, count: int) -> list[str]:
    return [f"frame={index} crop={value}" for index in range(count)]


def _evidence(crop: CropMargins) -> CropDetectionEvidence:
    return CropDetectionEvidence(
        crop=crop,
        source_width=1920,
        source_height=1080,
        observations=100,
        supporting_observations=92,
        support_ratio=Decimal("0.92"),
        jitter_pixels=2,
    )


def test_cropdetect_plan_is_bounded_and_distributed_across_title() -> None:
    intervals = plan_cropdetect_intervals(7200)
    assert len(intervals) == 12
    assert {interval.duration_seconds for interval in intervals} == {Decimal("2")}
    assert intervals[0].start_seconds == Decimal("299")
    assert intervals[-1].start_seconds == Decimal("6899")
    assert sum(item.duration_seconds for item in intervals) == Decimal("24")

    commands = distributed_cropdetect_commands(Path("reference.mkv"), 7200)
    assert len(commands) == 12
    for interval, command in zip(intervals, commands):
        assert command.index("-ss") < command.index("-i")
        assert Decimal(command[command.index("-ss") + 1]) == max(
            Decimal(0), interval.start_seconds - Decimal(12)
        )
        output_seek = command.index("-ss", command.index("-i") + 1)
        assert Decimal(command[output_seek + 1]) == Decimal(12)
        assert command[command.index("-t") + 1] == "2"
        assert command[command.index("-map") + 1] == "0:v:0"
        assert command[command.index("-vf") + 1] == (
            "cropdetect=limit=0.094:round=2:reset=0"
        )
        assert command[-2:] == ["null", "-"]


def test_full_title_cropdetect_inspects_every_frame_before_rate_limiting() -> None:
    command = full_title_cropdetect_command(Path("reference.mkv"))

    assert "-ss" not in command
    assert "-t" not in command
    assert command[command.index("-map") + 1] == "0:v:0"
    assert command[command.index("-vf") + 1] == (
        "cropdetect=limit=0.094:round=2:reset=0,fps=1"
    )
    assert "-xerror" in command
    assert command[command.index("-err_detect") + 1] == "explode"
    assert command[-2:] == ["null", "-"]


def test_short_title_uses_fewer_non_overlapping_samples() -> None:
    intervals = plan_cropdetect_intervals(Decimal("5"), samples=12, sample_seconds=2)
    assert len(intervals) == 2
    assert intervals == (
        type(intervals[0])(Decimal("0.25"), Decimal("2")),
        type(intervals[0])(Decimal("2.75"), Decimal("2")),
    )


def test_parse_stable_cropdetect_accepts_modal_border_with_small_jitter() -> None:
    log = "\n".join(
        _crop_lines("1480:1080:220:0", 28)
        + _crop_lines("1478:1080:222:0", 6)
        + _crop_lines("1920:1080:0:0", 6)
    )
    evidence = parse_stable_cropdetect(
        log, source_width=1920, source_height=1080
    )
    assert evidence.crop == CropMargins(left=220, right=220)
    assert evidence.observations == 40
    assert evidence.supporting_observations == 34
    assert evidence.support_ratio == Decimal("0.85")
    assert evidence.safe_crop == CropMargins()


def test_crop_policy_preserves_variable_aspect_picture_across_windows() -> None:
    logs = tuple(
        "\n".join(_crop_lines(value, 3))
        for value in (["1920:804:0:138"] * 9 + ["1920:1080:0:0"] * 3)
    )
    evidence = parse_stable_cropdetect(
        logs, source_width=1920, source_height=1080
    )

    assert evidence.crop == CropMargins(top=138, bottom=138)
    assert evidence.support_ratio == Decimal("0.75")
    assert evidence.safe_crop == CropMargins()
    assert validate_operator_crop(None, evidence).release_safe == CropMargins()
    with pytest.raises(CropPolicyError) as error:
        validate_operator_crop(CropMargins(top=138, bottom=138), evidence)
    assert error.value.code == "variable_aspect_ratio"


def test_crop_policy_accepts_constant_letterbox_crop_across_windows() -> None:
    logs = tuple("\n".join(_crop_lines("1920:804:0:138", 3)) for _ in range(12))
    evidence = parse_stable_cropdetect(
        logs, source_width=1920, source_height=1080
    )

    selected = CropMargins(top=138, bottom=138)
    assert evidence.safe_crop == selected
    assert validate_operator_crop(selected, evidence).release_safe == selected


def test_parse_stable_cropdetect_rejects_weak_or_sparse_evidence() -> None:
    sparse = "\n".join(_crop_lines("1480:1080:220:0", 10))
    with pytest.raises(CropPolicyError, match="too few") as sparse_error:
        parse_stable_cropdetect(sparse, source_width=1920, source_height=1080)
    assert sparse_error.value.code == "insufficient_detection"

    unstable = "\n".join(
        _crop_lines("1480:1080:220:0", 13)
        + _crop_lines("1920:1080:0:0", 12)
    )
    with pytest.raises(CropPolicyError, match="not stable") as unstable_error:
        parse_stable_cropdetect(unstable, source_width=1920, source_height=1080)
    assert unstable_error.value.code == "unstable_detection"


def test_crop_policy_allows_no_crop_when_no_bar_is_detected() -> None:
    decision = validate_operator_crop(None, _evidence(CropMargins()))
    assert decision.status == "accepted"
    assert decision.requested == CropMargins()
    assert decision.residual == CropMargins()


def test_automatic_crop_uses_release_safe_borders_and_preserves_thin_noise() -> None:
    letterbox = _evidence(CropMargins(top=138, bottom=138))
    assert automatic_crop(letterbox) == CropMargins(top=138, bottom=138)

    thin = _evidence(CropMargins(left=6, right=6))
    assert automatic_crop(thin) == CropMargins()


def test_crop_policy_allows_thin_noise_and_conservative_safety_margin() -> None:
    thin = validate_operator_crop(None, _evidence(CropMargins(top=6, bottom=6)))
    assert thin.residual == CropMargins(top=6, bottom=6)

    conservative = validate_operator_crop(
        CropMargins(left=214, right=218),
        _evidence(CropMargins(left=220, right=220)),
    )
    assert conservative.residual == CropMargins(left=6, right=2)


def test_crop_policy_rejects_substantial_uncropped_black_border() -> None:
    evidence = _evidence(CropMargins(left=220, right=220))
    with pytest.raises(CropPolicyError, match="stable 220px black border") as error:
        validate_operator_crop(None, evidence)
    assert error.value.code == "under_crop"

    with pytest.raises(CropPolicyError, match="stable 12px black border"):
        validate_operator_crop(CropMargins(left=208, right=220), evidence)


def test_crop_policy_rejects_material_overcrop() -> None:
    evidence = _evidence(CropMargins(left=220, right=220))
    with pytest.raises(CropPolicyError, match="exceeds the release-safe") as error:
        validate_operator_crop(CropMargins(left=224, right=220), evidence)
    assert error.value.code == "over_crop"

    # A two-pixel operator safety trim is intentionally accepted.
    decision = validate_operator_crop(
        CropMargins(left=222, right=220), evidence
    )
    assert decision.status == "accepted"


def _timed_line(t: float, width: int, height: int, x: int, y: int) -> str:
    return (
        f"[Parsed_cropdetect_0 @ 0x55d0] x1:{x} x2:{x + width - 1} y1:{y} "
        f"y2:{y + height - 1} w:{width} h:{height} x:{x} y:{y} "
        f"pts:{int(t * 1000)} t:{t:.6f} limit:0.094118 crop={width}:{height}:{x}:{y}"
    )


def _running_envelope_log(events: list[tuple[float, int, int, int, int]], end: float) -> str:
    """Cumulative cropdetect (reset=0) log: one line per second."""

    lines: list[str] = []
    current = events[0]
    pending = list(events[1:])
    for second in range(int(end)):
        if pending and second >= pending[0][0]:
            current = pending.pop(0)
        lines.append(_timed_line(second, *current[1:]))
    return "\n".join(lines)


def test_aspect_profile_reports_imax_style_expansion() -> None:
    from bdencode.qc.crop import parse_aspect_profile

    log = _running_envelope_log(
        [
            (0, 1920, 500, 0, 290),  # dark opening
            (40, 1920, 800, 0, 140),  # scope picture
            (3600, 1920, 1080, 0, 0),  # first expanded (IMAX) scene
        ],
        7200,
    )
    profile = parse_aspect_profile(
        log, source_width=1920, source_height=1080, duration_seconds=7200
    )
    assert profile is not None and profile.variable
    assert (profile.baseline_width, profile.baseline_height) == (1920, 800)
    assert (profile.widest_width, profile.widest_height) == (1920, 1080)
    assert profile.timing == "exact"
    assert [item.time_seconds for item in profile.expansions] == [3600.0]
    assert profile.expansions[0].aspect_ratio == pytest.approx(1.7778)
    assert "variable aspect ratio" in profile.summary()
    assert profile.to_dict()["widest"]["aspect_ratio"] == pytest.approx(1.7778)


def test_aspect_profile_ignores_the_opening_minutes_and_constant_titles() -> None:
    from bdencode.qc.crop import parse_aspect_profile

    constant = _running_envelope_log(
        [(0, 1920, 400, 0, 340), (20, 1920, 800, 0, 140)], 7200
    )
    profile = parse_aspect_profile(
        constant, source_width=1920, source_height=1080, duration_seconds=7200
    )
    assert profile is not None and not profile.variable and not profile.expansions
    assert (profile.baseline_width, profile.baseline_height) == (1920, 800)
    assert profile.summary() == "constant aspect ratio"


def test_aspect_profile_ignores_edge_noise_below_the_change_threshold() -> None:
    from bdencode.qc.crop import parse_aspect_profile

    log = _running_envelope_log(
        [(0, 1920, 800, 0, 140), (4000, 1920, 806, 0, 137)], 7200
    )
    profile = parse_aspect_profile(
        log, source_width=1920, source_height=1080, duration_seconds=7200
    )
    assert profile is not None and not profile.variable


def test_aspect_profile_without_timestamps_is_marked_approximate() -> None:
    from bdencode.qc.crop import parse_aspect_profile

    lines = ["crop=1920:800:0:140"] * 60 + ["crop=1920:1080:0:0"] * 40
    profile = parse_aspect_profile(
        "\n".join(lines),
        source_width=1920,
        source_height=1080,
        duration_seconds=6000,
    )
    assert profile is not None and profile.variable
    assert profile.timing == "approximate"
    assert 3000 < profile.expansions[0].time_seconds < 4000


def test_aspect_profile_edge_cases() -> None:
    from bdencode.qc.crop import parse_aspect_profile

    assert (
        parse_aspect_profile(
            "nothing to see", source_width=1920, source_height=1080, duration_seconds=10
        )
        is None
    )
    # An observation larger than the source canvas is not trusted.
    assert (
        parse_aspect_profile(
            "crop=4000:3000:0:0",
            source_width=1920,
            source_height=1080,
            duration_seconds=10,
        )
        is None
    )
    with pytest.raises(ValueError):
        parse_aspect_profile("", source_width=0, source_height=1, duration_seconds=1)
    with pytest.raises(ValueError):
        parse_aspect_profile("", source_width=1, source_height=1, duration_seconds=0)


def test_variable_aspect_evidence_accepts_a_few_step_envelope() -> None:
    from bdencode.qc.crop import variable_aspect_evidence

    log = _running_envelope_log(
        [(0, 1920, 800, 0, 140), (2500, 1920, 1080, 0, 0)], 7200
    )
    with pytest.raises(CropPolicyError) as unstable:
        parse_stable_cropdetect(log, source_width=1920, source_height=1080)
    assert unstable.value.code == "unstable_detection"

    evidence = variable_aspect_evidence(
        log, source_width=1920, source_height=1080, duration_seconds=7200
    )
    assert evidence is not None and evidence.variable_aspect
    assert evidence.crop == evidence.safe_crop == CropMargins(0, 0, 0, 0)
    assert evidence.to_dict()["variable_aspect"] is True
    # The widest canvas removes no picture, so it passes the operator policy.
    decision = validate_operator_crop(automatic_crop(evidence), evidence)
    assert decision.status == "accepted"


def test_variable_aspect_evidence_refuses_noisy_or_flash_only_logs() -> None:
    from bdencode.qc.crop import variable_aspect_evidence

    noisy = _running_envelope_log(
        [(0, 1920, 600, 0, 240)]
        + [(1000 + 900 * step, 1920, 600 + 60 * (step + 1), 0, 240 - 30 * (step + 1)) for step in range(7)],
        7200,
    )
    assert (
        variable_aspect_evidence(
            noisy, source_width=1920, source_height=1080, duration_seconds=7200
        )
        is None
    )

    flash_at_the_very_end = _running_envelope_log(
        [(0, 1920, 800, 0, 140), (7190, 1920, 1080, 0, 0)], 7200
    )
    assert (
        variable_aspect_evidence(
            flash_at_the_very_end,
            source_width=1920,
            source_height=1080,
            duration_seconds=7200,
        )
        is None
    )

    constant = _running_envelope_log([(0, 1920, 800, 0, 140)], 7200)
    assert (
        variable_aspect_evidence(
            constant, source_width=1920, source_height=1080, duration_seconds=7200
        )
        is None
    )


def test_ordinary_evidence_serializes_without_the_variable_flag() -> None:
    assert "variable_aspect" not in _evidence(CropMargins(0, 138, 0, 138)).to_dict()
