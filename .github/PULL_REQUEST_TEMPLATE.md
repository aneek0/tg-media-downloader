name: Pull Request

about: Changes to the bot, runtime, or tooling

---
## Summary

What this change does, in one or two sentences.

## User-facing impact

What a bot user or operator will notice, or "none".

## Environment / deployment notes

Any new or changed `.env` variables, Docker implications, or migration steps, or "none".

## Checklist

- [ ] `uv run pytest` passes
- [ ] `uv run pylint $(git ls-files '*.py')` passes
- [ ] Tests added or updated for behavior changes
- [ ] `CHANGELOG.md` updated for user-visible changes
- [ ] Related issue linked (if any)
