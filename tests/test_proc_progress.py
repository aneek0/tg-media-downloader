import asyncio
import sys

import pytest

from services.proc import run_command


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
async def test_run_command_reads_json_line_larger_than_stream_limit():
    """yt-dlp probe JSON may exceed StreamReader's 64 KiB readline limit."""
    payload = "x" * 131072

    stdout, _ = await run_command(
        [sys.executable, "-c", "print('x' * 131072)"]
    )

    assert stdout == payload


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
