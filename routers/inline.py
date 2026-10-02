from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultPhoto,
    InputMediaPhoto,
    InputTextMessageContent,
    InlineQuery,
)
from config import Settings
from services.gallery import (
    VIDEO_EXTENSIONS,
    _gallery_friendly_error,
    plain_caption,
    probe_gallery,
    small_thumbnail_url,
    split_tweet_media,
)
from services.inline_flow import InlineTaskRegistry, run_inline_download
from services.media_cache import MediaCache, cached_inline_results
from services.parsing import is_twitter_status_url, parse_user_input
from services.request_store import RequestStore
from services.ytdlp import _ext_from_url
from utils.callbacks import GalleryNavCallback, InlineCancelCallback
from utils.keyboards import gallery_keyboard
from utils.logging_config import safe_url_label
from utils import text
from utils.models import ParsedInput, StoredRequest

router = Router(name="inline")
logger = logging.getLogger(__name__)

_inline_tasks = InlineTaskRegistry()
INLINE_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


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


def _inline_video_results(
    file_dicts: list[dict],
    parsed: ParsedInput,
    request_store: RequestStore,
    description: str,
) -> list:
    """Video items become tappable articles, not InlineQueryResultVideo:
    Telegram's own fetcher cannot pull video.twimg.com URLs, so a
    video_url result fails with WEBPAGE_CURL_FAILED on tap. The article
    starts the bot's regular download-and-upload flow instead, via the
    token in its result id."""
    results: list = []
    for media in file_dicts:
        if (media.get("extension") or "").lower() not in VIDEO_EXTENSIONS:
            continue
        if not media.get("_url"):
            continue
        token = request_store.create_token()
        # gallery-dl names twitter downloads "{tweet_id}_{num}.{extension}"
        # (GALLERY_FILENAME_FORMAT). That name identifies the tapped item
        # among the downloaded artifacts; probe order alone does not, since
        # photos and videos are downloaded in mixed-media albums too.
        preferred_name = None
        tweet_id = media.get("tweet_id")
        num = media.get("num")
        extension = (media.get("extension") or "").lower()
        if tweet_id and num and extension:
            preferred_name = f"{tweet_id}_{num}.{extension}"
        request_store.save(
            StoredRequest(
                token=token,
                request_type="inline_media",
                parsed_input=parsed,
                options=[],
                info={"preferred_name": preferred_name},
            )
        )
        results.append(
            InlineQueryResultArticle(
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
    settings: Settings,
    request_store: RequestStore,
) -> bool:
    """Answer with gallery-dl CDN results. Returns False when the caller
    should fall through to the generic token-article path."""
    probe = await probe_gallery(parsed, settings)
    logger.info(
        "Inline gallery probe | user=%s source=%s files=%s error=%s",
        query.from_user.id,
        safe_url_label(parsed.source_url),
        len(probe.file_dicts),
        (probe.error or "-")[:120],
    )
    twitter = is_twitter_status_url(parsed.source_url)
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
        caption = _probe_caption(probe.file_dicts, probe.content, parsed.source_url)[:1024]
        body = plain_caption(probe.content) or ""
        results: list = []
        gallery_photos = [m["_url"] for m in photos if m.get("_url")]
        if len(gallery_photos) >= 2:
            token = request_store.create_token()
            request_store.save(
                StoredRequest(
                    token=token,
                    request_type="inline_gallery",
                    parsed_input=parsed,
                    options=[],
                    info={"photos": gallery_photos, "caption": caption},
                )
            )
            results.append(_gallery_result(token, gallery_photos, caption))
        results.extend(_inline_photo_results(probe.file_dicts, caption))
        results.extend(
            _inline_video_results(
                probe.file_dicts,
                parsed,
                request_store,
                body or _description_for(),
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
    caption = _probe_caption(probe.file_dicts, probe.content, parsed.source_url)[:1024]
    results: list = []
    if len(preview_urls) >= 2:
        # Preview pager over thumbnails (Telegram CAN fetch the thumbnail
        # CDN even when the full files sit behind DDoS-Guard).
        token = request_store.create_token()
        stored = StoredRequest(
            token=token,
            request_type="inline_gallery",
            parsed_input=parsed,
            options=[],
            info={"photos": preview_urls, "caption": caption},
        )
        request_store.save(stored)
        results.append(_gallery_result(token, preview_urls, caption))
    for index, preview in enumerate(preview_urls):
        # result_id = token -> chosen_inline_result starts the background
        # download that swaps the small preview for the full-size file.
        # preferred_index puts the tapped photo into the message, the
        # remaining files go to the DM spillover.
        token = request_store.create_token()
        request_store.save(
            StoredRequest(
                token=token,
                request_type="inline_media",
                parsed_input=parsed,
                options=[],
                info={"preferred_index": index},
            )
        )
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
async def inline_query_handler(
    query: InlineQuery,
    settings: Settings,
    request_store: RequestStore,
    media_cache: MediaCache,
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

    cached = media_cache.get(parsed.source_url)
    if cached:
        results = cached_inline_results(cached)
        if results:
            cached_photos = [entry for entry in cached if entry.send_type == "photo"]
            if len(cached_photos) >= 2:
                # Same pager as the fresh-probe path, but over cached
                # file_ids (edit_message_media accepts file_id media).
                token = request_store.create_token()
                raw_caption = (cached_photos[0].caption or "")[:1000]
                caption = (
                    raw_caption
                    if parsed.source_url in raw_caption
                    else (f"{parsed.source_url}\n{raw_caption}" if raw_caption else parsed.source_url)
                )[:1024]
                stored = StoredRequest(
                    token=token,
                    request_type="inline_gallery",
                    parsed_input=parsed,
                    options=[],
                    info={
                        "photos": [entry.file_id for entry in cached_photos],
                        "caption": caption,
                    },
                )
                request_store.save(stored)
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

    if await _answer_gallery_media(query, parsed, settings, request_store):
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


@router.chosen_inline_result()
async def inline_chosen_handler(
    chosen: ChosenInlineResult,
    settings: Settings,
    request_store: RequestStore,
    media_cache: MediaCache,
) -> None:
    token = chosen.result_id
    inline_message_id = chosen.inline_message_id
    # 'cached*' results post media instantly client-side; they must never
    # reach the expired-request branch. 'gallery:' results are their own
    # self-contained pager - the saved token drives navigation, not
    # chosen_inline_result.
    if (
        not inline_message_id
        or token == "help"
        or token.startswith("cached")
        or token.startswith("gallery:")
    ):
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
        run_inline_download(
            bot=chosen.bot,
            user_id=chosen.from_user.id,
            inline_message_id=inline_message_id,
            stored=stored,
            settings=settings,
            request_store=request_store,
            media_cache=media_cache,
        )
    )
    _inline_tasks.start(token, task)


@router.callback_query(InlineCancelCallback.filter())
async def inline_cancel_callback(
    query: CallbackQuery,
    callback_data: InlineCancelCallback,
    request_store: RequestStore,
) -> None:
    token = callback_data.token
    task = _inline_tasks.cancel(token)
    if task:
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


@router.callback_query(GalleryNavCallback.filter())
async def gallery_nav_callback(
    query: CallbackQuery,
    callback_data: GalleryNavCallback,
    request_store: RequestStore,
) -> None:
    stored = request_store.load(callback_data.token)
    photos = (stored.info.get("photos") if stored else None) or []
    if not query.inline_message_id or not photos:
        await query.answer("This gallery is expired. Send the link again.")
        return
    index = callback_data.index % len(photos)
    try:
        await query.bot.edit_message_media(
            inline_message_id=query.inline_message_id,
            media=InputMediaPhoto(
                media=photos[index],
                caption=stored.info.get("caption"),
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
