# ADR 0004: Subscriptions to new artist releases

**Status:** Accepted
**Date:** 2026-11-04

## Context

Follow on the Artist Card must track new releases for the Telegram user. No Deezer account is required: `getArtist` returns public `albums[]`, `top_tracks[]`, cover art, and listener counts. The installed extension already provides all the data needed to compare catalogue snapshots.

## Decision

1. **Follow is a local association.** The record contains the Telegram user id, Provider, artist id and name, cover art, and known release ids. Storage is `DOWNLOADS/follows.json`, written atomically following the Media Cache pattern. A user cannot follow the same artist twice; different users have independent follows.

2. **The first Follow only records the baseline.** Existing albums do not trigger a mass mailing. On subsequent polls, new ids form one Release Notification per artist with at most five releases; the rest are recorded in the baseline and do not create a flood of messages.

3. **Polling every six hours.** A background asyncio task starts with polling and stops on shutdown. Only artists with followers are traversed; a failure of one Provider neither changes the baseline nor affects the others. Delivery to each user happens independently of the others.

4. **Management.** The `FOLLOW ➕`/`FOLLOWING ✓` button on the Artist Card toggles the follow. `/follows` shows a list with unfollow buttons, `/unfollows` opens the same list; `/follow <artist-url>` adds a follow by public link. The Release Notification contains the cover art, artist, release titles and dates, and an unfollow button. Singles and albums are not filtered.

## Risks and consequences

- The catalogue may delete or reissue a release: comparison is by id, so a reissue with a new id counts as a new release, while a disappearance is not treated as an unfollow.
- If the bot is offline, polls are skipped without losing releases: the baseline is preserved, and the next start discovers them. On the first start after the notification feature is introduced, there is no mass mailing of the old catalogue.
- Public metadata does not provide RADIO/RELATED; those features are out of scope for this decision.
