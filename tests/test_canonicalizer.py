from fetcher import article_id, canonicalize_url


def test_strips_query_params():
    assert canonicalize_url("https://example.com/story?utm_source=rss") == "https://example.com/story"


def test_strips_fragment():
    assert canonicalize_url("https://example.com/story#section") == "https://example.com/story"


def test_strips_query_and_fragment_together():
    assert canonicalize_url("https://example.com/story?id=1#top") == "https://example.com/story"


def test_lowercases_scheme():
    assert canonicalize_url("HTTPS://example.com/story").startswith("https://")


def test_lowercases_host():
    assert canonicalize_url("https://Example.COM/story") == "https://example.com/story"


def test_removes_trailing_slash():
    assert canonicalize_url("https://example.com/story/") == "https://example.com/story"


def test_preserves_path():
    result = canonicalize_url("https://example.com/news/2026/05/story")
    assert result == "https://example.com/news/2026/05/story"


def test_root_path_trailing_slash_removed():
    assert canonicalize_url("https://example.com/") == "https://example.com"


def test_article_id_is_16_hex_chars():
    result = article_id("https://example.com/story")
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


def test_article_id_is_deterministic():
    url = "https://example.com/story"
    assert article_id(url) == article_id(url)


def test_article_id_differs_for_different_urls():
    assert article_id("https://example.com/a") != article_id("https://example.com/b")
