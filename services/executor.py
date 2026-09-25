from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import Message

from config import Settings
from services.direct_downloads import download_direct_file
from services.gallery import download_gallery_media
from services.media_cache import MediaCache
from services.progress import StatusProgress, humanbytes
from services.request_store import RequestStore
from services.telegram_uploads import upload_artifact, upload_artifacts
from services.thumbnail_store import ThumbnailStore
from services.ytdlp import (
    download_best_quality,
    download_quick_youtube,
    download_selected_format,
)
from utils import text
from utils.logging_config import safe_url_label
from utils.models import DownloadOption, FileTooLargeError, StoredRequest

logger = logging.getLogger(__name__)


async def execute_request(
    *,
    stored: StoredRequest,
    option: DownloadOption,
    bot: Bot,
    status_message: Message,
    source_message: Message,
    user_id: int,
    settings: Settings,
    request_store: RequestStore,
    thumbnail_store: ThumbnailStore,
    media_cache: MediaCache,
) -> None:
    started_at = datetime.now()
    work_dir = request_store.work_directory(stored.token)

    file_name = stored.parsed_input.custom_file_name or "downloaded-file"
    safe_name = text.esc(file_name)
    logger.info(
        "Starting request action | user=%s token=%s type=%s option=%s send_type=%s source=%s",
        user_id,
        stored.token,
        stored.request_type,
        option.option_id,
        option.send_type,
        safe_url_label(stored.parsed_input.source_url),
    )
    try:
        await status_message.edit_text(text.download_caption(safe_name))
        progress = StatusProgress(status_message.edit_text, safe_name)
        if stored.request_type == "gallery_media":
            artifacts = await download_gallery_media(
                parsed_input=stored.parsed_input,
                settings=settings,
                work_dir=work_dir,
            )
            uploaded = await upload_artifacts(
                bot=bot,
                status_message=status_message,
                source_message=source_message,
                artifacts=artifacts,
                started_at=started_at,
                thumbnail_path=thumbnail_store.get(user_id),
            )
            if uploaded:
                await media_cache.record(stored.parsed_input.source_url, uploaded)
            logger.info(
                "Completed request action | user=%s token=%s files=%s",
                user_id,
                stored.token,
                len(artifacts),
            )
            return
        if stored.request_type == "direct_download":
            artifact = await download_direct_file(
                status_message=status_message,
                parsed_input=stored.parsed_input,
                option=option,
                settings=settings,
                work_dir=work_dir,
                suggested_ext=stored.info.get("ext"),
            )
        elif stored.request_type == "youtube_quick":
            artifact = await download_quick_youtube(
                parsed_input=stored.parsed_input,
                option=option,
                settings=settings,
                work_dir=work_dir,
                progress=progress,
            )
        elif stored.request_type == "ytdlp_auto":
            artifact = await download_best_quality(
                parsed_input=stored.parsed_input,
                settings=settings,
                work_dir=work_dir,
                info=stored.info,
                progress=progress,
            )
        else:
            artifact = await download_selected_format(
                parsed_input=stored.parsed_input,
                option=option,
                info=stored.info,
                settings=settings,
                work_dir=work_dir,
                progress=progress,
            )

        uploaded = await upload_artifact(
            bot=bot,
            status_message=status_message,
            source_message=source_message,
            artifact=artifact,
            thumbnail_path=thumbnail_store.get(user_id),
            started_at=started_at,
        )
        if uploaded:
            await media_cache.record(stored.parsed_input.source_url, [uploaded])
        logger.info(
            "Completed request action | user=%s token=%s file=%s send_type=%s",
            user_id,
            stored.token,
            artifact.file_name,
            artifact.send_type,
        )
    except FileTooLargeError as exc:
        logger.warning(
            "Download exceeded upload limit | user=%s token=%s type=%s option=%s error=%s",
            user_id,
            stored.token,
            stored.request_type,
            option.option_id,
            exc,
        )
        await status_message.edit_text(
            text.TOO_LARGE.format(
                size=humanbytes(settings.max_upload_bytes),
            )
        )
        return
    except Exception as exc:  # pylint: disable=broad-exception-caught  # pragma: no cover - user-facing safety boundary
        logger.exception(
            "Request action failed | user=%s token=%s type=%s option=%s",
            user_id,
            stored.token,
            stored.request_type,
            option.option_id,
        )
        await status_message.edit_text(
            f"{text.DOWNLOAD_FAILED}\n<code>{text.esc(str(exc))}</code>"
        )
    finally:
        request_store.delete(stored.token)
        logger.info("Cleaned request state | token=%s", stored.token)
