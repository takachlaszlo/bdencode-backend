"""Build attachable logs while keeping raw diagnostics as job sidecars."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)\S+"),
    re.compile(r"(?i)([?&](?:key|token)=)[^&\s]+"),
)


def sanitize_text(
    text: str,
    *,
    secret_values: Iterable[str] = (),
    replacements: Mapping[str, str] | None = None,
) -> str:
    value = text.replace("\x00", "")
    for secret in secret_values:
        if secret:
            value = value.replace(secret, "<redacted>")
    for pattern in _SENSITIVE_PATTERNS:
        value = pattern.sub(r"\1<redacted>", value)
    for original, replacement in sorted(
        (replacements or {}).items(), key=lambda item: len(item[0]), reverse=True
    ):
        if original:
            value = value.replace(original, replacement)
    # Preserve tabs/newlines while removing terminal escape and control noise.
    value = re.sub(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", value)
    value = "".join(char for char in value if char in "\n\r\t" or ord(char) >= 32)
    return value


def build_sanitized_log(
    inputs: Sequence[tuple[str, Path]],
    output: Path,
    *,
    secret_values: Iterable[str] = (),
    replacements: Mapping[str, str] | None = None,
) -> Path:
    sections: list[str] = []
    for title, path in inputs:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        sections.append(f"===== {title} =====\n{content.rstrip()}\n")
    sanitized = sanitize_text(
        "\n".join(sections), secret_values=secret_values, replacements=replacements
    )
    output.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    output.write_text(sanitized, encoding="utf-8", newline="\n")
    output.chmod(0o640)
    return output


def assert_secret_absent(path: Path, secret_values: Iterable[str]) -> None:
    content = path.read_text(encoding="utf-8", errors="replace")
    for secret in secret_values:
        if secret and secret in content:
            raise ValueError("sanitized log still contains a supplied secret")
