from __future__ import annotations

import pytest

from services.status import StatusSurface


def make_surface(calls: list[str], *, min_interval: float = 0) -> StatusSurface:
    async def edit(text: str, **_: object) -> None:
        calls.append(text)

    return StatusSurface(edit, name="video.mp4", min_interval=min_interval)


@pytest.mark.asyncio
async def test_feed_line_parses_and_skips_duplicates():
    calls: list[str] = []
    surface = make_surface(calls)
    await surface.feed_line(
        "[download]  10.0% of ~100.00MiB at 5.00MiB/s ETA 00:18"
    )
    await surface.feed_line("not a progress line")
    await surface.feed_line(
        "[download]  10.0% of ~100.00MiB at 5.00MiB/s ETA 00:18"
    )
    await surface.feed_line("[download]  55.5% of ~100.00MiB")

    assert len(calls) == 2
    assert "10.0% of 100.00MiB" in calls[0]
    assert "Speed: 5.00MiB/s" in calls[0]
    assert "ETA: 00:18" in calls[0]
    assert "55.5% of 100.00MiB" in calls[1]


@pytest.mark.asyncio
async def test_min_interval_blocks_updates():
    """Pins the bug where the first progress line was throttled away when
    the rate limiter compared against a None timestamp."""
    calls: list[str] = []
    surface = make_surface(calls, min_interval=60)
    await surface.feed_line("[download]  10.0% of ~100.00MiB")
    await surface.feed_line("[download]  20.0% of ~100.00MiB")

    assert len(calls) == 1
    assert "10.0%" in calls[0]


@pytest.mark.asyncio
async def test_forced_progress_bypasses_rate_limit():
    calls: list[str] = []
    surface = make_surface(calls, min_interval=60)
    await surface.progress("first", force=True)
    await surface.progress("second", force=True)

    assert calls == ["first", "second"]


@pytest.mark.asyncio
async def test_feed_line_swallows_edit_errors():
    async def broken_edit(_text: str, **_: object) -> None:
        raise RuntimeError("message to edit not found")

    surface = StatusSurface(broken_edit, name="v.mp4", min_interval=0)
    await surface.feed_line("[download]  10.0% of ~100.00MiB")  # must not raise
    await surface.fail("gone")  # must not raise


@pytest.mark.asyncio
async def test_edit_propagates_errors():
    async def broken_edit(_text: str, **_: object) -> None:
        raise RuntimeError("boom")

    surface = StatusSurface(broken_edit, name="v.mp4")
    with pytest.raises(RuntimeError, match="boom"):
        await surface.edit("text")


@pytest.mark.asyncio
async def test_feed_line_parses_aria2c_lines():
    calls: list[str] = []
    surface = make_surface(calls)
    await surface.feed_line(
        "[#1a2b3c 40.0MiB/200.0MiB(20%) CN:16 DL:9.5MiB ETA:16s]"
    )
    await surface.feed_line("[#1a2b3c 100.0MiB/200.0MiB(50%) CN:16]")

    assert len(calls) == 2
    assert "20% of 200.0MiB" in calls[0]
    assert "Speed: 9.5MiB/s" in calls[0]
    assert "ETA: 16s" in calls[0]
    assert "50% of 200.0MiB" in calls[1]