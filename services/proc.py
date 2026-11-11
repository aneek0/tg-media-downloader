from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from utils.logging_config import redact_command

logger = logging.getLogger(__name__)


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill the command and every child it spawned (ffmpeg, etc.)."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()



def _resolve_program(program: str) -> str:
    """Use the venv's own copy of a tool when PATH does not have it.

    The bot can be started as `.venv/bin/python bot.py` (a systemd unit, a
    bare shell) without activating the venv, which leaves `gallery-dl` and
    `yt-dlp` off PATH even though the bot's dependencies installed them.
    """
    if os.sep in program or shutil.which(program):
        return program
    candidate = Path(sys.executable).parent / program
    return str(candidate) if candidate.is_file() else program


async def _read_stream(
    stream: asyncio.StreamReader | None,
    sink: list[bytes],
    on_output: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Drain a subprocess stream into sink, streaming lines to on_output."""
    if stream is None:
        return
    while True:
        try:
            line = await stream.readline()
        except ValueError:
            # A single output line over the 64 KiB StreamReader limit is not
            # fatal: skip it instead of killing the drain task.
            continue
        if not line:
            return
        sink.append(line)
        if on_output is not None:
            try:
                await on_output(line.decode(errors="replace").rstrip("\r\n"))
            except Exception:
                logger.debug("on_output callback failed", exc_info=True)


async def run_command(
    command: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
    on_output: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, str]:
    """Run an external command and return (stdout, stderr) as stripped text.

    The command runs in its own process group; on timeout the whole group is
    killed so no orphaned children (yt-dlp workers, ffmpeg) survive.
    When on_output is given, each stderr/stdout line is streamed to it as it
    arrives (progress reporting); the full output is still returned.
    Raises RuntimeError when the command fails or exceeds the timeout.
    """
    command = [_resolve_program(command[0]), *command[1:]]
    logger.debug("Running command | cwd=%s command=%s", cwd, redact_command(command))
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _communicate() -> tuple[bytes, bytes]:
        if on_output is None:
            # yt-dlp --dump-single-json can emit megabytes on one line.
            # communicate() drains chunks without readline's 64 KiB limit.
            return await process.communicate()
        await asyncio.gather(
            _read_stream(process.stdout, stdout_chunks, on_output),
            _read_stream(process.stderr, stderr_chunks, on_output),
        )
        await process.wait()
        return b"".join(stdout_chunks), b"".join(stderr_chunks)

    try:
        stdout, stderr = await asyncio.wait_for(_communicate(), timeout)
    except asyncio.TimeoutError as exc:
        _kill_process_group(process)
        await process.wait()
        logger.warning(
            "Command timed out | cwd=%s command=%s timeout=%ss",
            cwd,
            redact_command(command),
            timeout,
        )
        raise RuntimeError(
            f"Command timed out after {int(cast(float, timeout))}s: {command[0]}"
        ) from exc
    if process.returncode != 0:
        error_text = (
            stderr.decode(errors="replace").strip()
            or stdout.decode(errors="replace").strip()
            or f"{command[0]} failed"
        )
        logger.warning(
            "Command failed | cwd=%s command=%s error=%s",
            cwd,
            redact_command(command),
            error_text.splitlines()[0] if error_text else "-",
        )
        raise RuntimeError(error_text)
    return (
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )
