"""Copy a disc from a slow mount to local disk before the reference remux.

On a Windows-managed installation the discs usually live on a Windows drive, which WSL reaches over
its 9p bridge. libbluray reads 6 KiB units, which that bridge serves at about 11-16 MB/s: the 60 GB
reference remux of a UHD disc took an hour. Large reads from several threads reach about 300 MB/s on
the same bridge, so the selected title's files are first copied ("staged") to the local cache with
parallel ranged reads (about five minutes for a UHD title) and the remux reads the local copy (another
five minutes).

The staged copy is byte-identical (sizes and modification times are kept and checked); the job keeps
referring to the original path. A stage is reused while the source files are unchanged and is removed
once the reference remux exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# Network and bridge filesystems whose small-read throughput makes direct libbluray access slow.
SLOW_FILESYSTEMS = frozenset({"9p", "v9fs", "drvfs", "cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs"})
DEFAULT_READERS = 8
DEFAULT_BLOCK = 8 * 1024 * 1024
PARALLEL_THRESHOLD = 256 * 1024 * 1024
MANIFEST = "stage.json"
STAGE_SCHEMA = 1
STALE_SECONDS = 24 * 3600
STOP_CHECK_SECONDS = 1.0


class StagingUnavailable(RuntimeError):
    """The disc cannot be staged (not enough space, unreadable file); read it in place instead."""


class StagingInterrupted(RuntimeError):
    """The copy was stopped because ``should_stop`` asked for it (job paused or cancelled)."""


@dataclass(frozen=True, slots=True)
class StagedFile:
    relative: str
    size: int
    mtime_ns: int

    def to_dict(self) -> dict[str, object]:
        return {"relative": self.relative, "size": self.size, "mtime_ns": self.mtime_ns}


def filesystem_type(path: Path, mounts: str | None = None) -> str | None:
    """The filesystem type of the mount that contains ``path`` (Linux ``/proc/self/mounts``)."""

    if mounts is None:
        try:
            mounts = Path("/proc/self/mounts").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    target = posixpath.abspath(Path(path).as_posix())
    best: tuple[int, str] | None = None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mountpoint = fields[1].replace("\\040", " ")
        if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
            if best is None or len(mountpoint) > best[0]:
                best = (len(mountpoint), fields[2])
    return best[1] if best else None


def needs_staging(source: Path, *, mode: str = "auto", mounts: str | None = None) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    return filesystem_type(source, mounts) in SLOW_FILESYSTEMS


def stage_root_for(cache_root: Path, source: Path) -> Path:
    key = hashlib.sha256(os.path.abspath(source).encode("utf-8", "surrogateescape")).hexdigest()[:20]
    return cache_root / "disc-stage" / key


def _is_unselected_clip(relative: Path, clips: frozenset[str] | None) -> bool:
    """A stream file (``BDMV/STREAM/**``) of a clip the selected title does not play."""

    if clips is None:
        return False
    parts = tuple(part.upper() for part in relative.parts)
    return len(parts) >= 3 and parts[:2] == ("BDMV", "STREAM") and relative.stem.upper() not in clips


def plan_files(source: Path, clips: Iterable[str] | None = None) -> list[StagedFile]:
    """The regular files of the disc's BDMV and CERTIFICATE trees (what libbluray may open).

    With ``clips`` only those clips' stream files are included; every playlist, clip-info and
    index file is still copied, so libbluray sees a complete disc structure for that title.
    """

    if not (source / "BDMV").is_dir():
        raise StagingUnavailable(f"{source} has no BDMV directory")
    wanted = None if clips is None else frozenset(item.upper() for item in clips)
    files: list[StagedFile] = []
    for top in ("BDMV", "CERTIFICATE"):
        root = source / top
        if not root.is_dir():
            continue
        for directory, subdirs, names in os.walk(root, followlinks=False):
            subdirs.sort()
            for name in sorted(names):
                path = Path(directory) / name
                if _is_unselected_clip(path.relative_to(source), wanted):
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                details = path.stat()
                files.append(StagedFile(path.relative_to(source).as_posix(), details.st_size, details.st_mtime_ns))
    return files


def _manifest_matches(target: Path, source: Path, files: Iterable[StagedFile]) -> bool:
    try:
        document = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    expected = [item.to_dict() for item in files]
    if document.get("schema") != STAGE_SCHEMA or document.get("source") != os.path.abspath(source):
        return False
    if document.get("files") != expected:
        return False
    # The copies themselves must still be there with the recorded sizes.
    return all((target / item["relative"]).is_file()
               and (target / item["relative"]).stat().st_size == item["size"] for item in expected)


def current_stage(cache_root: Path, source: Path, clips: Iterable[str] | None = None) -> Path | None:
    """The complete, still matching stage of ``source``, if there is one."""

    target = stage_root_for(cache_root, source)
    if not target.is_dir():
        return None
    try:
        files = plan_files(source, clips)
    except (OSError, StagingUnavailable):
        return None
    return target if _manifest_matches(target, source, files) else None


def _copy_range(source: Path, destination: Path, start: int, end: int, block: int,
                progress: Callable[[int], None]) -> None:
    with open(source, "rb", buffering=0) as reader, open(destination, "r+b", buffering=0) as writer:
        reader.seek(start)
        writer.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = reader.read(min(block, remaining))
            if not chunk:
                raise StagingUnavailable(f"{source} ended early at offset {end - remaining}")
            writer.write(chunk)
            remaining -= len(chunk)
            progress(len(chunk))


def _copy_file(source: Path, destination: Path, size: int, *, readers: int, block: int,
               progress: Callable[[int], None]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with open(destination, "wb") as handle:
        handle.truncate(size)
    if size < PARALLEL_THRESHOLD or readers <= 1:
        _copy_range(source, destination, 0, size, block, progress)
    else:
        bounds = [size * index // readers for index in range(readers + 1)]
        with ThreadPoolExecutor(max_workers=readers) as pool:
            futures = [pool.submit(_copy_range, source, destination, bounds[index], bounds[index + 1], block, progress)
                       for index in range(readers)]
            for future in futures:
                future.result()
    shutil.copystat(source, destination)


def stage_disc(
    source: Path,
    cache_root: Path,
    *,
    clips: Iterable[str] | None = None,
    reserve_bytes: int = 0,
    readers: int = DEFAULT_READERS,
    block: int = DEFAULT_BLOCK,
    report: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """Copy the disc to local disk (or reuse a matching copy) and return the local root.

    ``clips`` limits the stream files to the selected title's clips. ``reserve_bytes`` is space
    that must stay free next to the copy (the reference remux follows).
    """

    clips = None if clips is None else tuple(clips)
    files = plan_files(source, clips)
    target = stage_root_for(cache_root, source)
    if _manifest_matches(target, source, files):
        return target
    total = sum(item.size for item in files)
    cache_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cache_root).free
    if free < total + reserve_bytes:
        raise StagingUnavailable(
            f"not enough local space to stage the disc ({total / 1e9:.1f} GB needed plus "
            f"{reserve_bytes / 1e9:.1f} GB reserve, {free / 1e9:.1f} GB free)"
        )
    partial = target.with_name(target.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    shutil.rmtree(target, ignore_errors=True)
    partial.mkdir(parents=True)
    done = 0
    lock = threading.Lock()
    last_report = [0.0]
    last_check = [time.monotonic()]
    stopped = [False]

    def progress(amount: int) -> None:
        nonlocal done
        with lock:
            done += amount
            now = time.monotonic()
            if not stopped[0] and should_stop is not None and now - last_check[0] >= STOP_CHECK_SECONDS:
                last_check[0] = now
                stopped[0] = bool(should_stop())
            if stopped[0]:
                # Every reader thread stops at its next block.
                raise StagingInterrupted("staging was stopped")
            if report is not None and (now - last_report[0] >= 2.0 or done == total):
                last_report[0] = now
                report(done, total)

    try:
        for item in files:
            _copy_file(source / item.relative, partial / item.relative, item.size,
                       readers=readers, block=block, progress=progress)
            copied = (partial / item.relative).stat()
            if copied.st_size != item.size:
                raise StagingUnavailable(f"staged copy of {item.relative} has the wrong size")
        (partial / MANIFEST).write_text(
            json.dumps({"schema": STAGE_SCHEMA, "source": os.path.abspath(source),
                        "files": [item.to_dict() for item in files], "completed_at": time.time()}, indent=1),
            encoding="utf-8",
        )
        # The source must not have changed while it was copied.
        if plan_files(source, clips) != files:
            raise StagingUnavailable("the disc changed while it was being staged")
        os.replace(partial, target)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return target


def remove_stage(cache_root: Path, source: Path) -> None:
    shutil.rmtree(stage_root_for(cache_root, source), ignore_errors=True)


def remove_stale_stages(cache_root: Path, *, keep: Path | None = None, max_age: float = STALE_SECONDS) -> list[Path]:
    """Delete stages (and interrupted partial copies) older than ``max_age`` seconds."""

    removed: list[Path] = []
    root = cache_root / "disc-stage"
    if not root.is_dir():
        return removed
    now = time.time()
    for entry in root.iterdir():
        if keep is not None and entry == keep:
            continue
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age > max_age or entry.name.endswith(".partial") and age > 3600:
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
    return removed
