from pathlib import Path

from bdencode.logs import (
    assert_public_metadata_absent,
    assert_secret_absent,
    build_sanitized_log,
    sanitize_text,
)
from bdencode.process import redact_argv


def test_sanitize_redacts_credentials_and_control_sequences() -> None:
    value = sanitize_text(
        "api_key=abc123\nAuthorization: Bearer xyz\n\x1b[31merror",
        secret_values=["abc123"],
    )
    assert "abc123" not in value and "xyz" not in value
    assert "\x1b" not in value


def test_argv_redaction_handles_single_argument_authorization_and_urls() -> None:
    redacted = redact_argv(
        [
            "curl",
            "Authorization: Bearer topsecret",
            "https://example.invalid/upload?key=abc123&name=proof",
        ]
    )
    joined = " ".join(redacted)
    assert "topsecret" not in joined
    assert "abc123" not in joined
    assert "<redacted>" in joined


def test_userhash_and_credential_markers_are_redacted() -> None:
    value = sanitize_text("userhash=account-value\ncredential: encrypted-value")
    redacted = " ".join(
        redact_argv(
            [
                "--userhash=account-value",
                "https://example.invalid/?userhash=account-value",
            ]
        )
    )
    assert "account-value" not in value
    assert "encrypted-value" not in value
    assert "account-value" not in redacted


def test_build_attachable_log_has_sections_without_secret(tmp_path: Path) -> None:
    raw = tmp_path / "raw.log"
    raw.write_text("command --token top-secret\n", encoding="utf-8")
    target = build_sanitized_log(
        [("raw encoder", raw)], tmp_path / "encode.log", secret_values=["top-secret"]
    )
    assert "raw encoder" in target.read_text(encoding="utf-8")
    assert_secret_absent(target, ["top-secret"])


def test_public_log_redacts_host_paths_job_uuid_and_internal_settings(
    tmp_path: Path,
) -> None:
    job_id = "8be42efd-1c8a-524e-8262-dcd108a93d1e"
    raw = tmp_path / "raw.log"
    raw.write_text(
        "\n".join(
            (
                f"job_id={job_id}",
                'settings={"crf": 19, "preset": "slow"}',
                f"source=/home/fixture-user/jobs/{job_id}/reference.mkv",
                r"output=C:\Users\fixture-user\Videos\Release\encode.mkv",
            )
        ),
        encoding="utf-8",
    )
    target = build_sanitized_log([("raw", raw)], tmp_path / "public.log")
    value = target.read_text(encoding="utf-8")
    assert "fixture-user" not in value
    assert job_id not in value
    assert '"crf"' not in value
    assert "/home/" not in value
    assert r"C:\Users" not in value
    assert "<user-home>" in value
    assert "settings=<redacted>" in value
    assert_public_metadata_absent(target)


def test_raw_diagnostic_sidecar_can_explicitly_keep_private_paths() -> None:
    raw = "/home/operator/jobs/source.m2ts"
    assert sanitize_text(raw, public=False) == raw
