from pathlib import Path

from bdencode.logs import assert_secret_absent, build_sanitized_log, sanitize_text
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


def test_build_attachable_log_has_sections_without_secret(tmp_path: Path) -> None:
    raw = tmp_path / "raw.log"
    raw.write_text("command --token top-secret\n", encoding="utf-8")
    target = build_sanitized_log(
        [("raw encoder", raw)], tmp_path / "encode.log", secret_values=["top-secret"]
    )
    assert "raw encoder" in target.read_text(encoding="utf-8")
    assert_secret_absent(target, ["top-secret"])
