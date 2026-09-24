from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    InputTextMessageContent,
)
from services.gallery import (
    _gallery_friendly_error,
    _gallery_probe_command,
    _parse_gallery_probe,
    download_gallery_media,
)
from services.parsing import is_twitter_status_url, parse_user_input
from services.request_store import RequestStore
from services.telegram_uploads import _media_item
from services.ytdlp import _ext_from_url, _run_command, build_direct_options
from utils.callbacks import InlineCancelCallback
from utils.logging_config import safe_url_label
from utils import text
from utils.models import DownloadArtifact, ParsedInput, StoredRequest

router = Router(name="inline")
logger = logging.getLogger(__name__)

_active_inline_tasks: dict[str, asyncio.Task] = {}
INLINE_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXT_SET = {"mp4", "webm", "mov"}

def _first_url_in(raw: str) -> str:
    """Extract the first http(s) URL from pasted text (URL + caption)."""
    start = -1
    for scheme in ("https://", "http://"):
        idx = raw.find(scheme)
        if idx != -1 and (start == -1 or idx < start):
            start = idx
    if start == -1:
        return raw
    rest = raw[start:]
    end = len(rest)
    for ch in (" ", "\n", "\t"):
        idx = rest.find(ch)
        if idx != -1:
            end = min(end, idx)
    return rest[:end].rstrip(".,;:!?)]}\"'>")


def _description_for(parsed: ParsedInput) -> str:
    return "Download this link and send the media here."


def _tweet_caption(file_dicts: list[dict], content: str | None, source_url: str) -> str:
    """Caption in the classic inline-bot format: status URL, then
    'author_nick : tweet content'."""
    lines = [source_url]
    author = file_dicts[0].get("author") or {} if file_dicts else {}
    nick = (author.get("nick") or author.get("name") or "").strip()
    body = (content or "").strip()
    if nick and body:
        lines.append(f"{nick} : {body}")
    elif nick:
        lines.append(nick)
    elif body:
        lines.append(body)
    return "\n".join(lines)


def _inline_media_results(
    file_dicts: list[dict], content: str | None, source_url: str
) -> list:
    """Build inline results straight from gallery-dl probe data, so Telegram
    itself fetches and renders tweet photos from the twitter CDN - classic
    inline-bot UX. Photos become direct CDN results; video items are listed
    separately. The caption follows the classic format: URL, then
    'author : tweet content'."""
    results: list = []
    caption = _tweet_caption(file_dicts, content, source_url)[:1024]
    photos = [m for m in file_dicts if (m.get("extension") or "").lower() not in VIDEO_EXT_SET]
    videos = [m for m in file_dicts if (m.get("extension") or "").lower() in VIDEO_EXT_SET]
    photo_thumb = None
    for media in photos:
        url = media.get("_url")
        if url:
            photo_thumb = url
            break
    for index, media in enumerate(photos, start=1):
        media_url = media.get("_url")
        if not media_url:
            continue
        results.append(
            InlineQueryResultPhoto(
                id=f"photo{index}",
                photo_url=media_url,
                thumbnail_url=media_url,
                caption=caption if index == 1 else None,
            )
        )
    for index, media in enumerate(videos, start=1):
        media_url = media.get("_url")
        if not media_url:
            continue
        thumb = photo_thumb or media_url
        results.append(
            InlineQueryResultVideo(
                id=f"video{index}",
                video_url=media_url,
                thumbnail_url=thumb,
                mime_type="video/mp4",
                title=f"Video {index}",
                caption=caption if not photos and index == 1 else None,
            )
        )
    return results


