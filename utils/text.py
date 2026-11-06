from __future__ import annotations

import html

START_TEXT = """Hi, <b>{name}</b>

I can take a direct file link or a supported media URL, download it, and upload the result back to Telegram.

<b>What you can send</b>
- direct file URLs
- YouTube and other supported `yt-dlp` links
- `url|filename`
- `url|filename|username|password`
- `url * filename`

Use the buttons below for a quick tour, or send a link to get started."""

HELP_TEXT = """<b>How to use this bot</b>

Send a message containing any of these formats:

1. <b>Direct link</b>
   <code>https://example.com/file.mp4</code>

2. <b>Direct link with a custom file name</b>
   <code>https://example.com/file.mp4|my-video.mp4</code>

3. <b>Protected URL with login details</b>
   <code>https://example.com/file|my-file.mp4|username|password</code>

4. <b>Alternate custom-name format</b>
   <code>https://example.com/file.mp4 * renamed.mp4</code>

<b>Extra commands</b>
- <code>/thumb</code> shows your saved thumbnail
- <code>/delthumb</code> removes your saved thumbnail

By default I download the best available quality automatically. If format selection is enabled, I will show a menu before uploading."""

ABOUT_TEXT = """<b>tg-media-downloader</b>

Built with <a href="https://docs.aiogram.dev/">aiogram 3</a>, <a href="https://github.com/yt-dlp/yt-dlp">yt-dlp</a>, and <a href="https://github.com/mikf/gallery-dl">gallery-dl</a>.

<b>Repository</b>
<a href="https://github.com/aneek0/tg-media-downloader">github.com/aneek0/tg-media-downloader</a>

<b>What it does</b>
- downloads supported media from direct URLs and `yt-dlp` sources
- downloads Twitter/X posts via `gallery-dl`
- uploads video, audio, and documents back to Telegram
- works in inline mode in any chat
- supports custom per-user thumbnails

Send a supported link whenever you are ready."""

THUMB_SAVED = "Your custom thumbnail is saved."
THUMB_MISSING = "You do not have a thumbnail yet. Send a JPG image to save one."
THUMB_REMOVED = "Your thumbnail was removed successfully."

PROCESSING = "Processing your link..."
FORMAT_SELECTION = "Choose the format you want."

MUSIC_RESOLVING = "Looking up your music link..."
MUSIC_DOWNLOAD = "Downloading <b>{name}</b> ({kind}, {count} track(s))..."
ALBUM_INFO_CAPTION = (
    "💽 Album: <b>{name}</b>\n👤 Artist: {artist}\n📅 Date: {date}\n"
    "🎧 Total tracks: {count}"
)
SPOTIFY_PARTIAL = "{sent} track(s) sent, {failed} failed: {titles}"
SPOTIFY_FAILED_SINGLE = "No audio downloaded for that link."
SPOTIFY_TRACK_CAPTION = "{artists} — {title}"
DOWNLOAD_START = "Downloading <b>{name}</b>"
UPLOAD_START = "Uploading <b>{name}</b>"
QUICK_CHOICE = "Choose how you want this YouTube link downloaded."
YOUTUBE_AUDIO_LABEL = "Audio"
YOUTUBE_VIDEO_LABEL = "Video"
RATE_LIMIT = (
    "Please wait before starting another request. Try again in about {minutes} minute(s)."
)
DOWNLOAD_FAILED = "I could not process that link."
DIRECT_DOWNLOAD_FAILED = "I could not download that file."
REQUEST_EXPIRED = "That request has expired. Send the link again."
FILE_TOO_LARGE = (
    "Downloaded in {seconds} seconds.\nDetected file size: {size}\n"
    "I cannot upload files larger than Telegram allows."
)
DONE = "Downloaded in {download_seconds} seconds.\nUploaded in {upload_seconds} seconds."

INLINE_TITLE = "Download media"
INLINE_FAILED = "I could not download that link."

TOO_LARGE = (
    "That file is larger than Telegram allows ({size}).\n"
    "Skipping the download — it cannot be uploaded."
)


def upload_caption(name: str) -> str:
    return UPLOAD_START.format(name=name)


def download_caption(name: str) -> str:
    return DOWNLOAD_START.format(name=name)


def esc(value: str) -> str:
    """HTML-escape untrusted text for ParseMode.HTML messages."""
    return html.escape(value, quote=False)


CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def split_caption(caption: str | None) -> tuple[str | None, list[str]]:
    """Fit post text into Telegram limits: (media_caption, follow-up chunks).

    A media caption holds 1024 characters, so longer text travels as plain
    messages of up to 4096 instead of being truncated mid-link."""
    if not caption or len(caption) <= CAPTION_LIMIT:
        return caption, []
    return None, split_message(caption)


def split_message(value: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split text on line boundaries; over-long single lines are hard-wrapped."""
    chunks: list[str] = []
    current = ""
    for line in value.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]
SEARCH_PROMPT = "🔎 Search this text in the music catalogue:"
SEARCH_TRACKS = "🎵 Tracks"
SEARCH_ALBUMS = "💽 Albums"
SEARCH_ARTISTS = "🎤 Artists"
SEARCH_PLAYLISTS = "🎶 Playlists"
SEARCH_GLOBAL = "🌐 Global search"
SEARCH_FAILED = "Catalogue search failed"
SEARCH_NETWORK_ERROR = "Network error: catalogue provider is unreachable. Please try again."
SEARCH_RATE_LIMIT = "Wait a moment before searching again"
ARTIST_CARD = "Artist Card"
ARTIST_TOP = "TOP 🔝"
ARTIST_ALBUMS = "ALBUMS 💽"
ARTIST_TOP_BACK = "TOP 🔙"
ARTIST_FOLLOW = "FOLLOW ➕"
ARTIST_FOLLOWING = "FOLLOWING ✓"
ARTIST_CAPTION = "🎤 Artist: {name}\n👥 Listeners: {listeners}\n💽 Albums: {albums}"
ALBUM_CAPTION = "💽 Album: {album}\n👤 Artist: {artist}\n📅 Date: {date}\n🎧 Total tracks: {tracks}"
ALBUM_GET_ALL = "GET ALL ⬇️"
ALBUM_ZIP = "Download ZIP 🗃️"
ALBUM_BACK = "BACK 🔙"
FOLLOW_ADDED = "Following {name}"
FOLLOW_REMOVED = "Unfollowed {name}"
FOLLOW_ALREADY = "Already following {name}"
FOLLOW_USAGE = "Usage: /follow <artist-url>"
FOLLOW_LIST_EMPTY = "You are not following any artists."
FOLLOW_LIST_TITLE = "🎤 Followed artists:"
FOLLOW_REMOVE = "Unfollow"
FOLLOW_REMOVE_NAME = "Unfollow {name}"
FOLLOW_REMOVED_CONFIRMATION = "Unfollowed"
FOLLOW_ALREADY_REMOVED = "Already unfollowed"
NEW_RELEASE_CAPTION = "🎤 {artist} released:"
#: How to refer to the bot when its own handle is not yet known.
GENERIC_BOT = "the bot"
SETTINGS_TITLE = "⚙️ <b>Settings</b>"
SETTINGS_SAVED = "Saved"
SETTINGS_OPEN_DM = "Open /settings in {bot}"
SETTINGS_UNCHANGED = "Already selected"
