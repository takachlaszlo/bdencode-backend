from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "install" / "release_update.py"
SPEC = importlib.util.spec_from_file_location("bdencode_release_update", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
release_update = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release_update
SPEC.loader.exec_module(release_update)

CommandResult = release_update.CommandResult
posix_only = pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell")
REPOSITORY = "https://example.invalid/bdencode.git"
RELEASE_ID = "20261002T000500Z-4242"
SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = "2026-10-02T00:05:30Z"


class FakeRunner:
    """Stands in for SystemRunner with scripted answers and a real checkout directory."""

    def __init__(
        self,
        *,
        installed: str = "2.1.0",
        tags: tuple[str, ...] = ("v2.0.0", "v2.1.0", "v2.2.0"),
        sudo_exit: int = 0,
        queue_exit: int = 0,
        installer_exit: int = 0,
        installer_installs: bool = True,
        declared: str | None = None,
        clone_exit: int = 0,
        ls_remote_exit: int = 0,
        files: tuple[str, ...] = ("install/install.sh", "install/wsl-install.sh", "frontend/dist/index.html"),
        safe_pause_flag: bool = True,
        installer_timeout: bool = False,
        apt_output: str = "",
    ) -> None:
        self.apt_output = apt_output
        self.installed = installed
        self.tags = tags
        self.sudo_exit = sudo_exit
        self.queue_exit = queue_exit
        self.installer_exit = installer_exit
        self.installer_installs = installer_installs
        self.declared = declared
        self.clone_exit = clone_exit
        self.ls_remote_exit = ls_remote_exit
        self.files = files
        self.safe_pause_flag = safe_pause_flag
        self.installer_timeout = installer_timeout
        self.calls: list[dict[str, Any]] = []

    def commands(self, head: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["argv"][0] == head]

    def run(
        self,
        argv: Any,
        *,
        user: str | None = None,
        env: Any = None,
        timeout: float | None = None,
        grace: float | None = None,
        cwd: Path | None = None,
        log_path: Path | None = None,
    ) -> Any:
        command = [str(item) for item in argv]
        self.calls.append(
            {"argv": command, "user": user, "env": dict(env or {}), "cwd": cwd, "log_path": log_path}
        )
        head = command[0]
        if head == "git" and "ls-remote" in command:
            text = "".join(f"{SHA}\trefs/tags/{tag}\n" for tag in self.tags)
            return CommandResult(self.ls_remote_exit, text if not self.ls_remote_exit else "fatal: unable\n")
        if head == "git" and "clone" in command:
            if self.clone_exit:
                return CommandResult(self.clone_exit, "fatal: nope\n")
            tag = command[command.index("--branch") + 1]
            checkout = Path(command[-1])
            checkout.mkdir(parents=True)
            declared = self.declared or tag.removeprefix("v")
            (checkout / "pyproject.toml").write_text(
                f'[project]\nname = "bdencode-backend"\nversion = "{declared}"\n', encoding="utf-8"
            )
            for relative in self.files:
                target = checkout / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            return CommandResult(0)
        if head == "git" and "rev-parse" in command:
            return CommandResult(0, SHA + "\n")
        if head == "mkdir":
            Path(command[-1]).mkdir(parents=True, exist_ok=True)
            return CommandResult(0)
        if head == "rm":
            shutil.rmtree(command[-1], ignore_errors=True)
            return CommandResult(0)
        if head == "apt-get":
            return CommandResult(0, self.apt_output)
        if head == "sudo":
            return CommandResult(self.sudo_exit)
        if head == "bash":
            if self.installer_timeout:
                raise release_update.CommandTimeoutError("bash timed out")
            if self.installer_exit == 0 and self.installer_installs:
                declared = Path(command[1]).parents[1] / "pyproject.toml"
                version = declared.read_text(encoding="utf-8").split('version = "')[1].split('"')[0]
                self.installed = version
            return CommandResult(self.installer_exit)
        if Path(head).name == "python":
            return CommandResult(0, f"{self.installed}\n")
        if Path(head).name == "bdencode":
            if "--help" in command:
                flag = "  --allow-install-safe-pause\n" if self.safe_pause_flag else ""
                return CommandResult(0, f"usage: bdencode queue-idle\n{flag}")
            return CommandResult(self.queue_exit)
        raise AssertionError(f"unexpected command: {command}")


def make_updater(
    tmp_path: Path,
    *,
    config: Any = None,
    windows: bool = False,
    check_only: bool = False,
    install_tag: str | None = None,
    posts: list | None = None,
    **runner_options: Any,
) -> tuple[Any, FakeRunner, Any]:
    (tmp_path / "source").mkdir(parents=True, exist_ok=True)
    deployment = release_update.Deployment(
        task_user="taki",
        task_home=tmp_path / "home",
        data_root=tmp_path / "data",
        source_root=tmp_path / "source",
        cpu_percent=80,
        windows_managed=windows,
        windows_port=8790,
        service_config=tmp_path / "config.toml",
    )
    runner = FakeRunner(**runner_options)
    store = release_update.StatusStore(tmp_path / "state" / "status.json")
    report = release_update.Reporter(tmp_path / "state" / "release-update.log")
    updater = release_update.ReleaseUpdater(
        deployment,
        config or release_update.ReleaseConfig(repository=REPOSITORY),
        runner,
        store,
        report,
        check_only=check_only,
        install_tag=install_tag,
        clock=lambda: NOW,
        release_id=RELEASE_ID,
        post=(lambda url, payload: posts.append((url, payload))) if posts is not None else None,
    )
    return updater, runner, store


def scratch(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".cache" / "bdencode-release-update"


# -- pure helpers ---------------------------------------------------------------------------


def test_latest_release_is_compared_numerically_and_only_stable_tags_count() -> None:
    text = "\n".join(
        f"{SHA}\trefs/tags/{name}"
        for name in (
            "v2.9.0", "v2.10.0", "v2.10.0-rc1", "v3.0.0-beta", "2.99.0", "release-5", "v2.10.0.1", "v1.99.99",
        )
    )
    assert release_update.select_latest_release(text) == ("v2.10.0", (2, 10, 0))
    assert release_update.select_latest_release("") is None
    assert release_update.select_latest_release(f"{SHA}\trefs/heads/v9.9.9\n") is None


def test_version_strings_are_parsed_strictly() -> None:
    assert release_update.parse_version("2.2.0\n") == (2, 2, 0)
    assert release_update.parse_version("2.2.0.dev1") == (2, 2, 0)
    assert release_update.format_version((2, 10, 3)) == "2.10.3"
    for bad in ("", "2.2", "v2.2.0", "two.one.zero"):
        with pytest.raises(release_update.ReleaseUpdateError):
            release_update.parse_version(bad)


@pytest.mark.parametrize(
    "repository",
    [
        "https://github.com/takachlaszlo/bdencode-backend.git",
        "https://git.example.org:8443/team/bdencode",
        "file:///srv/mirror/bdencode.git",
    ],
)
def test_repository_accepts_plain_https_and_local_mirrors(repository: str) -> None:
    assert release_update.validate_repository(repository) == repository


@pytest.mark.parametrize(
    "repository",
    [
        "http://github.com/x/y.git",
        "git@github.com:x/y.git",
        "ssh://github.com/x/y.git",
        "https://user:token@github.com/x/y.git",
        "https://github.com/x/y.git?x=1",
        "https://github.com/x/y.git#frag",
        "https://github.com/x y.git",
        "/srv/local/path",
        "",
        42,
    ],
)
def test_repository_rejects_credentials_other_transports_and_odd_characters(repository: object) -> None:
    with pytest.raises(release_update.ReleaseUpdateError):
        release_update.validate_repository(repository)


def test_release_config_defaults_values_and_strictness(tmp_path: Path) -> None:
    missing = release_update.load_release_config(tmp_path / "absent.toml")
    assert missing == release_update.ReleaseConfig(
        repository=release_update.DEFAULT_REPOSITORY, automatic_install=True
    )

    path = tmp_path / "release-update.toml"
    path.write_text(f'repository = "{REPOSITORY}"\nautomatic_install = false\n', encoding="utf-8")
    assert release_update.load_release_config(path) == release_update.ReleaseConfig(REPOSITORY, False)

    for text in (
        'automatic_instal = false\n',
        'automatic_install = "yes"\n',
        'repository = "http://insecure.invalid/x.git"\n',
        "this is not toml\n",
    ):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(release_update.ReleaseUpdateError):
            release_update.load_release_config(path)


@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a non-root POSIX user to own the file",
)
def test_release_config_must_be_root_owned_when_root_reads_it(tmp_path: Path) -> None:
    path = tmp_path / "release-update.toml"
    path.write_text("automatic_install = true\n", encoding="utf-8")
    with pytest.raises(release_update.ReleaseUpdateError, match="owned by root"):
        release_update.load_release_config(path, require_root_owner=True)


def write_service_config(path: Path, data_root: Path, source_root: Path, cpu: int = 80) -> None:
    path.write_text(
        "[bdencode]\n"
        f'data_root = "{data_root.as_posix()}"\n'
        f'source_roots = ["{source_root.as_posix()}"]\n'
        f"cpu_limit_percent = {cpu}\n",
        encoding="utf-8",
    )


def test_deployment_is_read_from_the_installed_configuration(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_service_config(config, tmp_path / "data", tmp_path / "films", cpu=60)
    deployment = release_update.load_deployment(
        task_user="taki",
        task_home=tmp_path / "home",
        data_root=tmp_path / "data",
        service_config=config,
        windows_marker=tmp_path / "no-marker",
        wsl_nginx_config=tmp_path / "no-nginx.conf",
    )
    assert deployment.source_root == tmp_path / "films"
    assert deployment.cpu_percent == 60
    assert deployment.windows_managed is False
    assert deployment.app_root == tmp_path / "data" / "app"
    assert deployment.scratch_root == tmp_path / "home" / ".cache" / "bdencode-release-update"
    # Not below the data root, which the sandboxed worker can write.
    assert tmp_path / "data" not in deployment.scratch_root.parents

    with pytest.raises(release_update.ReleaseUpdateError, match="does not match"):
        release_update.load_deployment(
            task_user="taki",
            task_home=tmp_path / "home",
            data_root=tmp_path / "elsewhere",
            service_config=config,
        )
    config.write_text("[bdencode]\n", encoding="utf-8")
    with pytest.raises(release_update.ReleaseUpdateError, match="cannot read"):
        release_update.load_deployment(
            task_user="taki", task_home=tmp_path, data_root=tmp_path / "data", service_config=config
        )


def test_windows_managed_deployment_reuses_the_configured_web_port(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_service_config(config, tmp_path / "data", tmp_path / "films")
    marker = tmp_path / "windows-managed"
    marker.write_text("", encoding="utf-8")
    nginx = tmp_path / "bdencode-wsl.conf"

    def load() -> Any:
        return release_update.load_deployment(
            task_user="taki",
            task_home=tmp_path,
            data_root=tmp_path / "data",
            service_config=config,
            windows_marker=marker,
            wsl_nginx_config=nginx,
        )

    assert load().windows_port == release_update.DEFAULT_WINDOWS_PORT
    nginx.write_text("server {\n    listen 127.0.0.1:9090 default_server;\n}\n", encoding="utf-8")
    deployment = load()
    assert deployment.windows_managed is True and deployment.windows_port == 9090
    for port in (80, 8796):
        nginx.write_text(f"server {{\n    listen 127.0.0.1:{port};\n}}\n", encoding="utf-8")
        with pytest.raises(release_update.ReleaseUpdateError, match="unusable WSL web port"):
            load()


def test_the_log_keeps_one_previous_generation(tmp_path: Path) -> None:
    log = tmp_path / "release-update.log"
    release_update.rotate_log(log)  # a missing log is fine
    log.write_bytes(b"x" * 100)
    release_update.rotate_log(log, limit=1000)
    assert log.exists() and not (tmp_path / "release-update.log.1").exists()
    log.write_bytes(b"y" * 2000)
    release_update.rotate_log(log, limit=1000)
    assert not log.exists()
    assert (tmp_path / "release-update.log.1").read_bytes() == b"y" * 2000
    log.write_bytes(b"z" * 3000)
    release_update.rotate_log(log, limit=1000)
    assert (tmp_path / "release-update.log.1").read_bytes() == b"z" * 3000


def test_status_store_round_trips_and_ignores_damaged_files(tmp_path: Path) -> None:
    store = release_update.StatusStore(tmp_path / "state" / "status.json")
    assert store.load() == {}
    document = {"schema": release_update.STATUS_SCHEMA, "state": "up_to_date"}
    store.save(document)
    assert store.load() == document
    if os.name == "posix":
        assert (store.path.stat().st_mode & 0o777) == 0o644
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == {}
    store.path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
    assert store.load() == {}


# -- decisions of the updater -------------------------------------------------------------------


def test_nothing_happens_when_the_installed_release_is_the_newest(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, installed="2.2.0")
    assert updater.run() == 0
    document = store.load()
    assert document["state"] == "up_to_date"
    assert document["installed_version"] == "2.2.0" and document["latest_tag"] == "v2.2.0"
    assert document["last_successful_check_at"] == NOW
    assert not runner.commands("bash") and not runner.commands("sudo")
    assert not any("clone" in call["argv"] for call in runner.commands("git"))


def test_a_newer_development_build_is_never_downgraded(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, installed="2.3.0")
    assert updater.run() == 0
    assert store.load()["state"] == "up_to_date"
    assert not runner.commands("bash")


def test_check_only_reports_without_installing(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, check_only=True)
    assert updater.run() == 0
    assert store.load()["state"] == "update_available"
    assert not runner.commands("bash") and not runner.commands("sudo")


def test_automatic_install_can_be_switched_off(tmp_path: Path) -> None:
    config = release_update.ReleaseConfig(repository=REPOSITORY, automatic_install=False)
    updater, runner, store = make_updater(tmp_path, config=config)
    assert updater.run() == 0
    document = store.load()
    assert document["state"] == "update_available" and "automatic_install is off" in document["message"]
    assert not runner.commands("bash")


def test_a_newer_release_is_installed_unattended(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path)
    assert updater.run() == 0

    document = store.load()
    assert document["state"] == "installed"
    assert document["installed_version"] == "2.2.0"
    assert document["installed_commit"] == SHA
    assert document["installed_at"] == NOW
    assert document.get("failed_attempts") == {}
    (installer,) = runner.commands("bash")
    checkout = scratch(tmp_path) / f"v2.2.0-{RELEASE_ID}"
    assert installer["argv"] == ["bash", str(checkout / "install" / "install.sh")]
    assert installer["user"] == "taki" and installer["cwd"] == checkout
    assert installer["log_path"] == tmp_path / "state" / "release-update.log"
    assert not checkout.exists()
    clone = next(call for call in runner.commands("git") if "clone" in call["argv"])
    assert clone["argv"][-4:] == ["--branch", "v2.2.0", REPOSITORY, str(checkout)]
    assert "--depth" in clone["argv"] and clone["user"] == "taki"
    assert clone["env"]["GIT_TERMINAL_PROMPT"] == "0" and clone["env"]["GIT_ALLOW_PROTOCOL"] == "https"
    assert clone["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_installer_gets_an_explicit_unattended_environment(tmp_path: Path) -> None:
    updater, runner, _ = make_updater(tmp_path)
    updater.run()
    env = runner.commands("bash")[0]["env"]
    assert env["BDENCODE_UNATTENDED_UPDATE"] == "1"
    assert env["BDENCODE_DATA_ROOT"] == str(tmp_path / "data")
    assert env["BDENCODE_SOURCE_ROOT"] == str(tmp_path / "source")
    assert env["BDENCODE_CPU_PERCENT"] == "80"
    assert env["HOME"] == str(tmp_path / "home")
    assert "BDENCODE_WINDOWS_PORT" not in env
    # Nothing of the service environment may leak into the installer's test run.
    assert not any(key in env for key in ("BDENCODE_CONFIG", "BDENCODE_USER", "BDENCODE_DATABASE_PATH"))


def test_windows_managed_systems_run_the_wsl_installer_with_their_port(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, windows=True)
    assert updater.run() == 0
    (installer,) = runner.commands("bash")
    assert Path(installer["argv"][1]).parts[-2:] == ("install", "wsl-install.sh")
    assert installer["env"]["BDENCODE_WINDOWS_PORT"] == "8790"
    assert store.load()["state"] == "installed"


def test_a_local_mirror_only_allows_the_file_transport(tmp_path: Path) -> None:
    config = release_update.ReleaseConfig(repository="file:///srv/mirror.git")
    updater, runner, _ = make_updater(tmp_path, config=config)
    updater.run()
    clone = next(call for call in runner.commands("git") if "clone" in call["argv"])
    assert clone["env"]["GIT_ALLOW_PROTOCOL"] == "file"


def test_without_passwordless_sudo_the_release_is_only_reported(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, sudo_exit=1)
    assert updater.run() == 0
    document = store.load()
    assert document["state"] == "manual_update_required"
    assert "passwordless sudo" in document["message"]
    assert not runner.commands("bash")
    assert not any("clone" in call["argv"] for call in runner.commands("git"))


def test_a_busy_queue_defers_before_anything_is_downloaded(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, queue_exit=3)
    assert updater.run() == 0
    assert store.load()["state"] == "deferred"
    assert not runner.commands("bash")
    assert not any("clone" in call["argv"] for call in runner.commands("git"))


def queue_probe(runner: FakeRunner) -> list[str]:
    probes = [
        call["argv"]
        for call in runner.calls
        if Path(call["argv"][0]).name == "bdencode" and "--help" not in call["argv"]
    ]
    return probes[-1][1:]


def test_queue_probe_uses_the_install_safe_flag_only_when_the_cli_has_it(tmp_path: Path) -> None:
    updater, runner, _ = make_updater(tmp_path, queue_exit=3)
    updater.run()
    assert queue_probe(runner) == ["queue-idle", "--allow-install-safe-pause"]

    updater, runner, _ = make_updater(tmp_path / "legacy", queue_exit=3, safe_pause_flag=False)
    updater.run()
    assert queue_probe(runner) == ["queue-idle"]


def test_an_operationally_broken_queue_probe_is_a_check_failure(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, queue_exit=1)
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "check_failed" and "queue-idle failed" in document["message"]
    assert not runner.commands("bash")


def test_an_unmounted_source_root_defers_without_a_failure_count(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path)
    (tmp_path / "source").rmdir()
    assert updater.run() == 0
    document = store.load()
    assert document["state"] == "deferred" and "source root" in document["message"]
    assert not runner.commands("bash")


def test_the_installer_finding_a_busy_queue_is_not_a_failure(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, installer_exit=3)
    assert updater.run() == 0
    document = store.load()
    assert document["state"] == "deferred"
    assert document.get("failed_attempts", {}) == {}
    assert not (scratch(tmp_path) / f"v2.2.0-{RELEASE_ID}").exists()


def test_network_trouble_is_a_failed_check_and_changes_nothing(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, ls_remote_exit=128)
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "check_failed" and "cannot list the releases" in document["message"]
    assert "last_successful_check_at" not in document
    assert not runner.commands("bash") and not runner.commands("sudo")


def test_a_repository_without_release_tags_is_a_failed_check(tmp_path: Path) -> None:
    updater, _, store = make_updater(tmp_path, tags=("main", "v2.2.0-rc1", "nightly"))
    assert updater.run() == 1
    assert "no vX.Y.Z release tag" in store.load()["message"]


def test_an_unknown_installed_version_stops_the_run(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, installed="garbage")
    assert updater.run() == 1
    assert store.load()["state"] == "check_failed"
    assert not runner.commands("bash")


def test_a_failed_download_is_a_failed_check_and_leaves_no_files(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, clone_exit=128)
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "check_failed" and "cannot download v2.2.0" in document["message"]
    assert not runner.commands("bash")
    assert document.get("failed_attempts", {}) == {}


def test_failed_installations_are_retried_once_then_blocked_until_a_newer_tag(tmp_path: Path) -> None:
    for attempt in (1, 2):
        updater, runner, store = make_updater(tmp_path, installer_exit=1)
        assert updater.run() == 1
        document = store.load()
        assert document["state"] == "install_failed"
        assert document["failed_attempts"] == {"v2.2.0": attempt}
        assert len(runner.commands("bash")) == 1
        assert not (scratch(tmp_path) / f"v2.2.0-{RELEASE_ID}").exists()

    updater, runner, store = make_updater(tmp_path, installer_exit=1)
    assert updater.run() == 1
    assert store.load()["state"] == "blocked"
    assert not runner.commands("bash") and not runner.commands("sudo")

    updater, runner, store = make_updater(
        tmp_path, installer_exit=0, tags=("v2.1.0", "v2.2.0", "v2.3.0")
    )
    assert updater.run() == 0
    document = store.load()
    assert document["state"] == "installed" and document["installed_version"] == "2.3.0"
    assert document["failed_attempts"] == {}


def test_an_installer_that_times_out_counts_as_a_failure(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, installer_timeout=True)
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "install_failed" and "installer exit 124" in document["message"]
    assert document["failed_attempts"] == {"v2.2.0": 1}


def test_an_installer_that_leaves_the_old_version_is_a_failure(tmp_path: Path) -> None:
    updater, _, store = make_updater(tmp_path, installer_installs=False)
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "install_failed"
    assert "finished but BDEncode is 2.1.0" in document["message"]
    assert document["failed_attempts"] == {"v2.2.0": 1}


def test_a_tag_that_contradicts_its_pyproject_is_blocked_at_once(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, declared="2.2.1")
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "invalid_release"
    assert document["failed_attempts"] == {"v2.2.0": release_update.MAX_FAILED_ATTEMPTS}
    assert not runner.commands("bash")
    assert not (scratch(tmp_path) / f"v2.2.0-{RELEASE_ID}").exists()

    updater, runner, store = make_updater(tmp_path, declared="2.2.1")
    assert updater.run() == 1
    assert store.load()["state"] == "blocked"


@pytest.mark.parametrize(
    "missing", ["install/install.sh", "frontend/dist/index.html"]
)
def test_a_release_without_its_installer_or_frontend_is_invalid(tmp_path: Path, missing: str) -> None:
    files = tuple(
        item for item in ("install/install.sh", "install/wsl-install.sh", "frontend/dist/index.html") if item != missing
    )
    updater, runner, store = make_updater(tmp_path, files=files)
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "invalid_release" and missing in document["message"]
    assert not runner.commands("bash")


@posix_only
@pytest.mark.parametrize("linked", ["pyproject.toml", "install/install.sh", "frontend/dist/index.html"])
def test_a_release_that_links_to_other_files_is_invalid(tmp_path: Path, linked: str) -> None:
    updater, _, _ = make_updater(tmp_path)
    checkout = tmp_path / "checkout"
    (checkout / "install").mkdir(parents=True)
    (checkout / "frontend" / "dist").mkdir(parents=True)
    secret = tmp_path / "secret.toml"
    secret.write_text('[project]\nversion = "2.2.0"\n', encoding="utf-8")
    for relative in ("pyproject.toml", "install/install.sh", "frontend/dist/index.html"):
        target = checkout / relative
        if relative == linked:
            target.symlink_to(secret)
        else:
            target.write_text('[project]\nversion = "2.2.0"\n', encoding="utf-8")
    with pytest.raises(release_update.InvalidReleaseError, match=f"release is missing {linked}"):
        updater.verify_checkout(checkout, (2, 2, 0))


def test_a_windows_managed_release_needs_the_wsl_installer(tmp_path: Path) -> None:
    updater, _, store = make_updater(
        tmp_path, windows=True, files=("install/install.sh", "frontend/dist/index.html")
    )
    assert updater.run() == 1
    assert "install/wsl-install.sh" in store.load()["message"]


def test_stale_downloads_are_removed_and_foreign_directories_are_left_alone(tmp_path: Path) -> None:
    root = scratch(tmp_path)
    stale = root / "v2.0.0-20250101T000000Z-1"
    foreign = root / "keep-me"
    stale.mkdir(parents=True)
    foreign.mkdir()
    updater, _, _ = make_updater(tmp_path, installed="2.2.0")
    updater.run()
    # An up-to-date system does not touch the scratch area at all ...
    assert stale.exists() and foreign.exists()

    updater, _, _ = make_updater(tmp_path)
    updater.run()
    # ... a run that installs first clears what an earlier crash left behind.
    assert not stale.exists() and foreign.exists()


def test_checkout_removal_refuses_paths_outside_the_scratch_area(tmp_path: Path) -> None:
    updater, runner, _ = make_updater(tmp_path)
    victim = tmp_path / "data" / "v2.2.0-20261002T000500Z-1"
    victim.mkdir(parents=True)
    updater.remove_checkout(victim)
    updater.remove_checkout(scratch(tmp_path) / "not-a-release")
    assert victim.exists() and not runner.commands("rm")
    log = (tmp_path / "state" / "release-update.log").read_text(encoding="utf-8")
    assert log.count("refusing to remove unexpected path") == 2


def test_the_status_keeps_the_last_successful_check_across_failures(tmp_path: Path) -> None:
    updater, _, store = make_updater(tmp_path, installed="2.2.0")
    updater.run()
    assert store.load()["last_successful_check_at"] == NOW

    later = "2026-10-03T00:09:00Z"
    updater, _, store = make_updater(tmp_path, ls_remote_exit=128)
    updater.clock = lambda: later
    assert updater.run() == 1
    document = store.load()
    assert document["state"] == "check_failed" and document["checked_at"] == later
    assert document["last_successful_check_at"] == NOW


def test_every_run_is_logged_for_the_operator(tmp_path: Path) -> None:
    updater, _, _ = make_updater(tmp_path)
    updater.run()
    log = (tmp_path / "state" / "release-update.log").read_text(encoding="utf-8")
    assert "release check started" in log
    assert "running install/install.sh as taki" in log
    assert "installed: BDEncode 2.1.0 was updated to v2.2.0" in log


# -- real processes and a real git repository ---------------------------------------------------------


def git(*arguments: object, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "tag.gpgsign=false", *[str(item) for item in arguments]],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


class LocalRunner(release_update.SystemRunner):
    """The production runner without runuser: real processes, current account."""

    def run(self, argv: Any, *, user: str | None = None, **options: Any) -> Any:
        return super().run(argv, user=None, **options)


def build_release_repository(tmp_path: Path) -> str:
    work = tmp_path / "work"
    (work / "install").mkdir(parents=True)
    (work / "frontend" / "dist").mkdir(parents=True)
    (work / "frontend" / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (work / "install" / "install.sh").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$BDENCODE_UNATTENDED_UPDATE $BDENCODE_DATA_ROOT $BDENCODE_CPU_PERCENT" '
        '> "$BDENCODE_DATA_ROOT/installer-ran"\n'
        'echo "2.2.0" > "$BDENCODE_DATA_ROOT/app/current-version"\n'
        "echo installer output line\n",
        encoding="utf-8",
    )
    git("init", "--quiet", "--initial-branch=main", cwd=work)
    for version in ("2.1.0", "2.2.0"):
        (work / "pyproject.toml").write_text(
            f'[project]\nname = "bdencode-backend"\nversion = "{version}"\n', encoding="utf-8"
        )
        git("add", "-A", cwd=work)
        git("commit", "--quiet", "-m", f"release {version}", cwd=work)
        git("tag", f"v{version}", cwd=work)
    bare = tmp_path / "mirror.git"
    git("clone", "--quiet", "--bare", work, bare, cwd=tmp_path)
    return bare.resolve().as_uri()


@posix_only
@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_a_real_git_mirror_drives_the_whole_update(tmp_path: Path) -> None:
    repository = build_release_repository(tmp_path)
    data_root = tmp_path / "data"
    version_file = data_root / "app" / "current-version"
    bin_dir = data_root / "app" / "current" / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    version_file.write_text("2.1.0\n", encoding="utf-8")
    (bin_dir / "python").write_text(f'#!/bin/sh\ncat "{version_file}"\n', encoding="utf-8")
    (bin_dir / "bdencode").write_text(
        '#!/bin/sh\n[ "$2" = "--help" ] && echo "--allow-install-safe-pause"\nexit 0\n', encoding="utf-8"
    )
    for name in ("python", "bdencode"):
        (bin_dir / name).chmod(0o755)

    class Updater(release_update.ReleaseUpdater):
        def passwordless_sudo(self) -> bool:  # the sandbox account has no sudo rights to test
            return True

    (tmp_path / "source").mkdir()
    deployment = release_update.Deployment(
        task_user="taki",
        task_home=tmp_path / "home",
        data_root=data_root,
        source_root=tmp_path / "source",
        cpu_percent=70,
        windows_managed=False,
        windows_port=8787,
    )
    store = release_update.StatusStore(tmp_path / "state" / "status.json")
    report = release_update.Reporter(tmp_path / "state" / "release-update.log")
    updater = Updater(
        deployment,
        release_update.ReleaseConfig(repository=repository),
        LocalRunner(),
        store,
        report,
        release_id=RELEASE_ID,
    )

    assert updater.run() == 0

    document = store.load()
    assert document["state"] == "installed", document
    assert document["latest_tag"] == "v2.2.0" and document["installed_version"] == "2.2.0"
    assert len(document["installed_commit"]) == 40
    assert (data_root / "installer-ran").read_text(encoding="utf-8") == f"1 {data_root} 70\n"
    assert "installer output line" in (tmp_path / "state" / "release-update.log").read_text(encoding="utf-8")
    assert not (tmp_path / "home" / ".cache" / "bdencode-release-update" / f"v2.2.0-{RELEASE_ID}").exists()


@posix_only
def test_a_command_that_outlives_its_timeout_is_terminated_with_its_children() -> None:
    runner = release_update.SystemRunner()
    with pytest.raises(release_update.CommandTimeoutError):
        runner.run(["sh", "-c", "sleep 60 & wait"], timeout=0.5, grace=5)


def test_runner_wraps_commands_for_the_account_and_a_clean_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Process:
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, None]:
            return "out\n", None

    def fake_popen(command: list[str], **options: Any) -> Process:
        seen["command"], seen["options"] = command, options
        return Process()

    monkeypatch.setattr(release_update.subprocess, "Popen", fake_popen)
    result = release_update.SystemRunner().run(
        ["git", "status"], user="taki", env={"B": "2", "A": "1"}, cwd=Path("/tmp")
    )
    assert result.stdout == "out\n"
    assert seen["command"] == ["runuser", "-u", "taki", "--", "env", "-i", "A=1", "B=2", "git", "status"]
    assert seen["options"]["stdin"] == subprocess.DEVNULL and seen["options"]["start_new_session"] is True


# -- the command line -----------------------------------------------------------------------------------


@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a non-root POSIX user",
)
def test_the_command_line_refuses_to_run_without_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("BDENCODE_RELEASE_UPDATE_TESTING", raising=False)
    assert release_update.main(["--user", "taki", "--state-root", str(tmp_path / "state")]) == 2
    assert "must run as root" in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


def test_the_account_defaults_to_the_unit_environment_then_to_the_sudo_invoker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BDENCODE_USER", raising=False)
    monkeypatch.delenv("SUDO_USER", raising=False)
    assert release_update.parser().parse_args([]).user is None
    monkeypatch.setenv("SUDO_USER", "operator")
    assert release_update.parser().parse_args([]).user == "operator"
    monkeypatch.setenv("BDENCODE_USER", "taki")
    assert release_update.parser().parse_args([]).user == "taki"
    assert release_update.parser().parse_args(["--user", "other"]).user == "other"
    assert release_update.parser().parse_args(["check"]).command == "check"
    assert release_update.parser().parse_args([]).command == "run"


def test_the_command_line_needs_to_know_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BDENCODE_RELEASE_UPDATE_TESTING", "1")
    monkeypatch.delenv("BDENCODE_USER", raising=False)
    assert release_update.main(["--state-root", str(tmp_path / "state"), "--user", ""]) == 2
    assert "BDEncode account is unknown" in capsys.readouterr().err


@posix_only
def test_the_command_line_records_a_broken_configuration_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import getpass

    monkeypatch.setenv("BDENCODE_RELEASE_UPDATE_TESTING", "1")
    state = tmp_path / "state"
    code = release_update.main(
        [
            "--user", getpass.getuser(),
            "--state-root", str(state),
            "--data-root", str(tmp_path / "data"),
            "--service-config", str(tmp_path / "missing.toml"),
            "--release-config", str(tmp_path / "release-update.toml"),
        ]
    )
    assert code == 1
    document = json.loads((state / "status.json").read_text(encoding="utf-8"))
    assert document["state"] == "check_failed" and "cannot read the installed configuration" in document["message"]
    assert "check_failed" in (state / "release-update.log").read_text(encoding="utf-8")


@posix_only
def test_the_command_line_runs_a_check_with_the_installed_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import getpass

    monkeypatch.setenv("BDENCODE_RELEASE_UPDATE_TESTING", "1")
    data_root = tmp_path / "data"
    (tmp_path / "films").mkdir()
    service_config = tmp_path / "config.toml"
    write_service_config(service_config, data_root, tmp_path / "films")
    release_config = tmp_path / "release-update.toml"
    release_config.write_text(f'repository = "{REPOSITORY}"\n', encoding="utf-8")
    fake = FakeRunner(installed="2.1.0")
    monkeypatch.setattr(release_update, "SystemRunner", lambda: fake)
    monkeypatch.setattr(release_update, "WINDOWS_MARKER", tmp_path / "no-marker")

    code = release_update.main(
        [
            "check",
            "--user", getpass.getuser(),
            "--state-root", str(tmp_path / "state"),
            "--data-root", str(data_root),
            "--service-config", str(service_config),
            "--release-config", str(release_config),
        ]
    )

    assert code == 0
    document = json.loads((tmp_path / "state" / "status.json").read_text(encoding="utf-8"))
    assert document["state"] == "update_available" and document["latest_tag"] == "v2.2.0"
    assert not fake.commands("bash")


# -- wiring into the installer, the unit files and the documentation -------------------------------------


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_the_daily_unit_runs_the_release_updater_and_not_the_tool_updater() -> None:
    unit = read("deploy/systemd/bdencode-update.service.in")
    assert "ExecStart=/usr/local/libexec/bdencode-release-update run" in unit
    assert "bdencode-daily-update" not in unit
    assert "Requires=bdencode-update-recovery.service" in unit
    assert "Environment=BDENCODE_USER=@USER@" in unit and "Environment=BDENCODE_DATA_ROOT=@DATA_ROOT@" in unit
    assert "ExecStopPost=/usr/bin/systemctl --no-block start bdencode-install-recovery.service" in unit
    # The installer it starts writes below the account's home and below /etc.
    directives = "\n".join(line for line in unit.splitlines() if not line.startswith("#"))
    assert "ProtectHome" not in directives and "ReadWritePaths" not in directives
    assert "ProtectSystem" not in directives
    assert "TimeoutStartSec=3h" in unit
    # The helper's own installer timeout must fit inside the unit's.
    assert release_update.INSTALL_TIMEOUT_SECONDS + release_update.INSTALL_TERMINATE_GRACE_SECONDS < 3 * 3600


def test_the_timer_still_fires_once_a_day_and_catches_up() -> None:
    timer = read("deploy/systemd/bdencode-update.timer")
    assert "OnCalendar=daily" in timer and "Persistent=true" in timer
    assert "RandomizedDelaySec=45m" in timer
    assert "Description=Daily check for a new BDEncode release" in timer


def test_installer_publishes_the_release_updater_inside_the_rollback_snapshot() -> None:
    installer = read("install/install.sh")
    assert (
        'atomic_root_install "$repo_root/install/release_update.py" \\\n'
        "    /usr/local/libexec/bdencode-release-update 0755"
    ) in installer
    # The tool updater stays installed for manual use but is not on the timer.
    assert "/usr/local/libexec/bdencode-daily-update 0755" in installer
    module = importlib.util.spec_from_file_location(
        "bdencode_install_transaction_for_release_update", ROOT / "install" / "install_transaction.py"
    )
    assert module is not None and module.loader is not None
    install_transaction = importlib.util.module_from_spec(module)
    sys.modules[module.name] = install_transaction
    module.loader.exec_module(install_transaction)
    targets = {path.as_posix() for path in install_transaction.SYSTEM_TARGETS}
    assert "/usr/local/libexec/bdencode-release-update" in targets
    assert "/etc/bdencode/release-update.toml" not in targets  # operator-owned, never rolled back


def test_installer_does_not_stop_the_unit_that_runs_it_during_an_unattended_update() -> None:
    installer = read("install/install.sh")
    guard = installer.index('if [[ "${BDENCODE_UNATTENDED_UPDATE:-0}" != 1 ]]; then')
    stop = installer.index("sudo systemctl --no-block stop bdencode-update.service || true")
    assert guard < stop < installer.index("fi", stop)
    assert installer.count("stop bdencode-update.service") == 1
    # The timer is still stopped for the duration of every installation.
    assert installer.index("sudo systemctl stop bdencode-update.timer || true") < guard


def test_installer_writes_the_operator_config_once_and_never_leaks_credentials() -> None:
    installer = read("install/install.sh")
    start = installer.index("if ! sudo test -e /etc/bdencode/release-update.toml; then")
    block = installer[start : installer.index("\nfi\n", start)]
    assert 'git -C "$repo_root" remote get-url origin' in block
    assert "https://github.com/takachlaszlo/bdencode-backend.git" in block
    assert "automatic_install = true" in block
    assert "sudo chown root:root /etc/bdencode/release-update.toml" in block
    assert "sudo chmod 0644 /etc/bdencode/release-update.toml" in block


def installer_block(start: str, end: str) -> str:
    installer = read("install/install.sh")
    begin = installer.index(start)
    return installer[begin : installer.index(end, begin) + len(end)]


@posix_only
@pytest.mark.parametrize("unattended", [False, True])
def test_unattended_installation_leaves_the_update_service_running(
    tmp_path: Path, unattended: bool
) -> None:
    """Run the real installer lines with a recording sudo."""

    snippet = installer_block(
        "sudo systemctl stop bdencode-update.timer || true",
        "    sudo systemctl --no-block stop bdencode-update.service || true\nfi\n",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "sudo.log"
    (fake_bin / "sudo").write_text('#!/bin/sh\necho "$*" >> "$SUDO_LOG"\n', encoding="utf-8")
    (fake_bin / "sudo").chmod(0o755)
    environment = {"PATH": f"{fake_bin}:/usr/bin:/bin", "SUDO_LOG": str(log)}
    if unattended:
        environment["BDENCODE_UNATTENDED_UPDATE"] = "1"
    subprocess.run(["bash", "-c", f"set -Eeuo pipefail\n{snippet}"], check=True, env=environment)
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "systemctl stop bdencode-update.timer"
    assert ("systemctl --no-block stop bdencode-update.service" in calls) is (not unattended)


@posix_only
@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://github.com/someone/fork.git", "https://github.com/someone/fork.git"),
        ("https://user:secret@github.com/someone/fork.git", release_update.DEFAULT_REPOSITORY),
        ("git@github.com:someone/fork.git", release_update.DEFAULT_REPOSITORY),
        (None, release_update.DEFAULT_REPOSITORY),
    ],
)
def test_installer_writes_a_valid_operator_config_from_a_safe_origin(
    tmp_path: Path, origin: str | None, expected: str
) -> None:
    snippet = installer_block("if ! sudo test -e /etc/bdencode/release-update.toml; then", "\nfi\n")
    config = tmp_path / "release-update.toml"
    snippet = snippet.replace("/etc/bdencode/release-update.toml", str(config))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git("init", "--quiet", cwd=checkout)
    if origin is not None:
        git("remote", "add", "origin", origin, cwd=checkout)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # A sudo that runs the command unprivileged and accepts the chown it cannot do.
    (fake_bin / "sudo").write_text(
        '#!/bin/sh\n[ "$1" = chown ] && exit 0\nexec "$@"\n', encoding="utf-8"
    )
    (fake_bin / "sudo").chmod(0o755)
    script = f'set -Eeuo pipefail\nrepo_root="{checkout}"\n{snippet}'
    environment = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}

    subprocess.run(["bash", "-c", script], check=True, env=environment)

    loaded = release_update.load_release_config(config)
    assert loaded == release_update.ReleaseConfig(repository=expected, automatic_install=True)
    if os.name == "posix":
        assert (config.stat().st_mode & 0o777) == 0o644

    # The file now belongs to the operator: a second run must not overwrite it.
    config.write_text('repository = "https://example.org/mine.git"\nautomatic_install = false\n', encoding="utf-8")
    subprocess.run(["bash", "-c", script], check=True, env=environment)
    assert release_update.load_release_config(config) == release_update.ReleaseConfig(
        "https://example.org/mine.git", False
    )


