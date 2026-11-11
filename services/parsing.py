from __future__ import annotations

import re
from urllib.parse import urlparse

from aiogram.types import MessageEntity

from utils.models import ParsedInput


def first_url_in(text: str) -> str | None:
    """First http(s) URL in the text, so multi-URL messages download one link
    instead of treating the whole sentence as a URL."""
    match = re.search(r"https?://\S+", text)
    return match.group(0).rstrip(".,;:!?)»\"'") if match else None


def _is_downloadable_scheme(url: str) -> bool:
    return urlparse(url).scheme in {"http", "https"}


def extract_link_text(text: str, entities: list[MessageEntity] | None) -> str | None:
    if entities:
        for entity in entities:
            if entity.type == "text_link" and entity.url:
                return entity.url
            if entity.type == "url":
                # Telegram offsets/lengths are UTF-16 code units; a plain
                # Python slice mis-cuts when emoji precede the link.
                return entity.extract_from(text)
    return first_url_in(text)


def _extract_url(text: str, entities: list[MessageEntity] | None) -> str:
    entity_url = extract_link_text(text, entities)
    if not entity_url:
        raise ValueError("No URL found in the message")
    return entity_url.strip()


def parse_user_input(
    text: str, entities: list[MessageEntity] | None = None
) -> ParsedInput:
    parsed = _parse_raw_input(text, entities)
    if not _is_downloadable_scheme(parsed.source_url):
        raise ValueError("Only http and https links are supported")
    return parsed


def _parse_raw_input(
    text: str, entities: list[MessageEntity] | None
) -> ParsedInput:
    if "|" in text:
        parts = [part.strip() for part in text.split("|")]
        if len(parts) == 2:
            return ParsedInput(source_url=parts[0], custom_file_name=parts[1])
        if len(parts) == 4:
            return ParsedInput(
                source_url=parts[0],
                custom_file_name=parts[1],
                username=parts[2],
                password=parts[3],
            )

    if " * " in text:
        url, file_name = [part.strip() for part in text.split(" * ", maxsplit=1)]
        return ParsedInput(source_url=url, custom_file_name=file_name)

    return ParsedInput(source_url=_extract_url(text, entities))


def is_probable_youtube_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    return (
        hostname == "youtube.com"
        or hostname.endswith(".youtube.com")
        or hostname == "youtu.be"
        or hostname.endswith(".youtu.be")
    )


def is_twitter_status_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower()
    if not (
        hostname == "x.com"
        or hostname.endswith(".x.com")
        or hostname == "twitter.com"
        or hostname.endswith(".twitter.com")
    ):
        return False
    return re.search(r"/status(?:es)?/\d+", urlparse(url).path) is not None
