from __future__ import annotations

from decimal import Decimal

import pytest

from bdencode.audio import effective_audio_policy
from bdencode.qc.audio import parse_audio_analysis, verify_audio_signal


def _analysis_log(
    *,
    integrated: str = "-18.2",
    loudness_range: str = "7.4",
    true_peak: str = "-0.8",
    sample_peak: str = "-1.0",
    peak_count: str = "2.000000",
    nan_samples: str = "0.000000",
    inf_samples: str = "0.000000",
    denormal_samples: str = "0.000000",
    extra: str = "",
) -> str:
    return f"""
[Parsed_ebur128_0 @ 000001] t: 1.0 TARGET:-23 LUFS M:-70.0 S:-70.0 I:-70.0 LUFS LRA:0.0 LU
[Parsed_astats_1 @ 000002] Channel: 1
[Parsed_astats_1 @ 000002] Peak level dB: -6.0
[Parsed_astats_1 @ 000002] Peak count: 1.000000
[Parsed_astats_1 @ 000002] Overall
[Parsed_astats_1 @ 000002] Peak level dB: {sample_peak}
[Parsed_astats_1 @ 000002] Peak count: {peak_count}
[Parsed_astats_1 @ 000002] Number of NaNs: {nan_samples}
[Parsed_astats_1 @ 000002] Number of Infs: {inf_samples}
[Parsed_astats_1 @ 000002] Number of denormals: {denormal_samples}
[Parsed_ebur128_0 @ 000001] Summary:

[Parsed_ebur128_0 @ 000001]   Integrated loudness:
[Parsed_ebur128_0 @ 000001]     I:          {integrated} LUFS

[Parsed_ebur128_0 @ 000001]   Loudness range:
[Parsed_ebur128_0 @ 000001]     LRA:         {loudness_range} LU

[Parsed_ebur128_0 @ 000001]   True peak:
[Parsed_ebur128_0 @ 000001]     Peak:        {true_peak} dBFS
{extra}
"""


def _policy(action: str = "ac3"):
    return effective_audio_policy(
        action,
        source_codec="truehd",
        source_profile="TrueHD",
        source_channels=8,
        source_sample_rate=48_000,
    )


def test_parser_uses_final_ebur128_summary_and_astats_overall_values() -> None:
    analysis = parse_audio_analysis(_analysis_log().encode())

    assert analysis.integrated_lufs == Decimal("-18.2")
    assert analysis.loudness_range_lu == Decimal("7.4")
    assert analysis.true_peak_dbfs == Decimal("-0.8")
    assert analysis.sample_peak_dbfs == Decimal("-1.0")
    assert analysis.peak_count == 2
    assert analysis.clipped_samples == 0
    assert analysis.clipping_detection == "below_full_scale"
    assert analysis.nan_samples == 0
    assert analysis.inf_samples == 0
    assert analysis.denormal_samples == 0
    assert analysis.complete


def test_parser_derives_clipping_from_full_scale_peak_count() -> None:
    analysis = parse_audio_analysis(
        _analysis_log(sample_peak="-0.000000", peak_count="12.000000")
    )

    assert analysis.sample_peak_dbfs == 0
    assert analysis.clipped_samples == 12
    assert analysis.clipping_detection == "full_scale_peak_count"
    assert analysis.has_clipping


def test_explicit_clipping_counter_takes_precedence() -> None:
    analysis = parse_audio_analysis(
        _analysis_log(
            sample_peak="-0.2",
            peak_count="2",
            extra="[Parsed_astats_1] Number of clipped samples: 4.0",
        )
    )

    assert analysis.clipped_samples == 4
    assert analysis.clipping_detection == "explicit_counter"


def test_silent_negative_infinity_is_a_valid_complete_analysis() -> None:
    analysis = parse_audio_analysis(
        _analysis_log(
            integrated="-inf",
            loudness_range="0.0",
            true_peak="-inf",
            sample_peak="-inf",
            peak_count="0",
        )
    )

    assert analysis.complete
    assert analysis.invalid_metric_fields == ()
    assert analysis.clipped_samples == 0


