from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
from config import Settings

from services.direct_downloads import download_direct_file
from utils.models import DownloadOption, FileTooLargeError, ParsedInput


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        content_length: int,
        *,
        status: int = 200,
        content_type: str = "application/octet-stream",
        content_range: str | None = None,
    ):
        self._chunks = chunks
        self.status = status
        self.headers = {
            "Content-Length": str(content_length),
            "Content-Type": content_type,
        }
        if content_range is not None:
            self.headers["Content-Range"] = content_range

    def raise_for_status(self) -> None:
        pass

    async def read(self) -> bytes:
        return b"".join(self._chunks)

    class _Content:
        def __init__(self, chunks: list[bytes]):
            self._chunks = chunks

        async def iter_chunked(self, _chunk_size: int):
            for chunk in self._chunks:
                yield chunk

    @property
    def content(self) -> "_Content":
        return self._Content(self._chunks)


class FakeSession:
    """aiohttp.ClientSession stand-in that records the headers of every request.

    ``routes`` is either a single FakeResponse (returned for every request) or a
    callable ``(headers) -> FakeResponse`` for range-aware servers.
    """

    def __init__(self, routes):
        self._routes = routes if callable(routes) else (lambda _headers: routes)
        self.requests: list[dict[str, object]] = []

    def get(self, url: str, proxy: str | None = None, headers=None, timeout=None):
        sent = dict(headers or {})
        self.requests.append(
            {"url": url, "proxy": proxy, "headers": sent, "timeout": timeout}
        )
        return self._FakeGet(self._routes(sent))

    class _FakeGet:
        def __init__(self, response: FakeResponse):
            self._response = response

        async def __aenter__(self) -> FakeResponse:
            return self._response

        async def __aexit__(self, *_: object) -> None:
            return None

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def make_payload(size: int) -> bytes:
    pattern = bytes(range(256))
    return (pattern * (size // len(pattern) + 1))[:size]


def ranged_route(payload: bytes, *, reject_range: bool = False, payload_type="application/octet-stream"):
    """Range-aware server: 206 for range requests, full body otherwise.

    ``reject_range`` models a server that advertises ranges in the probe but
    then answers a real range request with a plain 200 full body.
    """
    total = len(payload)

    def route(headers: dict[str, str]) -> FakeResponse:
        range_header = headers.get("Range")
        if range_header is None:
            return FakeResponse([payload], total, content_type=payload_type)
        start, end = (int(part) for part in range_header.removeprefix("bytes=").split("-"))
        if end == 0:
            # Probe: advertise the full size for every one-byte range request.
            body = payload[:1]
            return FakeResponse(
                [body], len(body), status=206,
                content_type=payload_type,
                content_range=f"bytes 0-0/{total}",
            )
        if reject_range:
            return FakeResponse([payload], total, content_type=payload_type)
        body = payload[start : end + 1]
        return FakeResponse(
            [body],
            len(body),
            status=206,
            content_type=payload_type,
            content_range=f"bytes {start}-{end}/{total}",
        )

    return route


async def run_download(
    monkeypatch,
    tmp_path,
    settings: Settings,
    routes,
    url: str = "https://example.com/f.bin",
):
    session = FakeSession(routes)
    monkeypatch.setattr(
        "services.direct_downloads.aiohttp.ClientSession",
        lambda **kwargs: session,
    )
    status = StatusMessage()
    artifact = await download_direct_file(
        status_message=status,
        parsed_input=ParsedInput(source_url=url),
        option=make_option(),
        settings=settings,
        work_dir=tmp_path / "work",
    )
    return artifact, session, status


def make_settings(tmp_path: Path, max_upload_bytes: int) -> Settings:
    return Settings(
        bot_token="token",
        owner_id=99,
        auth_users={99},
        download_location=tmp_path / "downloads",
        chunk_size=8,
        http_proxy="",
        process_max_timeout=60,
        auto_best_quality=False,
        max_video_height=1080,
        gallery_probe_timeout=15,
        max_upload_bytes=max_upload_bytes,
        verify_ssl=True,
        twitter_cookies="",
        telegram_api_url="",
        telegram_proxy="",
    )


def make_option() -> DownloadOption:
    return DownloadOption(
        option_id="direct",
        label="File",
        send_type="document",
        mode="direct",
    )


class StatusMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)


@pytest.mark.asyncio
async def test_direct_download_aborts_when_content_length_exceeds_limit(
    monkeypatch, tmp_path
):
    response = FakeResponse(chunks=[b"x" * 8], content_length=1024)
    monkeypatch.setattr(
        "services.direct_downloads.aiohttp.ClientSession",
        lambda **kwargs: FakeSession(response),
    )
    settings = make_settings(tmp_path, max_upload_bytes=100)

    with pytest.raises(FileTooLargeError) as exc_info:
        await download_direct_file(
            status_message=StatusMessage(),
            parsed_input=ParsedInput(source_url="https://example.com/f.bin"),
            option=make_option(),
            settings=settings,
            work_dir=tmp_path / "work",
        )

    assert "1024" in str(exc_info.value)
    assert not list((tmp_path / "work").glob("*")) or not any(
        path.stat().st_size > 0 for path in (tmp_path / "work").glob("*")
    )


@pytest.mark.asyncio
async def test_direct_download_aborts_mid_stream_without_content_length(
    monkeypatch, tmp_path
):
    # Content-Length missing (0) so only the streaming guard can fire.
    response = FakeResponse(chunks=[b"x" * 8, b"y" * 8, b"z" * 8], content_length=0)
    monkeypatch.setattr(
        "services.direct_downloads.aiohttp.ClientSession",
        lambda **kwargs: FakeSession(response),
    )
    settings = make_settings(tmp_path, max_upload_bytes=20)

    with pytest.raises(FileTooLargeError) as exc_info:
        await download_direct_file(
            status_message=StatusMessage(),
            parsed_input=ParsedInput(source_url="https://example.com/f.bin"),
            option=make_option(),
            settings=settings,
            work_dir=tmp_path / "work",
        )

    assert "exceeds upload limit" in str(exc_info.value)


@pytest.mark.asyncio
async def test_direct_download_completes_within_limit(monkeypatch, tmp_path):
    response = FakeResponse(chunks=[b"x" * 8, b"y" * 4], content_length=12)
    monkeypatch.setattr(
        "services.direct_downloads.aiohttp.ClientSession",
        lambda **kwargs: FakeSession(response),
    )
    settings = make_settings(tmp_path, max_upload_bytes=100)

    artifact = await download_direct_file(
        status_message=StatusMessage(),
        parsed_input=ParsedInput(source_url="https://example.com/f.bin"),
        option=make_option(),
        settings=settings,
        work_dir=tmp_path / "work",
    )

    assert artifact.path.read_bytes() == b"x" * 8 + b"y" * 4
    assert artifact.send_type == "document"


@pytest.mark.asyncio
async def test_direct_download_rejects_html_page(monkeypatch, tmp_path):
    """A link that answers with text/html is a web page, not media — the
    old behaviour saved the page and uploaded it as a broken document."""
    response = FakeResponse(chunks=[b"<html>page</html>"], content_length=17)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    monkeypatch.setattr(
        "services.direct_downloads.aiohttp.ClientSession",
        lambda **kwargs: FakeSession(response),
    )
    settings = make_settings(tmp_path, max_upload_bytes=100)

    with pytest.raises(RuntimeError) as exc_info:
        await download_direct_file(
            status_message=StatusMessage(),
            parsed_input=ParsedInput(source_url="https://vt.tiktok.com/ZSbj8LgVN/"),
            option=make_option(),
            settings=settings,
            work_dir=tmp_path / "work",
        )

    assert "web page" in str(exc_info.value)
    assert not list((tmp_path / "work").glob("*.html"))


@pytest.mark.asyncio
async def test_direct_download_uses_parallel_ranges(monkeypatch, tmp_path):
    payload = make_payload(4 * 1024 * 1024)
    settings = make_settings(tmp_path, max_upload_bytes=len(payload) * 2)
    settings.download_threads = 4

    artifact, session, status = await run_download(
        monkeypatch, tmp_path, settings, ranged_route(payload)
    )

    assert artifact.path.read_bytes() == payload
    assert artifact.path.stat().st_size == len(payload)
    ranged = [request for request in session.requests if "Range" in request["headers"]]
    assert len(ranged) == 5  # one probe + four segments
    for request in ranged:
        assert request["headers"]["Accept-Encoding"] == "identity"
    assert "100.0%" in status.texts[-1]


@pytest.mark.asyncio
async def test_direct_download_falls_back_when_server_lacks_ranges(monkeypatch, tmp_path):
    payload = make_payload(3 * 1024 * 1024)
    settings = make_settings(tmp_path, max_upload_bytes=len(payload) * 2)
    settings.download_threads = 4

    def route(_: dict[str, str]) -> FakeResponse:
        return FakeResponse([payload], len(payload))

    artifact, session, _ = await run_download(monkeypatch, tmp_path, settings, route)

    assert artifact.path.read_bytes() == payload
    # One range probe (rejected) plus the single-stream attempt.
    assert len(session.requests) == 2
    assert "Range" in session.requests[0]["headers"]
    assert "Range" not in session.requests[-1]["headers"]


@pytest.mark.asyncio
async def test_direct_download_single_thread_skips_probe(monkeypatch, tmp_path):
    payload = make_payload(2 * 1024 * 1024)
    settings = make_settings(tmp_path, max_upload_bytes=len(payload) * 2)
    settings.download_threads = 1

    artifact, session, _ = await run_download(
        monkeypatch, tmp_path, settings, ranged_route(payload)
    )

    assert artifact.path.read_bytes() == payload
    assert len(session.requests) == 1
    assert "Range" not in session.requests[0]["headers"]


@pytest.mark.asyncio
async def test_direct_download_parallel_limit_uses_content_range(monkeypatch, tmp_path):
    payload = make_payload(2 * 1024 * 1024)
    settings = make_settings(tmp_path, max_upload_bytes=1024)
    settings.download_threads = 4

    with pytest.raises(FileTooLargeError) as exc_info:
        await run_download(monkeypatch, tmp_path, settings, ranged_route(payload))

    assert str(len(payload)) in str(exc_info.value)
    assert not list((tmp_path / "work").glob("*"))


@pytest.mark.asyncio
async def test_direct_download_retries_single_stream_after_range_break(
    monkeypatch, tmp_path
):
    payload = make_payload(4 * 1024 * 1024)
    settings = make_settings(tmp_path, max_upload_bytes=len(payload) * 2)
    settings.download_threads = 4

    artifact, session, _ = await run_download(
        monkeypatch, tmp_path, settings, ranged_route(payload, reject_range=True)
    )

    assert artifact.path.read_bytes() == payload
    ranged = [request for request in session.requests if "Range" in request["headers"]]
    assert len(ranged) == 13  # probe + four segments x three attempts, all rejected
    assert "Range" not in session.requests[-1]["headers"]


@pytest.mark.asyncio
async def test_direct_download_segment_resume_after_midstream_failure(
    monkeypatch, tmp_path
):
    """A segment failing mid-stream must resume in place, not restart the file."""
    payload = make_payload(4 * 1024 * 1024)
    settings = make_settings(tmp_path, max_upload_bytes=len(payload) * 2)
    settings.download_threads = 4
    mib = 1024 * 1024
    broken_served = False
    base_route = ranged_route(payload)

    class FailingResponse(FakeResponse):
        @property
        def content(self):
            chunks = self._chunks

            class _FailingContent:
                async def iter_chunked(self, _chunk_size: int):
                    for chunk in chunks:
                        yield chunk
                    raise aiohttp.ClientConnectionError("boom")

            return _FailingContent()

    def flaky_route(headers: dict[str, str]) -> FakeResponse:
        nonlocal broken_served
        if headers.get("Range") == f"bytes={mib}-{2 * mib - 1}" and not broken_served:
            broken_served = True
            return FailingResponse(
                [payload[mib : mib + 100]],
                100,
                status=206,
                content_range=f"bytes {mib}-{2 * mib - 1}/{len(payload)}",
            )
        return base_route(headers)

    artifact, session, _ = await run_download(
        monkeypatch, tmp_path, settings, flaky_route
    )

    assert artifact.path.read_bytes() == payload
    requested_ranges = [request["headers"].get("Range") for request in session.requests]
    assert f"bytes={mib + 100}-{2 * mib - 1}" in requested_ranges

@pytest.mark.asyncio
async def test_direct_download_rejects_html_from_probe(monkeypatch, tmp_path):
    payload = b"<html>page</html>" * (1024 * 1024 // 17 + 1)
    settings = make_settings(tmp_path, max_upload_bytes=len(payload) * 2)
    settings.download_threads = 4

    with pytest.raises(RuntimeError) as exc_info:
        await run_download(
            monkeypatch,
            tmp_path,
            settings,
            ranged_route(payload, payload_type="text/html; charset=utf-8"),
        )

    assert "web page" in str(exc_info.value)
    assert not list((tmp_path / "work").glob("*"))
