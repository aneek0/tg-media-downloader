# tg-media-downloader

A Telegram bot that fetches media from a link and returns it to the chat. The glossary
describes the domain language: how the bot names things that the user sees as
“one action — one result”. Implementation is out of scope here.

## General terms

Both pipelines — direct messages and inline — use this vocabulary.

**Source URL**:
The address the user gave the bot and from which the bot fetches media.
_Avoid_: link, URL, address

**Parsed Input**:
Source URL plus the optional parts the user added after it: file name, login,
password.
_Avoid_: input, arguments, parameters

**Request**:
A unit of work the bot has taken on for one Source URL and keeps while the user
chooses what exactly to download. A Request has a Token.
_Avoid_: task, request, job

**Token**:
A short identifier for a Request, used by the bot to find it later when the
user presses a button. The user-facing reference to a Request.
_Avoid_: id, key, request_id

**Option**:
One choice the user can select within a Request: a specific quality, format,
or “best available”. An Option has a Send Type.
_Avoid_: button, item, choice

**Send Type**:
How Telegram should accept the file: photo, video, audio, video_note, document,
or animation. It determines which method the bot uses to send the result.
_Avoid_: file type, format, mime

**Artifact**:
The downloaded file on disk plus the Send Type, caption, and metadata Telegram
needs. An intermediate result that has not yet been sent to the user.
_Avoid_: file, download, result

**Download Pipeline**:
The single place that decides, from a Source URL and Parsed Input, what to
download with: music, gallery, yt-dlp, or direct download. Intake asks it for
Options; the other pipelines ask it for Artifacts.
_Avoid_: executor, dispatcher, router

**Media Cache**:
A store of mappings from Source URL → file_id for already sent media. A repeated
request for the same URL is served instantly, without downloading. Entries are
evicted by limit and deleted manually.
_Avoid_: file cache, storage, box

**Preview Store**:
A store of a user's personal preview image, which the bot attaches to that
user's sends. Each user has their own; its lifetime is set by the user.
_Avoid_: thumbnail store, cache, avatar

**Session**:
Everything owned by one bot run: Settings, Cooldown, Media Cache, Preview
Store, the Token store for Requests, and the music Provider. Handlers and
pipelines receive it as one argument rather than a list of dependencies.
_Avoid_: context, dependencies, bot state

## Direct-message delivery

**Intake**:
The entry point of the whole chain: parsing the message, checking limits,
hitting the Media Cache, choosing the gallery or format, and showing the
keyboard.
_Avoid_: intake handler, parser, parsing

**Cooldown**:
A restriction that prevents the user from sending the bot new links more often
than a set interval.
_Avoid_: rate limit, anti-flood, throttling

**Work Directory**:
A Request's temporary folder holding the downloaded files. Deleted together
with the Request.
_Avoid_: tmp, downloads folder, staging

## Inline delivery

**Inline Result**:
What the bot shows in the autocomplete dropdown. It can be ready media or a
button that starts a download when pressed.
_Avoid_: result, item, suggestion

**Tap-to-Download**:
An Inline Result that carries no media itself and downloads it when pressed.
Used where Telegram cannot fetch the file itself.
_Avoid_: download link, placeholder, preview

**Telegram Fetch**:
A delivery method in which the bot gives Telegram an address and does not
download the file itself: Telegram fetches the media with its own servers.
_Avoid_: CDN sending, sending by link, outsource

**Direct Upload**:
A delivery method in which the bot downloads the file itself and sends it to
Telegram. The only way to deliver what Telegram Fetch cannot fetch.
_Avoid_: upload, sending a file, download

**Overflow DM**:
Extra media from an album that did not fit into the Inline Result and was sent
to the user in direct messages.
_Avoid_: tail, extra, remainder

## Gallery

**Work**:
One Pixiv artwork: a single image, a multi-page illustration, or an ugoira.
_Avoid_: post, illustration, image

**Ugoira**:
A Pixiv Work made of animation frames; the bot delivers it as an MP4 video.
_Avoid_: animation, gif, frames

**Profile Page**:
One screenful of an artist's Works in a browsing sequence. A page never splits
a Work, and its Works total at most 50 files.
_Avoid_: batch, chunk, slice

**Cover Album**:
The album of Work covers shown before a Profile Page is downloaded.
_Avoid_: preview, thumbnails, gallery preview

## Music

**Source Domain**:
The origin of a Source URL: a general site, a tweet post, a Pixiv artist or
Work, or a music service. The domain determines which pipeline turns a Request
into an Artifact. Current ones: guess (general), twitter, pixiv, spotify,
deezer, tidal, qobuz, soundcloud.
_Avoid_: platform, link type, provider

**Music Request**:
A Request for a Source URL with a music Source Domain (spotify, deezer, tidal,
qobuz, soundcloud): track, album, or playlist. Qobuz artist links are refused
with an explicit message. Metadata is resolved before the
keyboard; the download is performed by mornye through Providers. Track
Collection is truncated to the cap.
_Avoid_: Spotify Request, music request, mornye request, track

**Provider**:
A mornye extension from which FLAC is downloaded (ext:deezer, ext:tidal-web,
…). The set and order are set by config; none ship with
mornye — the registry is chosen by the bot owner.
_Avoid_: service, source, extension

**Track Collection**:
A music container with more than one track: album, playlist, artist
discography. Downloaded in full (up to the cap), sent as documents one by one
with cover art.
_Avoid_: album, playlist, set

**Search Query**:
The text the user searches for in the music catalogue, plus an optional
Search Filter. It is not a Source URL until a result is selected.
_Avoid_: query, text, message

**Search Filter**:
A restriction of a Search Query to one kind of result: track, album, artist, or
playlist. Without a filter, a global search runs across all four kinds.
_Avoid_: tab, mode, search type

**Catalogue Search**:
A search within one selected catalogue (deezer, spotify-web, or tidal-web). It
does not download files or consume Request Cooldown; the selected result
creates a Music Request for its Source URL.
_Avoid_: global search, deezer, spotify

**Artist Card**:
An artist catalogue view with the artist's photo, album and listener counts, and
the TOP 30, ALBUMS, and Follow buttons. Private inline selections hand off the
artist URL to produce a bot-authored DM card; other chats edit the Inline Result.
_Avoid_: card, profile, artist result

**Follow**:
A local subscription of a Telegram user to an artist's new releases in the
catalogue. The first Follow saves the baseline of existing releases and sends
nothing.
_Avoid_: Deezer subscription, favorites, following

**Release Notification**:
A single direct message about an artist's new releases after polling Follow:
the cover, up to five titles, and an unsubscribe button.
_Avoid_: broadcast, news, alert
