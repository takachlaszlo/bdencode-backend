"""Build attachable logs while keeping raw diagnostics as job sidecars."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|password|secret|userhash|credential)\s*[:=]\s*)\S+"
    ),
    re.compile(r"(?i)([?&](?:key|token|userhash)=)[^&\s]+"),
)

_UUID_PATTERN = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_PUBLIC_FIELD_PATTERNS = (
    re.compile(
        r"(?im)^(\s*(?:(?:bdencode[_-]?)?settings(?:[_-]?json)?|"
        r"encoder[_-]?settings)\s*[:=]\s*).*$"
    ),
    re.compile(
        r"(?i)((?:job[_-]?id|job[_-]?uuid|username|user_name)\s*[:=]\s*)[^\s,;]+"
    ),
)
_WINDOWS_USER_HOME = re.compile(
    r"(?i)\b[A-Z]:[\\/]+(?:Users|Documents[ ]and[ ]Settings)[\\/]+[^\\/\s\"']+"
)
_UNIX_USER_HOME = re.compile(r"(?i)(?<![:/\w])/(?:home|Users)/[^/\s\"']+")
_WINDOWS_ROOT = re.compile(r"(?i)\b[A-Z]:[\\/]+")
_UNIX_ROOT = re.compile(
    r"(?<![:/\w])/(?=(?:data|etc|home|media|mnt|opt|root|run|srv|tmp|usr|var|work|workspace)(?:/|\\))"
)
_UNC_ROOT = re.compile(r"(?<![\\])\\\\[^\\\s\"']+[\\/][^\\\s\"']+")


def sanitize_text(
    text: str,
    *,
    secret_values: Iterable[str] = (),
    replacements: Mapping[str, str] | None = None,
    public: bool = True,
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
    if public:
        # Preserve useful basenames/tool output, but make host paths relative
        # and remove stable identifiers which can link a public release back to
        # an operator or internal job database.
        value = _WINDOWS_USER_HOME.sub("<user-home>", value)
        value = _UNIX_USER_HOME.sub("<user-home>", value)
        value = _UNC_ROOT.sub("<network-root>", value)
        value = _WINDOWS_ROOT.sub("<internal-root>/", value)
        value = _UNIX_ROOT.sub("<internal-root>/", value)
        value = _UUID_PATTERN.sub("<uuid>", value)
        for pattern in _PUBLIC_FIELD_PATTERNS:
            value = pattern.sub(r"\1<redacted>", value)
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
    public: bool = True,
) -> Path:
    sections: list[str] = []
    for title, path in inputs:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        sections.append(f"===== {title} =====\n{content.rstrip()}\n")
    sanitized = sanitize_text(
        "\n".join(sections),
        secret_values=secret_values,
        replacements=replacements,
        public=public,
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


def assert_public_metadata_absent(path: Path) -> None:
    """Fail if a supposedly public log still exposes stable host metadata."""

    content = path.read_text(encoding="utf-8", errors="replace")
    if sanitize_text(content, public=True) != content:
        raise ValueError("sanitized log still contains private host/job metadata")
