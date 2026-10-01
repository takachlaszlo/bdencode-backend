from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def test_windows_bootstrap_is_wsl2_scoped_and_non_destructive() -> None:
    script = (ROOT / "install" / "windows.ps1").read_text(encoding="utf-8")

    assert "wsl.exe --install --no-distribution" in script
    assert '[string]$Branch = "main"' in script
    assert '[string]$Branch = "codex/frontend"' not in script
    assert "--set-default-version 2" in script
    assert "Get-DistroVersion" in script
    assert "Wait-DistroVersion -ExpectedVersion 2" in script
    assert "systemd=true" in script
    assert "/etc/bdencode/windows-managed" in script
    assert "AllowExistingDistro" in script
    assert "wsl.exe --unregister" not in script
    assert 'Join-Path $WslLocation "ext4.vhdx"' in script
    assert "félbeszakadt BDEncode Debian telepítés folytatása" in script
    assert "BDENCODE_SOURCE_ROOT=$wslSource" in script
    assert "BDENCODE_CPU_PERCENT=80" in script
    assert '$Script -replace "`r`n", "`n"' in script
    assert "'--exec', '/bin/sleep', 'infinity'" in script
    assert "Register-ScheduledTask" in script
    assert "BDEncode WSL" in script
    assert 'Join-Path $logDirectory "keepalive.ps1"' in script
    assert "-WindowStyle Hidden -File" in script
    assert "HttpClientHandler" in script
    assert "UseProxy = $false" in script
    assert "http://127.0.0.1:$Port/encoder/api/v1/health" in script
    assert "http://localhost:$Port/encoder/" in script
    assert "curl python3" in script


def test_windows_bootstrap_keeps_work_in_linux_and_exposes_completed_folder() -> None:
    script = (ROOT / "install" / "windows.ps1").read_text(encoding="utf-8")

    assert '"/mnt/$drive/$relative"' in script
    assert "mount -t drvfs" in script
    assert "\\\\wsl.localhost\\{0}\\home\\{1}\\encode\\completed" in script
    assert "FolderBrowserDialog" in script
    assert "Jelenleg csak meghajtóbetűjeles" in script


def test_wsl_installer_uses_local_only_standalone_web_server() -> None:
    installer = (ROOT / "install" / "wsl-install.sh").read_text(encoding="utf-8")
    nginx = (
        ROOT / "deploy" / "nginx" / "bdencode-standalone.conf.in"
    ).read_text(encoding="utf-8")

    assert "grep -qi microsoft /proc/sys/kernel/osrelease" in installer
    assert "nginx curl ca-certificates python3" in installer
    assert 'bash "$repo_root/install/install.sh"' in installer
    assert "/etc/nginx/conf.d/bdencode-wsl.conf" in installer
    assert "listen 127.0.0.1:@LISTEN_PORT@ default_server;" in nginx
    assert "server_name _;" in nginx
    assert "return 444;" in nginx
    assert "server_name localhost 127.0.0.1;" in nginx
    assert "auth_basic" not in nginx
    assert "proxy_pass http://127.0.0.1:@BACKEND_PORT@/api/;" in nginx
    assert "proxy_set_header X-Remote-User local-wsl-operator;" in nginx
    assert "try_files $uri $uri/ /encoder/index.html =404;" in nginx


def test_media_sources_render_for_supported_debian_releases() -> None:
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    sources = (ROOT / "install" / "media-apt.sources.list").read_text(
        encoding="utf-8"
    )
    api_unit = (
        ROOT / "deploy" / "systemd" / "bdencode-api.service.in"
    ).read_text(encoding="utf-8")
    worker_unit = (
        ROOT / "deploy" / "systemd" / "bdencode-worker.service.in"
    ).read_text(encoding="utf-8")

    assert 'bookworm|trixie) media_suite="$VERSION_CODENAME"' in installer
    assert 'sed "s|@SUITE@|$media_suite|g"' in installer
    assert "@SUITE@-security" in sources
    assert 'ReadOnlyPaths="@SOURCE_ROOT@"' in api_unit
    assert 'ReadOnlyPaths="@SOURCE_ROOT@"' in worker_unit
    assert "@DATA_ROOT@/release-kits" in api_unit
    assert "@DATA_ROOT@/release-kits" in worker_unit
    assert '"$data_root/release-kits"' in installer
    assert "dpkg-repack man-db mediainfo" in installer


def test_windows_bootstrap_forwards_explicit_parameters_when_relaunching() -> None:
    script = (ROOT / "install" / "windows.ps1").read_text(encoding="utf-8")

    # Functions get their own $PSBoundParameters, so the script-level value is
    # captured once and reused by the elevation and RunOnce continuation paths.
    assert "$script:InstallArguments = $PSBoundParameters" in script
    assert script.count("Get-ForwardedArgumentLine") == 3
    assert '$argumentLine = "$argumentLine $forwarded"' in script
    assert '$command = "$command $forwarded"' in script
    assert "if ($Port -eq 8796)" in script
    assert "/etc/wsl.conf.bdencode-backup" in script


def _parse_windows_command_line(line: str) -> list[str]:
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


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is not installed")
def test_forwarded_argument_line_round_trips_awkward_values(tmp_path: Path) -> None:
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        """
param([string]$Script, [string]$CasesJson)
$parseTokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$parseTokens, [ref]$errors)
if ($errors.Count) { exit 2 }
$wanted = 'ConvertTo-CommandLineArgument', 'Get-ForwardedArgumentLine'
$definitions = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $wanted }, $true)
foreach ($definition in $definitions) { . ([scriptblock]::Create($definition.Extent.Text)) }
$cases = Get-Content -Raw -Encoding UTF8 $CasesJson | ConvertFrom-Json
$lines = foreach ($case in $cases) {
    $script:InstallArguments = [ordered]@{}
    foreach ($property in $case.PSObject.Properties) {
        if ($property.Value -is [bool]) {
            $script:InstallArguments[$property.Name] = [System.Management.Automation.SwitchParameter]::new($property.Value)
        } else {
            $script:InstallArguments[$property.Name] = $property.Value
        }
    }
    , (Get-ForwardedArgumentLine)
}
ConvertTo-Json -Compress -InputObject @($lines)
""",
        encoding="utf-8",
    )
    values = [
        "9000",
        "D:\\Filmek",
        "D:\\Filmek\\",
        "D:\\Mozi filmek\\",
        'a "quoted" name',
        'ends with quote"',
        'back\\\\"slash',
        "trailing\\",
        "\u00fc\u00f1\u00ed \u0151",
        "",
    ]
    cases: list[dict[str, object]] = [
        {"SourcePath": value, "AllowExistingDistro": True, "Verbose": False}
        for value in values
    ]
    cases += [{}, {"Port": 9000, "DistroName": "Debian"}]
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")

    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(harness),
            "-Script",
            str(ROOT / "install" / "windows.ps1"),
            "-CasesJson",
            str(cases_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1"},
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = json.loads(completed.stdout)
    assert len(lines) == len(cases)
    for case, line in zip(cases, lines, strict=True):
        expected: list[str] = []
        for name, value in case.items():
            if isinstance(value, bool):
                if value:
                    expected.append(f"-{name}")
            else:
                expected.extend((f"-{name}", str(value)))
        assert _parse_windows_command_line(line) == expected
