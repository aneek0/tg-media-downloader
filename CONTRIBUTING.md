# Contributing

Thanks for helping improve tg-media-downloader.

This is a small self-hosted fork; contributions are welcome for bug fixes and focused improvements
that fit the bot's current scope.

## Before You Start

- search existing [issues](https://github.com/aneek0/tg-media-downloader/issues) first
- open an issue before large changes so the direction is clear
- keep changes focused; avoid mixing refactors, docs edits, and feature work unless they are
  directly related

## Local Setup

1. Clone the repository:

```bash
git clone https://github.com/aneek0/tg-media-downloader.git
cd tg-media-downloader
```

2. Create a `.env` file:

```bash
cp .env.example .env
```

3. Install dependencies:

```bash
uv sync --group dev
```

4. Run the bot locally:

```bash
uv run python bot.py
```

## Project Layout

- root runtime entrypoints: `bot.py`, `app.py`, `config.py`
- Telegram routers: `routers/`
- services and integrations: `services/`
- shared helpers, models, keyboards, and text: `utils/`
- automated tests: `tests/`

## Development Guidelines

- follow the current `aiogram` 3.x structure and existing project patterns
- prefer small, reviewable pull requests
- add or update tests when behavior changes
- keep user-facing copy clear and consistent
- update `CHANGELOG.md` for user-visible changes

## Checks

Run these before opening a pull request:

```bash
uv run pytest
uv run pylint $(git ls-files '*.py')
```

## Pull Requests

When opening a pull request:

- use a clear title and summary
- explain the user-facing impact
- mention any environment or deployment implications
- link the related issue when there is one

## Reporting Bugs

Bug reports are most useful when they include:

- what you tried to do
- what happened instead
- steps to reproduce
- logs or traceback output
- relevant environment details such as Python version, host platform, or proxy setup