@posix_only
def test_installer_origin_filter_accepts_plain_https_urls_only() -> None:
    installer = read("install/install.sh")
    line = next(item for item in installer.splitlines() if 'update_repository" =~' in item)
    pattern = line.split("=~ ", 1)[1].rsplit(" ]]", 1)[0]
    bash = shutil.which("bash")
    assert bash is not None

    def accepted(url: str) -> bool:
        script = f'[[ "$1" =~ {pattern} ]]'
        return subprocess.run([bash, "-c", script, "bash", url], check=False).returncode == 0

    assert accepted("https://github.com/takachlaszlo/bdencode-backend.git")
    assert accepted("https://example.org:8443/a/b")
    for rejected in (
        "https://user:secret@github.com/x/y.git",
        "http://github.com/x/y.git",
        "git@github.com:x/y.git",
        "file:///srv/x.git",
        "https://github.com/x/y.git?token=abc",
        "",
    ):
        assert not accepted(rejected), rejected


@posix_only
def test_changed_shell_scripts_keep_valid_bash_syntax() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    for name in ("install.sh", "daily-update.sh", "uninstall.sh"):
        subprocess.run([bash, "-n", str(ROOT / "install" / name)], check=True)


def test_uninstaller_removes_the_release_updater_and_its_operator_config() -> None:
    uninstaller = read("install/uninstall.sh")
    assert "/usr/local/libexec/bdencode-release-update" in uninstaller
    assert "/etc/bdencode/release-update.toml" in uninstaller


