from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "install" / "apt_transaction.py"
SPEC = importlib.util.spec_from_file_location("bdencode_apt_transaction", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
apt_transaction = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = apt_transaction
SPEC.loader.exec_module(apt_transaction)


def test_parse_simulation_extracts_exact_upgrade_versions() -> None:
    output = """
Reading package lists...
Inst libavcodec59 [7:5.1.8-0+deb12u1] (7:5.1.9-0+deb12u1 Debian-Security:12/oldstable-security [amd64])
Inst ffmpeg [7:5.1.8-0+deb12u1] (7:5.1.9-0+deb12u1 Debian:12/oldstable [amd64])
Conf libavcodec59 (7:5.1.9-0+deb12u1 Debian-Security:12/oldstable-security [amd64])
"""

    assert apt_transaction.parse_simulation(output) == [
        apt_transaction.PlannedUpgrade(
            query_name="libavcodec59",
            old_version="7:5.1.8-0+deb12u1",
            new_version="7:5.1.9-0+deb12u1",
        ),
        apt_transaction.PlannedUpgrade(
            query_name="ffmpeg",
            old_version="7:5.1.8-0+deb12u1",
            new_version="7:5.1.9-0+deb12u1",
        ),
    ]


def test_parse_simulation_accepts_no_change() -> None:
    assert apt_transaction.parse_simulation("0 upgraded, 0 newly installed\n") == []


@pytest.mark.parametrize(
    "line,expected",
    [
        ("Inst new-abi (2.0 Debian [amd64])", "new install"),
        ("Inst malformed", "unknown plan"),
        ("Remv libavcodec59 [1.0]", "removal"),
    ],
)
def test_parse_simulation_fails_closed(line: str, expected: str) -> None:
    with pytest.raises(apt_transaction.TransactionError, match=expected):
        apt_transaction.parse_simulation(line)


def test_parse_simulation_rejects_duplicate_package() -> None:
    line = "Inst ffmpeg [1.0] (2.0 Debian [amd64])"
    with pytest.raises(apt_transaction.TransactionError, match="Duplicate"):
        apt_transaction.parse_simulation(f"{line}\n{line}\n")


def test_control_source_name_handles_binary_and_explicit_source() -> None:
    assert apt_transaction.source_name({"Package": "ffmpeg"}) == "ffmpeg"
    assert (
        apt_transaction.source_name(
            {"Package": "libavcodec59", "Source": "ffmpeg (7:5.1.9-0+deb12u1)"}
        )
        == "ffmpeg"
    )


def test_transaction_id_is_strictly_scoped(tmp_path: Path) -> None:
    transaction = apt_transaction.AptTransaction(
        tmp_path, Path("/unused/sources"), Path("/unused/guard")
    )
    assert (
        transaction.transaction_dir("20260802T123456Z-42")
        == tmp_path / "20260802T123456Z-42"
    )
    with pytest.raises(
        apt_transaction.TransactionError, match="Invalid transaction id"
    ):
        transaction.transaction_dir("../../etc")


def test_atomic_state_write_replaces_complete_file(tmp_path: Path) -> None:
    transaction = tmp_path / "20260802T123456Z-42"
    transaction.mkdir()
    apt_transaction.AptTransaction.write_state(transaction, "PREPARED")
    apt_transaction.AptTransaction.write_state(transaction, "APPLYING")
    assert (transaction / "state").read_text(encoding="ascii") == "APPLYING\n"


def test_rollback_cache_is_retryable_and_rejects_symlinks(tmp_path: Path) -> None:
    cache = tmp_path / "rollback-cache"
    apt_transaction.AptTransaction.prepare_private_cache(cache)
    apt_transaction.AptTransaction.prepare_private_cache(cache)
    assert cache.is_dir()
    assert (cache / "partial").is_dir()

    unsafe = tmp_path / "unsafe-cache"
    try:
        unsafe.symlink_to(cache, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(apt_transaction.TransactionError, match="Unsafe rollback"):
        apt_transaction.AptTransaction.prepare_private_cache(unsafe)


def guard_fixture(tmp_path: Path) -> tuple[object, Path]:
    transaction_id = "20260802T123456Z-42"
    transaction_dir = tmp_path / transaction_id
    archive = transaction_dir / "new" / "0000.deb"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"verified candidate")
    (transaction_dir / "state").write_text("APPLYING\n", encoding="ascii")
    (transaction_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "transaction_id": transaction_id,
                "packages": [
                    {
                        "package": "ffmpeg",
                        "architecture": "amd64",
                        "old_version": "1.0",
                        "new_version": "2.0",
                        "new_deb": "new/0000.deb",
                        "new_sha256": apt_transaction.sha256_file(archive),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "active").write_text(f"{transaction_id}\n", encoding="ascii")
    helper = apt_transaction.AptTransaction(
        tmp_path, tmp_path / "sources", tmp_path / "guard"
    )
    return helper, archive


def invoke_guard(
    helper: object,
    archive: Path,
    monkeypatch: pytest.MonkeyPatch,
    package: str = "ffmpeg",
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        protocol = (
            "VERSION 3\n"
            "DPkg::Tools::Options::guard::Version=3\n"
            "\n"
            f"{package} 1.0 amd64 none < 2.0 amd64 none {archive.resolve()}\n"
        )
        os.write(write_fd, protocol.encode("utf-8"))
        os.close(write_fd)
        write_fd = -1
        monkeypatch.setenv("APT_HOOK_INFO_FD", str(read_fd))
        helper.guard_apt()
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_v3_guard_accepts_exact_manifest_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper, archive = guard_fixture(tmp_path)
    invoke_guard(helper, archive, monkeypatch)


def test_v3_guard_rejects_unplanned_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper, archive = guard_fixture(tmp_path)
    with pytest.raises(apt_transaction.TransactionError, match="unplanned"):
        invoke_guard(helper, archive, monkeypatch, package="mkvtoolnix")
