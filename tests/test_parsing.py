from services.parsing import (
    is_probable_youtube_url,
    is_twitter_status_url,
    parse_user_input,
)


def test_is_twitter_status_url_matches():
    assert is_twitter_status_url("https://x.com/AlterKyon/status/2103014006747439474")
    assert is_twitter_status_url("https://twitter.com/a/statuses/123")
    assert is_twitter_status_url("https://www.x.com/a/status/1")
    assert is_twitter_status_url("https://mobile.twitter.com/a/status/1")
    assert is_twitter_status_url("https://x.com/i/web/status/1")


def test_is_twitter_status_url_rejects_non_status():
    assert not is_twitter_status_url("https://x.com/AlterKyon")
    assert not is_twitter_status_url("https://x.com/search?q=x")
    assert not is_twitter_status_url("https://pbs.twimg.com/media/abc")
    assert not is_twitter_status_url("https://youtube.com/watch?v=1")


def test_parse_pipe_filename():
    parsed = parse_user_input("https://example.com/video.mp4|custom.mp4")

    assert parsed.source_url == "https://example.com/video.mp4"
    assert parsed.custom_file_name == "custom.mp4"


def test_parse_pipe_auth():
    parsed = parse_user_input("https://example.com/file|name|user|pass")

    assert parsed.username == "user"
    assert parsed.password == "pass"


def test_parse_star_filename():
    parsed = parse_user_input("https://example.com/video.mp4 * renamed.mp4")

    assert parsed.custom_file_name == "renamed.mp4"


def test_detect_youtube_url():
    assert is_probable_youtube_url("https://youtu.be/test")
    assert is_probable_youtube_url("https://www.youtube.com/watch?v=test")