@router.inline_query()
async def inline_query_handler(
    query: InlineQuery,
    settings: Settings,
    request_store: RequestStore,
) -> None:
    raw = (query.query or "").strip()
    if not raw:
        await query.answer(
            [
                InlineQueryResultArticle(
                    id="help",
                    title="Send me a link",
                    description="Type a URL after the bot name to download it.",
                    input_message_content=InputTextMessageContent(
                        message_text=(
                            "Send me a direct link, a Twitter/X status URL, "
                            "or any supported media URL — I will download it "
                            "and send the media back."
                        ),
                    ),
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    if "http://" not in raw and "https://" not in raw:
        return

    # Users often paste a whole message (URL + caption text); extract the
    # first URL from it instead of treating everything as one URL.
    raw = _first_url_in(raw)
    try:
        parsed = parse_user_input(raw)
    except ValueError:
        logger.info("Inline query ignored (no URL) | user=%s", query.from_user.id)
        return

    ext = _ext_from_url(parsed.source_url)
    if ext and ext.lower() in INLINE_IMAGE_EXTENSIONS and not is_twitter_status_url(
        parsed.source_url
    ):
        await query.answer(
            [
                InlineQueryResultPhoto(
                    id="photo",
                    photo_url=parsed.source_url,
                    thumbnail_url=parsed.source_url,
                )
            ],
            cache_time=300,
        )
        return

    if is_twitter_status_url(parsed.source_url):
        # Probe the tweet right here and hand Telegram the direct twitter
        # CDN links: the picked result renders as real media instantly,
        # with no server-side download (classic inline-bot UX).
        try:
            stdout, _ = await _run_command(
                _gallery_probe_command(parsed, settings)
            )
            file_dicts, content, error = _parse_gallery_probe(stdout)
        except RuntimeError as exc:
            file_dicts, content, error = [], None, str(exc)
        if error or not file_dicts:
            friendly = _gallery_friendly_error(
                error or "no media", bool(settings.twitter_cookies)
            )
            await query.answer(
                [await _error_article(query, friendly)],
                cache_time=0,
                is_personal=True,
            )
            return
        results = _inline_media_results(file_dicts, content, parsed.source_url)
        if not results:
            await query.answer(
                [await _error_article(query, "No media found in this tweet")],
                cache_time=0,
                is_personal=True,
            )
            return
        await query.answer(results, cache_time=0, is_personal=True)
        return

    # result_id doubles as the request token: ChosenInlineResult carries it back
    token = request_store.create_token()
    stored = StoredRequest(
        token=token,
        request_type="inline_media",
        parsed_input=parsed,
        options=[],
    )
    request_store.save(stored)
    logger.info(
        "Prepared inline request | user=%s token=%s source=%s",
        query.from_user.id,
        token,
        safe_url_label(parsed.source_url),
    )
    await query.answer(
        [
            InlineQueryResultArticle(
                id=token,
                title=text.INLINE_TITLE,
                description=_description_for(parsed),
                input_message_content=InputTextMessageContent(
                    message_text=text.PROCESSING,
                ),
                # A keyboard (even a no-op one) is REQUIRED for Telegram to
                # include inline_message_id in the chosen_inline_result
                # update - without it the sent message cannot be edited into
                # media later. The button also lets the user cancel.
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Cancel",
                                callback_data=InlineCancelCallback(token=token).pack(),
                            )
                        ]
                    ]
                ),
            )
        ],
        cache_time=0,
        is_personal=True,
    )


async def _error_article(query: InlineQuery, message: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="error",
        title="Cannot download",
        description=message[:120],
        input_message_content=InputTextMessageContent(message_text=message),
    )


async def _dm_send_artifacts(
    bot: Bot, user_id: int, artifacts: list[DownloadArtifact]
) -> None:
    for start in range(0, len(artifacts), 10):
        chunk = artifacts[start : start + 10]
        if len(chunk) == 1:
            artifact = chunk[0]
            if artifact.send_type == "photo":
                await bot.send_photo(
                    chat_id=user_id, photo=artifact.path, caption=artifact.caption
                )
            elif artifact.send_type == "video":
                await bot.send_video(
                    chat_id=user_id,
                    video=artifact.path,
                    caption=artifact.caption,
                    supports_streaming=True,
                )
            else:
                await bot.send_document(
                    chat_id=user_id,
                    document=artifact.path,
                    caption=artifact.caption,
                )
        else:
            await bot.send_media_group(
                chat_id=user_id,
                media=[
                    _media_item(
                        artifact,
                        artifacts[0].caption if start == 0 and index == 0 else None,
                    )
                    for index, artifact in enumerate(chunk)
                ],
            )


