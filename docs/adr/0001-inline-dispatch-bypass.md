# ADR 0001: Inline delivery is dispatched separately from DM

**Status:** Accepted (records the status quo)
**Date:** 2026-10-17

## Context

`services/executor.py` was long described as "the single dispatch point".
In fact that holds only for the DM path: inline requests (`inline_media`,
`inline_gallery`) are handled in `services/inline_flow.py`, bypassing `executor.py`
and the call site `routers/callbacks.py:71`.

Because of the documentation mismatch (`AGENTS.md`), the request type list was
missing `inline_gallery` — the seventh type out of seven.

## Decision

We keep two entry points into the code. The documentation is brought in line with
the facts: `executor.py` dispatches the DM flow; `inline_flow.py` dispatches the
inline flow.

We do not route inline through `executor.py`.

## Why not unify

The DM path works with a `status_message` object (`aiogram.types.Message`), while
the inline path works with an `inline_message_id` string and the `_InlineStatus`
shim (`services/inline_flow.py:56`), which substitutes `edit_text`. An inline
message lives in someone else's chat and is not available for ordinary editing.
These are different delivery surfaces, not duplication of the same code.

## Closed duplication

The chunking in groups of 10 and the `file_id` parsing now live in
`services/delivery.py:DirectDelivery`. `services/inline_flow.py:_dm_send_artifacts`
delegates overflow DM sending to it. The separate DM/inline dispatchers remain:
this ADR separates delivery surfaces, it does not duplicate file sending.

## Consequences

- A reader of `AGENTS.md` sees that inline does not go through `executor.py`.
- The tweet-video behaviour mismatch (DM tries Telegram Fetch first, inline always
  downloads locally) remains and requires a separate decision.
