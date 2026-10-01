from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bdencode.vmaf_runner import StreamedVmafError, main, run_streamed_vmaf

posix_only = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "mkfifo"),
    reason="streamed VMAF needs POSIX named pipes",
)

# Stand-ins for libvmaf, vspipe and ffmpeg.  The fake libvmaf records its own
# arguments, checks that it was handed a real named pipe, and writes the
# smallest JSON the runner accepts; the producers simply succeed.
FAKE_VMAF = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$FAKE_VMAF_ARGS"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out="$2"; shift 2 ;;
    --reference) reference="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [ -p "$reference" ]; then echo fifo > "$FAKE_VMAF_KIND"; fi
printf '{"frames":[{"frameNum":0}],"pooled_metrics":{"vmaf":{"mean":91.0,"harmonic_mean":90.5}}}' > "$out"
"""
FAKE_PRODUCER = "#!/usr/bin/env bash\nexit 0\n"


def _tools(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("vmaf", FAKE_VMAF),
        ("vspipe", FAKE_PRODUCER),
        ("ffmpeg", FAKE_PRODUCER),
    ):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return {
        "vmaf": str(bin_dir / "vmaf"),
        "vspipe": str(bin_dir / "vspipe"),
        "ffmpeg": str(bin_dir / "ffmpeg"),
    }


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    script = tmp_path / "sample.vpy"
    script.write_text("# fake\n", encoding="utf-8")
    encoded = tmp_path / "probe.mkv"
    encoded.write_bytes(b"x")
    job_tree = tmp_path / "job" / "work" / "crf-search"
    return script, encoded, job_tree


@posix_only
@pytest.mark.parametrize("vmaf_only", [False, True])
def test_named_pipes_live_under_fifo_root_not_beside_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vmaf_only: bool
) -> None:
    tools = _tools(tmp_path)
    script, encoded, job_tree = _inputs(tmp_path)
    output = job_tree / "probe.vmaf.json"
    fifo_root = tmp_path / "cache" / "vmaf"
    args_log = tmp_path / "vmaf-args.txt"
    kind_log = tmp_path / "vmaf-kind.txt"
    monkeypatch.setenv("FAKE_VMAF_ARGS", str(args_log))
    monkeypatch.setenv("FAKE_VMAF_KIND", str(kind_log))

    result = run_streamed_vmaf(
        script,
        encoded,
        output,
        hdr10=False,
        threads=6,
        vmaf_only=vmaf_only,
        fifo_root=fifo_root,
        **tools,
    )

    argv = args_log.read_text(encoding="utf-8").split()
    assert argv[argv.index("--threads") + 1] == "6"
    assert ("--feature" in argv) is (not vmaf_only)
    document = json.loads(result.read_text(encoding="utf-8"))
    assert document["bdencode"]["additional_features"] == (
        [] if vmaf_only else ["psnr", "float_ssim", "float_ms_ssim"]
    )
    assert kind_log.read_text(encoding="utf-8").strip() == "fifo"
    reference_pipe = Path(argv[argv.index("--reference") + 1])
    assert fifo_root.resolve() in reference_pipe.parents
    # The pipes are gone afterwards and none ever appeared in the job tree.
    assert list(fifo_root.iterdir()) == []
    assert sorted(item.name for item in job_tree.iterdir()) == ["probe.vmaf.json"]


@posix_only
def test_default_keeps_the_historical_pipe_location_and_single_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools(tmp_path)
    script, encoded, job_tree = _inputs(tmp_path)
    output = job_tree / "probe.vmaf.json"
    args_log = tmp_path / "vmaf-args.txt"
    monkeypatch.setenv("FAKE_VMAF_ARGS", str(args_log))
    monkeypatch.setenv("FAKE_VMAF_KIND", str(tmp_path / "vmaf-kind.txt"))

    run_streamed_vmaf(script, encoded, output, hdr10=False, **tools)

    argv = args_log.read_text(encoding="utf-8").split()
    assert argv[argv.index("--threads") + 1] == "0"
    assert {"psnr", "float_ssim", "float_ms_ssim"} <= set(argv)
    reference_pipe = Path(argv[argv.index("--reference") + 1])
    assert job_tree.resolve() in reference_pipe.parents


def test_negative_thread_counts_are_refused(tmp_path: Path) -> None:
    script, encoded, job_tree = _inputs(tmp_path)
    with pytest.raises(StreamedVmafError):
        run_streamed_vmaf(
            script, encoded, job_tree / "x.json", hdr10=False, threads=-1
        )
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "--script",
                str(script),
                "--encoded",
                str(encoded),
                "--output",
                str(job_tree / "x.json"),
                "--threads",
                "-3",
            ]
        )
    assert exit_info.value.code == 2
