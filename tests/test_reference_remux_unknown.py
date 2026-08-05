from pathlib import Path

from bdencode.encode import (
    ReferenceRemuxPlan,
    reference_remux_command,
)


def test_reference_remux_ignores_unknown_input_streams(
    tmp_path: Path,
) -> None:
    plan = ReferenceRemuxPlan(
        disc_root=tmp_path / "disc",
        playlist_id="00803",
        output_path=tmp_path / "reference.mkv",
        angle=1,
    )

    command = reference_remux_command(plan)

    assert "-ignore_unknown" in command
    assert command.index("-ignore_unknown") < command.index("-map")
    assert command[command.index("-map") + 1] == "0"
    assert "-copy_unknown" not in command
