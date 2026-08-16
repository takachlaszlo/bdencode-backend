from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
UNINSTALLER = ROOT / "install" / "uninstall.sh"


def script() -> str:
    return UNINSTALLER.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="requires Bash")
def test_uninstaller_has_valid_bash_syntax_and_help() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    subprocess.run([bash, "-n", str(UNINSTALLER)], check=True)
    result = subprocess.run(
        [bash, str(UNINSTALLER), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "--confirm-data-root" in result.stdout
    assert "APT packages" in result.stdout


def test_uninstaller_requires_explicit_purge_confirmation() -> None:
    content = script()
    assert '"$purge_data" -eq 1' in content
    assert '--purge-data requires --confirm-data-root matching' in content
    assert (
        'assert_absolute_without_symlinks "$confirm_data_root"' in content
    )
    assert 'assert_absolute_without_symlinks "$data_root_raw"' in content
    assert 'if [[ "$parent" == / ]]' in content
    assert 'if paths_overlap "$data_root" "$source"' in content
    assert 'if path_is_within "$repo_root" "$data_root"' in content


def test_uninstaller_fails_closed_for_queue_and_transactions() -> None:
    content = script()
    assert "queue-idle" in content
    assert "--allow-review" not in content
    assert "deployment.lock" in content
    assert "flock -n -x 9" in content
    assert 'exec 9>>"$deployment_lock"' in content
    assert '[[ -L "$deployment_lock" ]]' in content
    for marker in (
        "/var/lib/bdencode/install-transactions/active",
        "/var/lib/bdencode/install-transactions/services-pending",
        "/var/lib/bdencode/update-runtime/active.json",
        "/var/lib/bdencode/apt-transactions/active",
    ):
        assert marker in content
    assert "bdencode-update-recover --finalize" in content


def test_uninstaller_preflights_every_recursive_target_before_mutation() -> None:
    content = script()
    mutation = content.index(
        "if sudo systemctl is-active --quiet bdencode-api.service"
    )
    for preflight in (
        'validate_system_tree_exact "$frontend_root"',
        'validate_system_tree_exact "$system_state_root"',
        'validate_owned_tree_exact "$data_root"',
        'validate_owned_tree_exact "$app_root"',
    ):
        assert content.index(preflight) < mutation


def test_uninstaller_validates_database_and_credential_paths() -> None:
    content = script()
    assert 'assert_absolute_without_symlinks "$state_root"' in content
    assert 'assert_absolute_without_symlinks "$database_path_raw"' in content
    assert 'path_is_within "$database_path" "$state_root"' in content
    assert '"$database_path-wal" "$database_path-shm"' in content
    assert 'assert_absolute_without_symlinks "$credential_directory"' in content


def test_uninstaller_preserves_or_purges_only_fixed_credentials() -> None:
    content = script()
    assert "--purge-credentials" in content
    assert "Remove all seven fixed BDEncode credentials" in content
    assert "qBittorrent login, Aither and OpenAI" in content
    assert "Remove the three fixed image-host credentials" not in content
    assert "--purge-credential)" in content
    assert "Legacy option: remove only the ImgBB credential" in content
    for credential in (
        "imgbb-api-key",
        "catbox-userhash",
        "freeimage-api-key",
        "qbittorrent-username",
        "qbittorrent-password",
        "tracker-aither-api-token",
        "openai-api-key",
    ):
        assert credential in content
    assert 'credential_purge_mode=imgbb' in content
    assert 'credential_purge_mode=all' in content
    assert 'os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW' in content
    assert 'os.unlink(name, dir_fd=descriptor)' in content
    assert 'sudo rm -f -- "$credential_path"' not in content
    assert 'rm -f -- "$credential_directory"' not in content
    preflight = content.index("Credential target is unexpectedly a directory")
    mutation = content.index(
        "if sudo systemctl is-active --quiet bdencode-api.service"
    )
    assert preflight < mutation


def test_nginx_removal_has_verified_rollback() -> None:
    content = script()
    assert "nginx_binary=/usr/sbin/nginx" in content
    copied = content.index('sudo cp -aT -- "$nginx_target"')
    armed = content.index('nginx_backup="$nginx_backup_candidate"')
    removed = content.index('sudo rm -f -- "$nginx_target"')
    verified = content.index('sudo "$nginx_binary" -t', removed)
    assert copied < armed < removed < verified


def test_uninstaller_covers_all_installer_host_artifacts() -> None:
    content = script()
    for target in (
        "/var/www/bdencode",
        "/var/lib/bdencode",
        "/etc/nginx/apps/bdencode.conf",
        "/etc/apt/preferences.d/bdencode-media",
        "/usr/local/libexec/bdencode-install-transaction",
        "/usr/local/libexec/bdencode-apt-transaction",
        "/usr/local/libexec/bdencode-apt-guard",
        "/usr/local/libexec/bdencode-update-runtime",
        "/usr/local/libexec/bdencode-update-recover",
        "/usr/local/libexec/bdencode-recovery-check",
        "/etc/systemd/system/bdencode-update-recovery.service",
        "/etc/systemd/system/bdencode-install-recovery.service",
        "/etc/systemd/system/bdencode-install-recovery.path",
        "/etc/systemd/system/multi-user.target.wants/bdencode-update-recovery.service",
        "/etc/systemd/system/multi-user.target.wants/bdencode-install-recovery.path",
    ):
        assert target in content


def test_source_roots_are_validation_only() -> None:
    content = script()
    mutating_lines = [
        line
        for line in content.splitlines()
        if any(command in line for command in ("rm ", "rmdir ", "mv ", "chown ", "chmod "))
    ]
    assert all("source" not in line.lower() for line in mutating_lines)
