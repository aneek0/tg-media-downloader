from pathlib import Path

import pytest
from config import Settings

from services.direct_downloads import download_direct_file
from utils.models import DownloadOption, FileTooLargeError, ParsedInput


class FakeResponse:
    def __init__(self, chunks: list[bytes], content_length: int):
        self._chunks = chunks
        self.headers = {
            "Content-Length": str(content_length),
            "Content-Type": "application/octet-stream",
        }

    def raise_for_status(self) -> None:
        pass

    class _Content:
        def __init__(self, chunks: list[bytes]):
            self._chunks = chunks

        async def iter_chunked(self, chunk_size: int):
            for chunk in self._chunks:
                yield chunk

    @property
    def content(self) -> "_Content":
        return self._Content(self._chunks)


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response

    def get(self, url: str, proxy: str | None = None):
        return self._FakeGet(self._response)

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
