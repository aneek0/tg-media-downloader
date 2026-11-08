from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import cast

import aiohttp
from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageContent,
    InputTextMessageContent,
)

from config import Settings
from services.delivery import rich_post_html
from services.gallery import (
    _gallery_friendly_error,
    plain_caption,
    probe_gallery,
    small_thumbnail_url,
    split_tweet_media,
)
from services.inline_flow import InlineTaskRegistry, run_inline_download
from services.media_cache import cached_inline_results
from services.mornye import audio_cache_key
from services.parsing import is_twitter_status_url, parse_user_input
from services.requests import inline_gallery_request, inline_media_request
from services.session import Session
from services.sources import VIDEO_EXTENSIONS, SourceKind, classify
from services.ytdlp import probe_url
from utils import text
from utils.callbacks import GalleryNavCallback, InlineCancelCallback
from utils.keyboards import gallery_keyboard
from utils.logging_config import safe_url_label
from utils.models import InlineGalleryPayload, ParsedInput

router = Router(name="inline")
logger = logging.getLogger(__name__)

_inline_tasks = InlineTaskRegistry()


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


def _description_for() -> str:
    return "Download this link and send the media here."


def _probe_caption(file_dicts: list[dict], content: str | None, source_url: str) -> str:
    """Caption in the classic inline-bot format: source URL, then
    'author_nick : post content'. Uses plain_caption() so HTML-tagged
    content (pawchive <p>...</p>) is stripped; falls back to the
    extractor's 'username' (pawchive puts the author there)."""
    lines = [source_url]
    author = file_dicts[0].get("author") or {} if file_dicts else {}
    nick = (
        author.get("nick")
        or author.get("name")
        or (file_dicts[0].get("username") if file_dicts else None)
    )
    nick = (nick or "").strip()
    body = plain_caption(content) or ""
    if nick and body:
        lines.append(f"{nick} : {body}")
    elif nick:
        lines.append(nick)
    elif body:
        lines.append(body)
    return "\n".join(lines)


