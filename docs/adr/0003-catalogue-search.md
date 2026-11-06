# ADR 0003: Catalogue Search

**Status:** Accepted
**Date:** 2026-11-04

## Context

Music Request starts with a ready Source URL. Users need the reverse entry point:
plain text becomes a search across Deezer/Spotify/Tidal, and the selected result
becomes an existing Music Request. DeezLoad is used as the UX reference: filters for
track/album/artist/playlist, an Artist Card, TOP 30, albums, and Tap-to-Download.
YT Music is not included in this phase: the installed `ytmusic-spotiflac` extension
returns an empty `customSearch`, whereas Deezer and Spotify respond correctly.

Search must not create a second music pipeline. A found entity provides a
Source URL, after which ADR 0002 and the existing `pipeline`/`inline_flow`
handle downloading and delivery.

## Decision

1. **One search provider per request.** `deezer`, `spotify-web`, and `tidal-web` are supported.
   The default is set by `SEARCH_PROVIDER=deezer`; the user can select another
   in `/settings`. Search uses the `customSearch` of the installed mornye
   extension; results are normalized into JS-independent domain records.
   Errors from one catalogue are shown as a search error, with no hidden fallback
   to another catalogue.
   Tidal results without an external URL retain their Tidal Source URL rather than
   being converted into an invalid Deezer link.

2. **Search Query and Search Filter.** Bare text means global search;
   the `.trk`, `.a`, `.art`, and `.pla` prefixes restrict the search. A query shorter
   than two characters receives a hint without an API call. Telegram responses are cached
   for 30 seconds, repeated identical queries are cached in memory for 60 seconds, and the rate
   is limited to one search per 1.5 seconds per user. REQUEST_COOLDOWN
   applies only to downloads.

3. **A single results renderer.** Filtered search shows up to eight
   results of the selected type; global search shows up to four of each type. Tracks
   and collections are represented as articles with a cover, `Artist — Title`, and the description
   `Album: … · mm:ss`. A tap creates a Music Request from `external_urls`.

4. **Artist Card is created on selection.** The first inline result for an artist
   uses the search metadata already retrieved and does not trigger a second API call.
   When the result is selected, `getArtist` populates the cover, listener count, TOP, and
   ALBUMS. The Deezer extension limits TOP to twenty tracks, so the public
   `api.deezer.com/artist/{id}/top?limit=30` supplements them to 30, merging by id.
   TOP contains up to 30 tracks, and ALBUMS contains up to 50 albums. Each track
   receives its own `inline_media` token; TOP/ALBUMS/BACK modify only the
   existing card via `inline_message_id`. Follow toggles the record from
   ADR 0004. An external URL is not used in `callback_data`: the Bot API limit is
   64 bytes.
5. **Album Card and downloading.** An album from ALBUMS opens its cover, metadata,
   tracks, and the `GET ALL`, `Download ZIP`, and `BACK` buttons. GET ALL downloads
   the collection through the existing mornye pipeline and sends the audio files one by one.
   ZIP packages the actually downloaded files into a single archive; the inline message is changed
   to a document using the file_id from a preliminary DM upload, because the Bot API does
   not allow uploading a new file directly in an inline message edit.
6. **Inline track delivery.** The message first shows Downloading,
   then the file is uploaded to the private chat to obtain a `file_id`, after which
   `editMessageMedia` replaces the content with `InputMediaAudio`. The private copy
   remains as delivery and a fallback if the edit fails.

## Risks and Consequences

- Search depends on the unstable JS runtime of the extensions; requests are isolated
  by the mornye timeout and do not block the URL pipeline.
- Editing an old inline message requires preserving the artist state
  in Request Store; a stale token receives a clear response instead of a traceback.
- Enabling text search changes the handling of ordinary private-chat messages, but
  only for messages without URLs, and the behavior has a reversible per-user toggle.
- MP3 transcoding requires ffmpeg, which yt-dlp already requires.
