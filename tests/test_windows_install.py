from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_windows_installer_installs_the_newest_release_unless_a_branch_is_given() -> None:
    script = (ROOT / "install" / "windows.ps1").read_text(encoding="utf-8")

    # An explicit -Branch is cloned as given; without it the highest vX.Y.Z tag is, like the daily updater.
    assert '$PSBoundParameters.ContainsKey("Branch")' in script
    assert "--exec /usr/bin/git ls-remote --tags --refs $Repository" in script
    assert "'^v(\\d+)\\.(\\d+)\\.(\\d+)$'" in script
    assert "[version]" in script
    assert '"--branch", $cloneRef,' in script
    assert '"--branch", $Branch,' not in script
    # No release tag reachable: fall back to the branch instead of failing the installation.
    assert "a '$Branch' ág lesz telepítve" in script


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
