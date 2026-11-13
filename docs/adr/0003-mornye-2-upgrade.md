# ADR 0003: mornye 1.0 → 2.x upgrade for Qobuz links

**Status:** Accepted
**Date:** 2026-11-13

## Context

Users paste Qobuz links (`open.qobuz.com/album/<id>`); the bot answered
"This link returns a web page, not a media file." The gate is
`services/mornye.py:is_mornye_url`, which delegates link recognition to the
installed mornye's `SITES` table — and mornye 1.0.0 (the pinned `>=1,<2`) knows
only melon, bugs, genie, deezer. Qobuz hosts are recognized from mornye 2.0.4 on,
and the bot needs no new music-flow code: a Qobuz link then rides the existing
catalogue-extension branch (`_resolve_catalogue_url`) that Deezer links use.

## Decision

1. **Pin mornye 2.x** (`>=2,<3` while on PyPI releases). All bot-imported mornye
   symbols are unchanged in 2.0.6; Deezer/Spotify/Tidal resolution and downloads
   verified against 2.0.6 before upgrading.

2. **Two registries in `MORNYE_REGISTRIES`.** mornye 2.x moved signed sessions,
   the Turnstile solver, and the Spotify/Tidal metadata helpers into
   `utilities.sflx`, shipped only in `registry-python.json`. The music extensions
   themselves (qobuz-web, tidal-web, deezer, …) live only in `registry.json`.
   Both URLs are required; `.env.example` and README document the combined value.

3. **Temporary fork pin for short Qobuz ids.** mornye 2.0.6's Qobuz URL patterns
   demand 9–14 character ids, but real Qobuz track and playlist ids are 7–9 digits
   (albums are 12–14-digit UPCs and work). Until upstream releases the fix
   (mornyeapp/Mornye-Python#278), the bot pins
   `mornye @ git+…@fix/qobuz-url-id-lengths`. After a 2.0.7 release, revert the
   dependency line to `mornye>=2,<3`.

4. **Qobuz artist links are refused explicitly** in `MornyeService.resolve_url`:
   mornye's catalogue resolver has no `getArtist`, so the link would otherwise
   die on a `KeyError` surfaced as a failed download. Same shape as the Tidal
   artist/mix refusal. Native artist support can be added later in mornye.

## Consequences

- Qobuz track/album/playlist links work in DM and inline with no bot-side
  download code; `ext:qobuz-web` is already in `MORNYE_SERVICES`.
- `uv sync` and Docker `--frozen` installs build mornye from the fork branch;
  each upstream release needs a `uv lock` refresh until the pin reverts to PyPI.
- The mornye venv install is a *build* of the fork branch; local edits to the
  Mornye-Python checkout do not reach the bot until pushed and re-locked.