def _cancel_keyboard(token: str) -> InlineKeyboardMarkup:
    """Keyboard with a single Cancel button. A keyboard is REQUIRED for
    Telegram to include inline_message_id in the chosen_inline_result
    update - without it the sent message cannot be edited into the
    media later."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data=InlineCancelCallback(token=token).pack(),
                )
            ]
        ]
    )


def _inline_photo_results(file_dicts: list[dict], caption: str) -> list:
    """Photo results straight from gallery-dl probe data: Telegram itself
    fetches the twitter CDN images, so photos need no download step."""
    results: list = []
    photos, _ = split_tweet_media(file_dicts)
    for index, media in enumerate(photos, start=1):
        media_url = media.get("_url")
        if not media_url:
            continue
        results.append(
            InlineQueryResultPhoto(
                id=f"photo{index}",
                photo_url=media_url,
                thumbnail_url=small_thumbnail_url(media_url) or media_url,
                caption=caption if index == 1 else None,
            )
        )
    return results


_INLINE_VIDEO_MAX_BYTES = 16 * 1024 * 1024  # Telegram's URL-fetcher limit (~20MB, keep margin)


async def _video_poster_url(parsed: ParsedInput, settings: Settings) -> str | None:
    """Poster/thumbnail image URL for a video page (yt-dlp probe), or None.
    Telegram fetches pbs.twimg.com posters itself, so the inline result
    can be a real photo message that later edits into the video."""
    if not is_twitter_status_url(parsed.source_url):
        return None
    try:
        info = await probe_url(parsed, settings)
    except (RuntimeError, ValueError):
        return None
    poster = info.get("thumbnail")
    return poster if isinstance(poster, str) and poster.startswith("http") else None


def _poster_photo_result(
    token: str, poster_url: str, caption: str | None
) -> InlineQueryResultPhoto:
    """Inline result for a big video: its poster photo. A media message can
    be edited into the video later (editMessageMedia), unlike a text
    article - that edit is what used to fail silently."""
    return InlineQueryResultPhoto(
        id=token,
        photo_url=poster_url,
        thumbnail_url=small_thumbnail_url(poster_url) or poster_url,
        caption=(caption or None),
        reply_markup=_cancel_keyboard(token),
    )


async def _content_length(url: str) -> int | None:
    """Content-Length of a media URL, or None when unreachable."""
    try:
        async with aiohttp.ClientSession() as session, session.head(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            if response.status != 200:
                return None
            return response.content_length
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def _inline_video_results(
    file_dicts: list[dict],
    *,
    parsed: ParsedInput,
    session: Session,
    description: str,
    caption: str | None,
) -> list:
    """Small videos become real InlineQueryResultVideo results: Telegram's
    own fetcher pulls the CDN URL (works again since Oct 2026; verified by
    a live sendVideo-by-URL), so tapping posts the video instantly - no
    bot download, no edit, no DM. The URL fetcher caps around 20MB, so
    anything above _INLINE_VIDEO_MAX_BYTES falls back to the poster-photo
    flow: the result is the video's poster image (a real media message),
    the background download swaps it for the video via editMessageMedia
    (media -> media edits are allowed; text -> media are NOT - that was
    the silent 'invalid message content specified' hang)."""
    results: list = []
    for media in file_dicts:
        if (media.get("extension") or "").lower() not in VIDEO_EXTENSIONS:
            continue
        url = media.get("_url")
        if not url:
            continue
        size = await _content_length(url)
        if size is not None and size <= _INLINE_VIDEO_MAX_BYTES:
            extension = (media.get("extension") or "mp4").lower()
            mime_type = "video/mp4" if extension != "webm" else "video/webm"
            results.append(
                InlineQueryResultVideo(
                    id=f"video{len(results) + 1}",
                    video_url=url,
                    mime_type=mime_type,
                    title="Video",  # required by the Bot API schema
                    thumbnail_url=url,
                    caption=(caption or None) if len(results) == 0 else None,
                )
            )
            continue

        # gallery-dl names twitter downloads "{tweet_id}_{num}.{extension}"
        # (GALLERY_FILENAME_FORMAT). That name identifies the tapped item
        # among the downloaded artifacts; probe order alone does not, since
        # photos and videos are downloaded in mixed-media albums too.
        preferred_name = None
        tweet_id = media.get("tweet_id")
        num = media.get("num")
        extension = (media.get("extension") or "mp4").lower()
        if tweet_id and num and extension:
            preferred_name = f"{tweet_id}_{num}.{extension}"
        poster = await _video_poster_url(parsed, session.settings)
        stored = inline_media_request(
            session.request_store,
            parsed,
            preferred_name=preferred_name,
            poster_url=poster,
            # editMessageMedia on an inline message cannot upload a new
            # file (Bot API rule) - the media must be a URL or a known
            # file_id. The CDN URL is kept so the inline edit can hand it
            # to Telegram to fetch itself.
            video_url=url,
        )
        token = stored.token
        results.append(
            _poster_photo_result(
                token,
                poster,
                caption if len(results) == 0 else None,
            )
            if poster
            else InlineQueryResultArticle(
                id=token,
                title=f"Video {len(results) + 1}",
                description=description[:120],
                input_message_content=InputTextMessageContent(
                    message_text=text.PROCESSING
                ),
                reply_markup=_cancel_keyboard(token),
            )
        )
    return results


def _gallery_result(
    token: str, photos: list[str], caption: str | None
) -> InlineQueryResultPhoto:
    """Gallery result: first photo + a 'prev/next' keyboard. The keyboard
    forces Telegram to include inline_message_id, which the navigation
    callback needs to edit the message media."""
    return InlineQueryResultPhoto(
        id=f"gallery:{token}",
        photo_url=photos[0],
        thumbnail_url=small_thumbnail_url(photos[0]) or photos[0],
        caption=caption,
        reply_markup=gallery_keyboard(token=token, count=len(photos), index=0),
    )


def _rich_results(
    full_caption: str, photo_urls: list[str]
) -> list[InlineQueryResultArticle]:
    """Rich results for posts that overflow the 1024-character caption limit
    of photo results: one message with the images and the complete text."""
    if not photo_urls or len(full_caption) <= text.CAPTION_LIMIT:
        return []
    return [
        InlineQueryResultArticle(
            id="rich",
            title="Full post",
            description=full_caption[:120],
            input_message_content=InputRichMessageContent(
                rich_message=InputRichMessage(
                    html=rich_post_html(photo_urls, text.esc(full_caption))
                )
            ),
        )
    ]



async def _error_article(message: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id="error",
        title="Cannot download",
        description=message[:120],
        input_message_content=InputTextMessageContent(message_text=message),
    )


async def _answer_gallery_media(
    query: InlineQuery,
    parsed: ParsedInput,
    session: Session,
) -> bool:
    """Answer with gallery-dl CDN results. Returns False when the caller
    should fall through to the generic token-article path."""
    settings = session.settings
    request_store = session.request_store
    probe = await probe_gallery(parsed, settings)
    logger.info(
        "Inline gallery probe | user=%s source=%s files=%s error=%s",
        query.from_user.id,
        safe_url_label(parsed.source_url),
        len(probe.file_dicts),
        (probe.error or "-")[:120],
    )
    twitter = is_twitter_status_url(parsed.source_url)
    body = plain_caption(probe.content)
    if twitter and not probe.error and not probe.file_dicts and body:
        chunks = text.split_message(body)
        await query.answer(
            [
                InlineQueryResultArticle(
                    id=f"text:{index}",
                    title="Post text" if len(chunks) == 1 else f"Post text {index + 1}/{len(chunks)}",
                    description=chunk[:120],
                    input_message_content=InputTextMessageContent(
                        message_text=chunk,
                        parse_mode=None,
                    ),
                )
                for index, chunk in enumerate(chunks)
            ],
            cache_time=0,
            is_personal=True,
        )
        return True
    if probe.error or not probe.file_dicts:
        if twitter and probe.error and "Unsupported URL" not in probe.error:
            friendly = _gallery_friendly_error(probe.error, bool(settings.twitter_cookies))
            await query.answer(
                [await _error_article(friendly)], cache_time=0, is_personal=True
            )
            return True
        return False
    photos, _ = split_tweet_media(probe.file_dicts)
    if twitter:
        # Twitter status: photos stay instant CDN results (Telegram fetches
        # pbs.twimg.com itself); with >=2 photos the first result is a
        # gallery pager. Videos cannot be CDN results - Telegram's fetcher
        # refuses video.twimg.com - so they become download-on-tap articles.
        full_caption = _probe_caption(
            probe.file_dicts, probe.content, parsed.source_url
        )
        caption = full_caption[: text.CAPTION_LIMIT]
        body = plain_caption(probe.content) or ""
        results: list = []
        gallery_photos = [m["_url"] for m in photos if m.get("_url")]
        results.extend(_rich_results(full_caption, gallery_photos))
        if len(gallery_photos) >= 2:
            stored = inline_gallery_request(
                request_store, parsed, gallery_photos, caption
            )
            token = stored.token
            results.append(_gallery_result(token, gallery_photos, caption))
        results.extend(_inline_photo_results(probe.file_dicts, caption))
        results.extend(
            await _inline_video_results(
                probe.file_dicts,
                parsed=parsed,
                session=session,
                description=body or _description_for(),
                caption=caption,
            )
        )
        if results:
            await query.answer(results, cache_time=0, is_personal=True)
            return True
        await query.answer(
            [await _error_article("No media found in this tweet")],
            cache_time=0,
            is_personal=True,
        )
        return True
    preview_urls = [
        (small_thumbnail_url(m["_url"]) or m["_url"])
        for m in photos
        if m.get("_url")
    ]
    if not preview_urls:
        return False  # videos/attachments only - generic article path
    full_caption = _probe_caption(probe.file_dicts, probe.content, parsed.source_url)
    caption = full_caption[: text.CAPTION_LIMIT]
    results: list = []
    results.extend(_rich_results(full_caption, preview_urls))
    if len(preview_urls) >= 2:
        # Preview pager over thumbnails (Telegram CAN fetch the thumbnail
        # CDN even when the full files sit behind DDoS-Guard).
        stored = inline_gallery_request(
            request_store, parsed, preview_urls, caption
        )
        token = stored.token
        results.append(_gallery_result(token, preview_urls, caption))
    for index, preview in enumerate(preview_urls):
        # result_id = token -> chosen_inline_result starts the background
        # download that swaps the small preview for the full-size file.
        # preferred_index puts the tapped photo into the message, the
        # remaining files go to the DM spillover.
        stored = inline_media_request(
            request_store, parsed, preferred_index=index
        )
        token = stored.token
        results.append(
            InlineQueryResultPhoto(
                id=token,
                photo_url=preview,
                thumbnail_url=preview,
                caption=caption if index == 0 else None,
                reply_markup=_cancel_keyboard(token),
            )
        )
    await query.answer(results, cache_time=0, is_personal=True)
    return True


@router.inline_query()
async def inline_query_handler(  # noqa: C901
    query: InlineQuery,
    session: Session,
) -> None:
    request_store = session.request_store
    media_cache = session.media_cache
    mornye = session.mornye
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
    source = classify(parsed)

    preferences = session.preferences(query.from_user.id)
    cache_key = audio_cache_key(
        parsed.source_url, preferences.audio_quality, preferences.audio_format
    )
    cached = media_cache.get(cache_key)
    if cached:
        results = cached_inline_results(cached)
        if results:
            cached_photos = [entry for entry in cached if entry.send_type == "photo"]
            if len(cached_photos) >= 2:
                # Same pager as the fresh-probe path, but over cached
                # file_ids (edit_message_media accepts file_id media).
                raw_caption = (cached_photos[0].caption or "")[:1000]
                caption = (
                    raw_caption
                    if parsed.source_url in raw_caption
                    else (f"{parsed.source_url}\n{raw_caption}" if raw_caption else parsed.source_url)
                )[:1024]
                stored = inline_gallery_request(
                    request_store,
                    parsed,
                    [entry.file_id for entry in cached_photos],
                    caption,
                )
                token = stored.token
                results.insert(
                    0,
                    InlineQueryResultCachedPhoto(
                        id=f"gallery:{token}",
                        photo_file_id=cached_photos[0].file_id,
                        caption=caption,
                        reply_markup=gallery_keyboard(
                            token=token, count=len(cached_photos), index=0
                        ),
                    ),
                )
            await query.answer(results, cache_time=0, is_personal=True)
            return

    if source.kind is SourceKind.IMAGE and not source.is_twitter:
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

    if mornye and mornye.enabled and source.kind is SourceKind.MUSIC:
        try:
            target = await mornye.resolve_url(parsed.source_url)
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Inline spotify resolve failed | user=%s source=%s error=%s",
                query.from_user.id,
                safe_url_label(parsed.source_url),
                exc,
            )
            await query.answer(
                [await _error_article(text.INLINE_FAILED)], cache_time=0
            )
            return
        title = target.name
        description = (
            "Music track — tap to download"
            if target.kind == "track"
            else f"Music {target.kind} — {len(target.tracks)} track(s), tap to download"
        )
        stored = inline_media_request(request_store, parsed)
        token = stored.token
        await query.answer(
            [
                InlineQueryResultArticle(
                    id=token,
                    title=text.esc(title),
                    description=description,
                    thumbnail_url=target.cover_url or None,
                    input_message_content=InputTextMessageContent(
                        message_text=text.PROCESSING,
                    ),
                    reply_markup=_cancel_keyboard(token),
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    if await _answer_gallery_media(query, parsed, session):
        return

    # result_id doubles as the request token: ChosenInlineResult carries it back
    stored = inline_media_request(request_store, parsed)
    token = stored.token
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
                description=_description_for(),
                input_message_content=InputTextMessageContent(
                    message_text=text.PROCESSING,
                ),
                reply_markup=_cancel_keyboard(token),
            )
        ],
        cache_time=0,
        is_personal=True,
    )


def _result_needs_no_download(token: str) -> bool:
    """True for ready results that must never start a background download."""
    return token in {"help", "rich"} or token.startswith(
        ("cached", "gallery:", "video", "text:")
    )


@router.chosen_inline_result()
async def inline_chosen_handler(
    chosen: ChosenInlineResult,
    session: Session,
) -> None:
    token = chosen.result_id
    inline_message_id = chosen.inline_message_id
    if not inline_message_id or _result_needs_no_download(token):
        return

    # aiogram binds a bot to every dispatched update.
    bot = cast(Bot, chosen.bot)
    stored = session.request_store.load(token)
    if not stored:
        logger.warning(
            "Expired inline request | user=%s token=%s", chosen.from_user.id, token
        )
        with contextlib.suppress(TelegramAPIError):
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=text.REQUEST_EXPIRED,
            )
        return

    task = asyncio.create_task(
        run_inline_download(
            session,
            bot=bot,
            user_id=chosen.from_user.id,
            inline_message_id=inline_message_id,
            stored=stored,
        )
    )
    _inline_tasks.start(token, task)


@router.callback_query(InlineCancelCallback.filter())
async def inline_cancel_callback(
    query: CallbackQuery,
    callback_data: InlineCancelCallback,
    session: Session,
) -> None:
    request_store = session.request_store
    token = callback_data.token
    task = _inline_tasks.cancel(token)
    if task:
        logger.info("Inline request cancelled | user=%s token=%s", query.from_user.id, token)
        message_text = "Cancelled."
    else:
        message_text = "Nothing to cancel."

    if query.inline_message_id:
        # aiogram binds a bot to every dispatched update.
        bot = cast(Bot, query.bot)
        with contextlib.suppress(TelegramAPIError):
            await bot.edit_message_text(
                inline_message_id=query.inline_message_id,
                text=message_text,
            )
    request_store.delete(token)
    await query.answer()


@router.callback_query(GalleryNavCallback.filter())
async def gallery_nav_callback(
    query: CallbackQuery,
    callback_data: GalleryNavCallback,
    session: Session,
) -> None:
    request_store = session.request_store
    stored = request_store.load(callback_data.token)
    payload = stored.payload if stored else None
    photos = payload.photos if isinstance(payload, InlineGalleryPayload) else []
    if not query.inline_message_id or not photos:
        await query.answer("This gallery is expired. Send the link again.")
        return
    index = callback_data.index % len(photos)
    # aiogram binds a bot to every dispatched update.
    bot = cast(Bot, query.bot)
    try:
        await bot.edit_message_media(
            inline_message_id=query.inline_message_id,
            media=InputMediaPhoto(
                media=photos[index],
                caption=(
                    payload.caption
                    if isinstance(payload, InlineGalleryPayload)
                    else None
                ),
                parse_mode=None,
            ),
            reply_markup=gallery_keyboard(
                token=callback_data.token, count=len(photos), index=index
            ),
        )
    except TelegramAPIError:
        await query.answer("Failed to update the gallery.")
        return
    await query.answer()
