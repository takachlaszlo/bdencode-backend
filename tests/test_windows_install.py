from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_windows_bootstrap_is_wsl2_scoped_and_non_destructive() -> None:
    script = (ROOT / "install" / "windows.ps1").read_text(encoding="utf-8")

    assert "wsl.exe --install --no-distribution" in script
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
    assert "/bin/sleep infinity" in script
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
    assert "listen 127.0.0.1:@LISTEN_PORT@;" in nginx
    assert "auth_basic" not in nginx
    assert "proxy_pass http://127.0.0.1:@BACKEND_PORT@/api/;" in nginx
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