def test_root_scripts_never_follow_or_truncate_the_worker_writable_lock() -> None:
    recover = read("install/update-recover.sh")
    assert 'exec 9>"$deployment_lock"' not in recover
    assert 'exec 9>>"$deployment_lock"' in recover
    assert recover.count('[[ -L "$deployment_lock" ]]') == 2  # before and after opening
    assert 'touch "$deployment_lock"' not in recover

    daily = read("install/daily-update.sh")
    assert 'exec 9>"$deployment_lock"' not in daily
    assert 'exec 9>>"$deployment_lock"' in daily
    assert 'chown "$task_user' not in daily
    assert daily.count('chown -h "$task_user') == 2
    assert 'for guarded in "$report_file" "$deployment_lock"' in daily


@posix_only
def test_the_recovery_script_refuses_a_symlinked_lock(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    (data_root / "state").mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("precious\n", encoding="utf-8")
    (data_root / "state" / "deployment.lock").symlink_to(victim)
    user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    result = subprocess.run(
        ["bash", str(ROOT / "install" / "update-recover.sh"), "--gate"],
        env={"PATH": "/usr/bin:/bin", "BDENCODE_USER": user, "BDENCODE_DATA_ROOT": str(data_root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and "Refusing a symlink deployment lock" in result.stderr
    assert victim.read_text(encoding="utf-8") == "precious\n"


def test_the_tool_updater_documents_that_it_is_manual_now() -> None:
    head = " ".join(line.lstrip("# ") for line in read("install/daily-update.sh").splitlines()[:8])
    assert "daily timer no longer runs it" in head and "bdencode-release-update" in head


def test_release_update_module_is_executable_python_with_a_help_text() -> None:
    assert read("install/release_update.py").startswith("#!/usr/bin/env python3\n")
    result = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--help"], check=True, capture_output=True, text=True
    )
    assert "run" in result.stdout and "check" in result.stdout


# -- operator-requested install (rollback), notification, media report ---------------------------------


def test_an_operator_can_install_exactly_one_tag_even_an_older_one(tmp_path: Path) -> None:
    updater, runner, store = make_updater(
        tmp_path, installed="2.2.1", tags=("v2.2.0", "v2.2.1"), install_tag="v2.2.0",
        config=release_update.ReleaseConfig(repository=REPOSITORY, automatic_install=False),
    )
    assert updater.run() == 0
    document = store.load()
    assert document["state"] == "installed" and document["installed_version"] == "2.2.0"
    (installer,) = runner.commands("bash")
    assert "v2.2.0" in installer["argv"][1]
    # No listing of releases: the operator named the tag.
    assert not any("ls-remote" in call["argv"] for call in runner.commands("git"))


def test_an_install_request_still_waits_for_an_idle_queue_and_sudo(tmp_path: Path) -> None:
    updater, runner, store = make_updater(tmp_path, installed="2.2.1", install_tag="v2.2.0", queue_exit=3)
    assert updater.run() == 0 and store.load()["state"] == "deferred" and not runner.commands("bash")
    updater, runner, store = make_updater(tmp_path / "b", installed="2.2.1", install_tag="v2.2.0", sudo_exit=1)
    assert updater.run() == 0 and store.load()["state"] == "manual_update_required"


def test_install_command_line_needs_a_valid_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BDENCODE_RELEASE_UPDATE_TESTING", "1")
    for extra in ([], ["--tag", "latest"], ["--tag", "v2.2"]):
        assert release_update.main(["install", "--user", "x", "--state-root", str(tmp_path / "s"), *extra]) == 2
    assert "install needs --tag vX.Y.Z" in capsys.readouterr().err
    assert release_update.parser().parse_args(["install", "--tag", "v2.2.1"]).tag == "v2.2.1"


def test_release_config_accepts_only_a_plain_https_notification_url(tmp_path: Path) -> None:
    path = tmp_path / "release-update.toml"
    path.write_text('notify_url = "https://ntfy.sh/my-bdencode"\n', encoding="utf-8")
    assert release_update.load_release_config(path).notify_url == "https://ntfy.sh/my-bdencode"
    for bad in ("http://ntfy.sh/x", "https://user:pw@ntfy.sh/x", "https://ntfy.sh/x?a=1", "ftp://x/y"):
        path.write_text(f'notify_url = "{bad}"\n', encoding="utf-8")
        with pytest.raises(release_update.ReleaseUpdateError, match="notify_url"):
            release_update.load_release_config(path)


def test_outcomes_that_need_attention_are_posted_once(tmp_path: Path) -> None:
    posts: list = []
    config = release_update.ReleaseConfig(repository=REPOSITORY, notify_url="https://ntfy.sh/x")
    updater, _, _ = make_updater(tmp_path, config=config, posts=posts, installer_exit=1)
    assert updater.run() == 1
    (url, payload), = posts
    body = json.loads(payload)
    assert url == "https://ntfy.sh/x" and body["state"] == "install_failed"
    assert body["event"] == "bdencode-release-update.install_failed" and "installing v2.2.0 failed" in body["text"]

    # A repeated failure of the same kind (blocked every night) is not announced again.
    updater, _, _ = make_updater(tmp_path, config=config, posts=posts, installer_exit=1)
    updater.run()
    updater, _, _ = make_updater(tmp_path, config=config, posts=posts, installer_exit=1)
    updater.run()
    states = [json.loads(item[1])["state"] for item in posts]
    assert states == ["install_failed", "blocked"]

    posts.clear()
    updater, _, _ = make_updater(tmp_path / "ok", config=config, posts=posts)
    assert updater.run() == 0
    assert [json.loads(item[1])["state"] for item in posts] == ["installed"]


def test_quiet_outcomes_and_a_missing_url_post_nothing(tmp_path: Path) -> None:
    posts: list = []
    config = release_update.ReleaseConfig(repository=REPOSITORY, notify_url="https://ntfy.sh/x")
    updater, _, _ = make_updater(tmp_path, config=config, posts=posts, installed="2.2.0")
    updater.run()
    updater, _, _ = make_updater(tmp_path / "n", posts=posts)  # no notify_url configured
    updater.run()
    assert posts == []


def test_a_failing_webhook_never_breaks_the_update(tmp_path: Path) -> None:
    config = release_update.ReleaseConfig(repository=REPOSITORY, notify_url="https://ntfy.sh/x")
    updater, _, store = make_updater(tmp_path, config=config)

    def broken(url: str, payload: bytes) -> None:
        raise OSError("network down")

    updater.post = broken
    assert updater.run() == 0 and store.load()["state"] == "installed"
    assert "notification failed: network down" in (tmp_path / "state" / "release-update.log").read_text(encoding="utf-8")


def test_pending_media_package_updates_are_reported_not_installed(tmp_path: Path) -> None:
    apt = (
        "Inst ffmpeg [7:7.1.5-0+deb13u1] (7:7.1.6-0+deb13u1 Debian-Security:13/stable-security [amd64])\n"
        "Inst mkvtoolnix [92.0-1] (92.0-1+deb13u1 Debian-Security:13/stable-security [amd64])\n"
        "Conf ffmpeg (7:7.1.6-0+deb13u1 Debian-Security:13/stable-security [amd64])\n"
    )
    updater, runner, store = make_updater(tmp_path, installed="2.2.0", apt_output=apt)
    assert updater.run() == 0
    assert store.load()["media_updates"] == [
        "ffmpeg 7:7.1.5-0+deb13u1 -> 7:7.1.6-0+deb13u1",
        "mkvtoolnix 92.0-1 -> 92.0-1+deb13u1",
    ]
    (call,) = runner.commands("apt-get")
    assert call["argv"][:5] == ["apt-get", "-s", "-qq", "install", "--only-upgrade"] and call["user"] is None
    # Nothing pending later: the list is cleared again.
    updater, _, store = make_updater(tmp_path, installed="2.2.0")
    updater.run()
    assert store.load()["media_updates"] == []
