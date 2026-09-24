from pathlib import Path

import pytest

from config import Settings



@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    # chdir alone is not enough: python-dotenv's find_dotenv() resolves from the
    # caller's __file__ (config.py in the repo root) when not in a REPL, so the
    # real repo .env would still leak into Settings.from_env(). Disabling dotenv
    # loading entirely makes the isolation hermetic.
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")

def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("AUTH_USERS", "1 2 42")
    monkeypatch.setenv("DOWNLOAD_LOCATION", "./tmp-downloads")
    monkeypatch.setenv("CHUNK_SIZE", "128")

    settings = Settings.from_env()

    assert settings.bot_token == "token"
    assert settings.owner_id == 42
    assert settings.auth_users == {1, 2, 42}
    assert settings.download_location == Path("./tmp-downloads")
    assert settings.chunk_size == 131072


def test_settings_auto_best_quality_defaults_true(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.delenv("AUTO_BEST_QUALITY", raising=False)
    monkeypatch.delenv("MAX_HEIGHT", raising=False)

    settings = Settings.from_env()

    assert settings.auto_best_quality is True
    assert settings.max_video_height == 1080


def test_settings_auto_best_quality_parses_false(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("MAX_HEIGHT", "1080")

    for raw in ("false", "0"):
        monkeypatch.setenv("AUTO_BEST_QUALITY", raw)
        assert Settings.from_env().auto_best_quality is False


def test_settings_auto_best_quality_rejects_garbage(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("MAX_HEIGHT", "1080")
    monkeypatch.setenv("AUTO_BEST_QUALITY", "maybe")

    with pytest.raises(RuntimeError):
        Settings.from_env()


def test_settings_max_height_defaults_1080(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.delenv("MAX_HEIGHT", raising=False)

    assert Settings.from_env().max_video_height == 1080


def test_settings_max_height_parses_value(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("MAX_HEIGHT", "480")

    assert Settings.from_env().max_video_height == 480


def test_settings_max_height_rejects_nonpositive(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")

    for raw in ("0", "abc"):
        monkeypatch.setenv("MAX_HEIGHT", raw)
        with pytest.raises(RuntimeError):
            Settings.from_env()


def test_settings_twitter_cookies_unset_is_empty(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.delenv("TWITTER_COOKIES", raising=False)

    assert Settings.from_env().twitter_cookies == ""


def test_settings_twitter_cookies_parses_inline_content(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv(
        "TWITTER_COOKIES",
        "  # Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tabc  ",
    )

    assert Settings.from_env().twitter_cookies == (
        "# Netscape HTTP Cookie File\n.x.com\tTRUE\t/\tTRUE\t0\tauth_token\tabc"
    )


def test_settings_twitter_cookies_parses_path(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("TWITTER_COOKIES", "  /etc/cookies.txt  ")

    assert Settings.from_env().twitter_cookies == "/etc/cookies.txt"


def test_settings_telegram_api_url_defaults_empty(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.delenv("TELEGRAM_API_URL", raising=False)

    assert Settings.from_env().telegram_api_url == ""


def test_settings_telegram_api_url_parses(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("TELEGRAM_API_URL", "http://localhost:8081/ ")

    # from_env() strips whitespace; trailing-slash handling is delegated to
    # TelegramAPIServer.from_base() in app.py.
    assert Settings.from_env().telegram_api_url == "http://localhost:8081/"


def test_settings_telegram_proxy_defaults_to_http_proxy(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("HTTP_PROXY", "http://gen:1")
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)

    settings = Settings.from_env()

    assert settings.telegram_proxy == "http://gen:1"
    assert settings.http_proxy == "http://gen:1"


def test_settings_telegram_proxy_overrides_http_proxy(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.setenv("HTTP_PROXY", "http://gen:1")
    monkeypatch.setenv("TELEGRAM_PROXY", "http://tg:2")

    settings = Settings.from_env()

    assert settings.telegram_proxy == "http://tg:2"
    assert settings.http_proxy == "http://gen:1"


def test_settings_telegram_proxy_empty_without_any_proxy(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("OWNER_ID", "42")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)

    assert Settings.from_env().telegram_proxy == ""