async def _run_inline_download(
    *,
    bot: Bot,
    user_id: int,
    inline_message_id: str,
    stored: StoredRequest,
    settings: Settings,
    request_store: RequestStore,
) -> None:
    token = stored.token

    await bot.edit_message_text(
        inline_message_id=inline_message_id,
        text=text.DOWNLOAD_START.format(name=stored.parsed_input.source_url),
    )

    try:
        if is_twitter_status_url(stored.parsed_input.source_url):
            artifacts = await download_gallery_media(
                parsed_input=stored.parsed_input,
                settings=settings,
                work_dir=request_store.work_directory(token),
            )
        else:
            option = build_direct_options(stored.parsed_input)[0]
            artifacts = [
                await download_direct_file(
                    status_message=_InlineStatus(bot, inline_message_id),
                    parsed_input=stored.parsed_input,
                    option=option,
                    settings=settings,
                    work_dir=request_store.work_directory(token),
                )
            ]
    except asyncio.CancelledError:
        # user hit Cancel: the cancel callback already edited the message
        # and removed the token; just stop quietly
        logger.info(
            "Inline download cancelled | user=%s token=%s", user_id, token
        )
        raise
    except Exception as exc:  # pylint: disable=broad-exception-caught  # pragma: no cover - user-facing safety boundary
        logger.exception(
            "Inline request failed | user=%s token=%s", user_id, token
        )
        try:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=f"{text.INLINE_FAILED}\n<code>{exc}</code>",
            )
        except TelegramAPIError:
            pass
        request_store.delete(token)
        return

    dm_note = ""
    rest = artifacts[1:]
    if rest:
        try:
            await _dm_send_artifacts(bot, user_id, rest)
            dm_note = f"\n(+{len(rest)} more in your private chat)"
        except TelegramAPIError as exc:
            logger.info(
                "Inline DM delivery failed | user=%s error=%s", user_id, exc
            )
            dm_note = f"\n(+{len(rest)} more files — open the bot and send the link)"

    first = artifacts[0]
    caption = (first.caption or first.file_name)[:1000] + dm_note
    try:
        await bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=_media_item(first, caption),
        )
    except TelegramAPIError as exc:
        logger.exception(
            "Inline media edit failed | user=%s token=%s error=%s",
            user_id,
            token,
            exc,
        )
    logger.info(
        "Inline request complete | user=%s token=%s files=%s dm_rest=%s",
        user_id,
        token,
        len(artifacts),
        len(rest),
    )
    request_store.delete(token)


@router.chosen_inline_result()
async def inline_chosen_handler(
    chosen: ChosenInlineResult,
    settings: Settings,
    request_store: RequestStore,
) -> None:
    token = chosen.result_id
    inline_message_id = chosen.inline_message_id
    if not inline_message_id or token == "help":
        return

    stored = request_store.load(token)
    if not stored:
        logger.warning(
            "Expired inline request | user=%s token=%s", chosen.from_user.id, token
        )
        try:
            await chosen.bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=text.REQUEST_EXPIRED,
            )
        except TelegramAPIError:
            pass
        return

    task = asyncio.create_task(
        _run_inline_download(
            bot=chosen.bot,
            user_id=chosen.from_user.id,
            inline_message_id=inline_message_id,
            stored=stored,
            settings=settings,
            request_store=request_store,
        )
    )
    _active_inline_tasks[token] = task
    task.add_done_callback(lambda _: _active_inline_tasks.pop(token, None))


class _InlineStatus:
    """Duck-typed status-message shim: lets download_direct_file edit the
    inline message's text while it streams progress updates."""

    def __init__(self, bot: Bot, inline_message_id: str) -> None:
        self._bot = bot
        self._inline_message_id = inline_message_id

    async def edit_text(self, message_text: str, **_: object) -> None:
        await self._bot.edit_message_text(
            inline_message_id=self._inline_message_id,
            text=message_text,
        )


@router.callback_query(InlineCancelCallback.filter())
async def inline_cancel_callback(
    query: CallbackQuery,
    callback_data: InlineCancelCallback,
    request_store: RequestStore,
) -> None:
    token = callback_data.token
    task = _active_inline_tasks.get(token)
    if task and not task.done():
        task.cancel()
        logger.info("Inline request cancelled | user=%s token=%s", query.from_user.id, token)
        message_text = "Cancelled."
    else:
        message_text = "Nothing to cancel."

    if query.inline_message_id:
        try:
            await query.bot.edit_message_text(
                inline_message_id=query.inline_message_id,
                text=message_text,
            )
        except TelegramAPIError:
            pass
    request_store.delete(token)
    await query.answer()
