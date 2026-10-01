"""Executes the argument-handling paths of install/windows.ps1 for real.

The installer's relaunch logic (UAC elevation, reboot continuation through
RunOnce, the reserved API port) cannot run in CI because it would install WSL.
Instead ``tests/powershell/build_windows_harness.ps1`` cuts those exact
statements out of the real script with the PowerShell parser and replaces only
the system-touching commands (``Start-Process``, ``New-Item``,
``New-ItemProperty``) by recorders.  Every test runs under each PowerShell that
is installed: PowerShell 7 (``pwsh``) and, on Windows, the Windows PowerShell
5.1 that the installer actually targets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
WINDOWS_SCRIPT = ROOT / "install" / "windows.ps1"
BUILDER = ROOT / "tests" / "powershell" / "build_windows_harness.ps1"


def parse_windows_command_line(line: str) -> list[str]:
    """Split a command line with the CommandLineToArgvW backslash/quote rules."""

    arguments: list[str] = []
    index, length = 0, len(line)
    while index < length:
        while index < length and line[index] in " \t":
            index += 1
        if index >= length:
            break
        current: list[str] = []
        quoted = False
        while index < length and (quoted or line[index] not in " \t"):
            char = line[index]
            if char == "\\":
                end = index
                while end < length and line[end] == "\\":
                    end += 1
                count = end - index
                if end < length and line[end] == '"':
                    current.append("\\" * (count // 2))
                    if count % 2:
                        current.append('"')
                        index = end + 1
                    else:
                        index = end
                else:
                    current.append("\\" * count)
                    index = end
            elif char == '"':
                quoted = not quoted
                index += 1
            else:
                current.append(char)
                index += 1
        arguments.append("".join(current))
    return arguments


def _installed_shells() -> list[str]:
    return [name for name in ("pwsh", "powershell") if shutil.which(name)]


@pytest.fixture(scope="module", params=_installed_shells())
def shell(request: pytest.FixtureRequest) -> str:
    return request.param


def _environment(result: Path | None = None, **extra: str) -> dict[str, str]:
    environment = {**os.environ, "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"}
    # The installer's -WslLocation default is Join-Path $env:LOCALAPPDATA ...,
    # which is evaluated during parameter binding.  Windows always has the
    # variable; PowerShell 7 on Linux does not, and Join-Path rejects $null.
    environment.setdefault(
        "LOCALAPPDATA", os.path.join(tempfile.gettempdir(), "bdencode-localappdata")
    )
    if result is not None:
        environment["BDENCODE_TEST_RESULT"] = str(result)
    environment.update(extra)
    return environment


def _command(shell: str, script: Path, *arguments: str) -> list[str]:
    command = [shell, "-NoProfile", "-NonInteractive"]
    if os.name == "nt":
        command += ["-ExecutionPolicy", "Bypass"]
    return [*command, "-File", str(script), *arguments]


def _run(
    shell: str,
    script: Path,
    *arguments: str,
    result: Path | None = None,
    **extra: str,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, Any] | None]:
    if result is not None:
        result.unlink(missing_ok=True)
    completed = subprocess.run(
        _command(shell, script, *arguments),
        capture_output=True,
        env=_environment(result, **extra),
        timeout=180,
        check=False,
    )
    return completed, _read_result(result)


def _read_result(result: Path | None) -> dict[str, Any] | None:
    if result is None or not result.is_file():
        return None
    return json.loads(result.read_text(encoding="utf-8-sig"))


def _text(completed: subprocess.CompletedProcess[bytes]) -> str:
    return (completed.stdout + completed.stderr).decode("utf-8", errors="replace")


def _build(shell: str, mode: str, directory: Path) -> Path:
    output = directory / f"synthetic-{mode.lower()}.ps1"
    completed, _ = _run(
        shell,
        BUILDER,
        "-Source",
        str(WINDOWS_SCRIPT),
        "-Out",
        str(output),
        "-Mode",
        mode,
    )
    assert completed.returncode == 0, _text(completed)
    assert output.is_file()
    return output


@pytest.fixture(scope="module")
def _shell_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("windows-harness")


@pytest.fixture(scope="module")
def _built_scripts(shell: str, _shell_directory: Path) -> dict[str, Path]:
    """Cut the harness scripts out of windows.ps1 once per shell."""

    directory = _shell_directory / shell
    directory.mkdir(exist_ok=True)
    return {
        mode: _build(shell, mode, directory) for mode in ("Elevation", "RunOnce")
    }


@pytest.fixture
def elevation_script(_built_scripts: dict[str, Path]) -> Path:
    return _built_scripts["Elevation"]


@pytest.fixture
def runonce_script(_built_scripts: dict[str, Path]) -> Path:
    return _built_scripts["RunOnce"]


AWKWARD_VALUES = [
    "9000",
    "D:\\Filmek",
    "D:\\Filmek\\",
    "D:\\Mozi filmek\\",
    "D:\\Mozi filmek\\Új mappa",
    "\u00fc\u00f1\u00ed \u0151",
    "trailing\\",
    'a "quoted" name',
    'ends with quote"',
    'back\\\\"slash',
]


def _expected_tokens(values: dict[str, object]) -> list[str]:
    tokens: list[str] = []
    for name, value in values.items():
        if isinstance(value, bool):
            if value:
                tokens.append(f"-{name}")
        else:
            tokens.extend((f"-{name}", str(value)))
    return tokens


def _forwarded_part(argument_list: str, script: Path) -> str:
    prefix = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
    assert argument_list.startswith(prefix), argument_list
    return argument_list[len(prefix) :].strip()


def _tokens_to_mapping(tokens: list[str]) -> dict[str, object]:
    """Read ``-Name value`` / ``-Switch`` tokens back into a dictionary."""

    mapping: dict[str, object] = {}
    index = 0
    while index < len(tokens):
        name = tokens[index]
        assert name.startswith("-"), tokens
        if index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            mapping[name[1:]] = tokens[index + 1]
            index += 2
        else:
            mapping[name[1:]] = True
            index += 1
    return mapping


# -- the real installer must parse under every supported shell --------------------


def test_installer_parses_without_errors_in_this_shell(shell: str, tmp_path: Path) -> None:
    output = tmp_path / "parsed.txt"
    completed, _ = _run(
        shell,
        BUILDER,
        "-Source",
        str(WINDOWS_SCRIPT),
        "-Out",
        str(output),
        "-Mode",
        "ParseOnly",
    )
    assert completed.returncode == 0, _text(completed)
    assert output.read_text(encoding="utf-8") == "ok"


def test_installer_keeps_a_utf8_bom_for_windows_powershell() -> None:
    # Windows PowerShell 5.1 reads a BOM-less file as ANSI and mangles the
    # Hungarian messages; the BOM is what makes the file portable.
    assert WINDOWS_SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


# -- elevation relaunch ---------------------------------------------------------------


@pytest.mark.parametrize("value", AWKWARD_VALUES)
def test_relaunch_forwards_every_bound_parameter_exactly(
    shell: str, elevation_script: Path, tmp_path: Path, value: str
) -> None:
    result = tmp_path / "result.json"
    arguments = ["-SourcePath", value, "-Port", "9000", "-AllowExistingDistro"]

    completed, record = _run(shell, elevation_script, *arguments, result=result)

    assert completed.returncode == 7, _text(completed)
    assert record is not None and "fell_through" not in record
    assert record["file_path"] == "powershell.exe" and record["verb"] == "RunAs"
    assert record["pass_thru"] is True and record["wait"] is True
    assert record["bound"]["SourcePath"] == value
    assert record["bound"]["Port"] == 9000
    assert record["bound"]["AllowExistingDistro"] is True

    forwarded = _forwarded_part(record["argument_list"], elevation_script)
    tokens = parse_windows_command_line(forwarded)
    assert _tokens_to_mapping(tokens) == {
        "SourcePath": value,
        "Port": "9000",
        "AllowExistingDistro": True,
    }


@pytest.mark.parametrize("value", AWKWARD_VALUES)
def test_forwarded_line_rebinds_to_the_same_parameters_in_the_elevated_process(
    shell: str, elevation_script: Path, tmp_path: Path, value: str
) -> None:
    result = tmp_path / "result.json"
    first, record = _run(
        shell,
        elevation_script,
        "-SourcePath",
        value,
        "-DistroName",
        "Debian Test",
        "-AllowExistingDistro",
        result=result,
    )
    assert first.returncode == 7, _text(first)
    assert record is not None
    forwarded = _forwarded_part(record["argument_list"], elevation_script)

    if os.name == "nt":
        # Hand the exact string to CreateProcess, as Start-Process does, so the
        # shell's own command-line parser decides how to split it.
        executable = shutil.which(shell)
        assert executable is not None
        line = (
            f'"{executable}" -NoProfile -NonInteractive -ExecutionPolicy Bypass '
            f'-File "{elevation_script}" {forwarded}'
        )
        result.unlink(missing_ok=True)
        second = subprocess.run(
            line, capture_output=True, env=_environment(result), timeout=180, check=False
        )
    else:
        second, _ = _run(
            shell,
            elevation_script,
            *parse_windows_command_line(forwarded),
            result=result,
        )
    assert second.returncode == 7, _text(second)
    relaunched = _read_result(result)
    assert relaunched is not None
    assert relaunched["bound"] == record["bound"]
    assert relaunched["bound"]["SourcePath"] == value


def test_no_parameters_forward_an_empty_argument_list(
    shell: str, elevation_script: Path, tmp_path: Path
) -> None:
    completed, record = _run(
        shell, elevation_script, result=tmp_path / "result.json"
    )
    assert completed.returncode == 7, _text(completed)
    assert record is not None and record["bound"] == {}
    assert _forwarded_part(record["argument_list"], elevation_script) == ""


def test_declined_elevation_is_reported_and_exits_with_one(
    shell: str, elevation_script: Path, tmp_path: Path
) -> None:
    completed, record = _run(
        shell,
        elevation_script,
        "-Port",
        "9000",
        result=tmp_path / "result.json",
        BDENCODE_TEST_ELEVATION_REFUSED="1",
    )
    assert completed.returncode == 1, _text(completed)
    assert record is not None and record["verb"] == "RunAs"
    assert "rendszergazdai" in _text(completed)


# -- the reserved API port and parameter validation -----------------------------------------


def test_the_internal_api_port_is_refused_before_anything_else_happens(
    shell: str, elevation_script: Path, tmp_path: Path
) -> None:
    result = tmp_path / "result.json"
    completed, record = _run(shell, elevation_script, "-Port", "8796", result=result)
    assert completed.returncode != 0
    assert "8796" in _text(completed)
    assert record is None, "the relaunch must not happen for a refused port"


@pytest.mark.parametrize("port", ["0", "80", "1023", "65536", "abc", "-5"])
def test_out_of_range_ports_are_rejected_by_parameter_validation(
    shell: str, elevation_script: Path, tmp_path: Path, port: str
) -> None:
    completed, record = _run(
        shell, elevation_script, "-Port", port, result=tmp_path / "result.json"
    )
    assert completed.returncode != 0, _text(completed)
    assert record is None


@pytest.mark.parametrize("port", ["1024", "8787", "8795", "8797", "65535"])
def test_valid_ports_reach_the_relaunch(
    shell: str, elevation_script: Path, tmp_path: Path, port: str
) -> None:
    completed, record = _run(
        shell, elevation_script, "-Port", port, result=tmp_path / "result.json"
    )
    assert completed.returncode == 7, _text(completed)
    assert record is not None and record["bound"]["Port"] == int(port)


# -- continuation after the reboot that WSL installation requires -----------------------------


@pytest.mark.parametrize("value", AWKWARD_VALUES)
def test_reboot_continuation_reuses_the_original_arguments(
    shell: str, runonce_script: Path, tmp_path: Path, value: str
) -> None:
    completed, record = _run(
        shell,
        runonce_script,
        "-SourcePath",
        value,
        "-Port",
        "9000",
        "-AllowExistingDistro",
        result=tmp_path / "result.json",
    )
    assert completed.returncode == 0, _text(completed)
    assert record is not None
    created, written = record["calls"]
    assert created["command"] == "New-Item"
    assert created["path"] == r"HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce"
    assert written["command"] == "New-ItemProperty"
    assert written["name"] == "BDEncodeInstall" and written["type"] == "String"

    tokens = parse_windows_command_line(written["value"])
    assert tokens[:6] == [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(runonce_script),
    ]
    assert _tokens_to_mapping(tokens[6:]) == {
        "SourcePath": value,
        "Port": "9000",
        "AllowExistingDistro": True,
    }


def test_reboot_continuation_without_parameters_registers_only_the_script(
    shell: str, runonce_script: Path, tmp_path: Path
) -> None:
    completed, record = _run(
        shell, runonce_script, result=tmp_path / "result.json"
    )
    assert completed.returncode == 0, _text(completed)
    assert record is not None
    value = record["calls"][1]["value"]
    assert parse_windows_command_line(value)[-1] == str(runonce_script)
