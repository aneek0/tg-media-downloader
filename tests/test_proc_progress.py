import asyncio

import pytest

from services.proc import run_command
from services.progress import StatusProgress


@pytest.mark.asyncio
async def test_run_command_streams_lines_to_callback():
    lines: list[str] = []

    async def on_output(line: str) -> None:
        lines.append(line)

    stdout, _ = await run_command(
        ["bash", "-c", "echo out1; echo err1 >&2; echo out2"],
        on_output=on_output,
    )
    assert stdout == "out1\nout2"
    assert sorted(lines) == ["err1", "out1", "out2"]


@pytest.mark.asyncio
async def test_run_command_without_callback_unchanged():
    stdout, stderr = await run_command(["bash", "-c", "echo a; echo b >&2"])
    assert (stdout, stderr) == ("a", "b")


@pytest.mark.asyncio
async def test_run_command_callback_error_does_not_fail_command():
    async def bad_callback(_line: str) -> None:
        raise ValueError("boom")

    stdout, _ = await run_command(["echo", "hi"], on_output=bad_callback)
    assert stdout == "hi"


@pytest.mark.asyncio
async def test_run_command_still_times_out_with_streaming():
    with pytest.raises(RuntimeError, match="timed out"):
        await asyncio.wait_for(
            run_command(["sleep", "5"], timeout=0.3, on_output=None), timeout=2
        )


@pytest.mark.asyncio
async def test_status_progress_parses_and_rate_limits():
    calls: list[str] = []

    async def edit(text: str) -> None:
        calls.append(text)

    reporter = StatusProgress(edit, "video.mp4", min_interval=0)
    await reporter.feed_line("[download]  10.0% of ~100.00MiB at 5.00MiB/s ETA 00:18")
    await reporter.feed_line("not a progress line")
    await reporter.feed_line("[download]  10.0% of ~100.00MiB at 5.00MiB/s ETA 00:18")
    await reporter.feed_line("[download]  55.5% of ~100.00MiB")

    assert len(calls) == 2
    assert "10.0% of 100.00MiB" in calls[0]
    assert "Speed: 5.00MiB/s" in calls[0]
    assert "ETA: 00:18" in calls[0]
    assert "55.5% of 100.00MiB" in calls[1]
    assert "Speed" not in calls[1]


@pytest.mark.asyncio
async def test_status_progress_min_interval_blocks_updates():
    calls: list[str] = []

    async def edit(text: str) -> None:
        calls.append(text)

    reporter = StatusProgress(edit, "v.mp4", min_interval=60)
    await reporter.feed_line("[download]  10.0% of ~100.00MiB")
    await reporter.feed_line("[download]  40.0% of ~100.00MiB")
    await reporter.feed_line("[download]  80.0% of ~100.00MiB")

    assert len(calls) == 1
    assert "10.0%" in calls[0]


@pytest.mark.asyncio
async def test_status_progress_swallows_edit_errors():
    async def broken_edit(_text: str) -> None:
        raise RuntimeError("message to edit not found")

    reporter = StatusProgress(broken_edit, "v.mp4", min_interval=0)
    await reporter.feed_line("[download]  10.0% of ~100.00MiB")  # must not raise