def test_missing_analysis_fields_fail_closed() -> None:
    missing = parse_audio_analysis("ffmpeg exited before filter summaries")
    complete = parse_audio_analysis(_analysis_log())

    result = verify_audio_signal(missing, complete, _policy())

    assert not result.passed
    assert not result.source_complete
    assert any(
        "source audio analysis is incomplete" in item for item in result.failures
    )


def test_non_finite_and_clipped_samples_are_rejected_and_denormals_reported() -> None:
    source = parse_audio_analysis(_analysis_log(denormal_samples="3"))
    encode = parse_audio_analysis(
        _analysis_log(
            sample_peak="0.0",
            peak_count="8",
            nan_samples="1",
            inf_samples="2",
        )
    )

    result = verify_audio_signal(source, encode, _policy("flac"))

    assert not result.passed
    assert any("non-finite samples" in item for item in result.failures)
    assert any("8 clipped/full-scale samples" in item for item in result.failures)
    assert any("3 denormal samples" in item for item in result.warnings)


def test_lossy_transcode_rejects_true_peak_overshoot_without_pcm_clipping() -> None:
    source = parse_audio_analysis(_analysis_log(true_peak="-0.5"))
    encode = parse_audio_analysis(_analysis_log(true_peak="0.2", sample_peak="-0.1"))

    result = verify_audio_signal(source, encode, _policy("eac3"))

    assert not result.passed
    assert result.lossy_transcode
    assert result.true_peak_increase_db == Decimal("0.7")
    assert any("above 0 dBTP" in item for item in result.failures)


@pytest.mark.parametrize(
    ("encode_peak", "passed"),
    (("-0.5", True), ("-0.4", False)),
)
def test_lossy_near_ceiling_true_peak_increase_has_point_three_db_limit(
    encode_peak: str, passed: bool
) -> None:
    source = parse_audio_analysis(_analysis_log(true_peak="-0.8"))
    encode = parse_audio_analysis(
        _analysis_log(true_peak=encode_peak, sample_peak="-0.9")
    )

    result = verify_audio_signal(source, encode, _policy())

    assert result.passed is passed
    if not passed:
        assert any(
            "increased near-ceiling true peak" in item for item in result.failures
        )


def test_lossless_and_core_extraction_report_intersample_peak_without_false_failure() -> (
    None
):
    source = parse_audio_analysis(_analysis_log(true_peak="-0.1"))
    encode = parse_audio_analysis(_analysis_log(true_peak="0.2", sample_peak="-0.05"))
    flac = verify_audio_signal(source, encode, _policy("flac"))
    dts_core_policy = effective_audio_policy(
        "dts",
        source_codec="dts",
        source_profile="DTS-HD MA",
        source_channels=8,
        source_sample_rate=48_000,
    )
    dts_core = verify_audio_signal(source, encode, dts_core_policy)

    assert flac.passed and not flac.lossy_transcode
    assert dts_core.passed and not dts_core.lossy_transcode
    assert dts_core.strategy == "dts_core_extract"
    assert any("above 0 dBTP" in item for item in flac.warnings)
    assert any("above 0 dBTP" in item for item in dts_core.warnings)


def test_parser_rejects_non_finite_counter_values() -> None:
    with pytest.raises(ValueError, match="finite non-negative"):
        parse_audio_analysis(_analysis_log(nan_samples="nan"))


def test_analysis_and_verification_are_manifest_serializable() -> None:
    source = parse_audio_analysis(_analysis_log(true_peak="-0.8"))
    encode = parse_audio_analysis(_analysis_log(true_peak="-0.6"))
    result = verify_audio_signal(source, encode, _policy())

    assert source.to_dict()["true_peak_dbfs"] == "-0.8"
    assert source.to_dict()["complete"] is True
    assert result.to_dict()["true_peak_increase_db"] == "0.2"
    assert result.to_dict()["failures"] == []
