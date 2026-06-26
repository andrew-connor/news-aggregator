from fetcher import article_id, canonicalize_url


def test_same_url_same_id():
    """Two fetches of the same article produce the same document ID — the second is a no-op."""
    url = "https://npr.org/2026/05/story"
    assert article_id(canonicalize_url(url)) == article_id(canonicalize_url(url))


def test_tracking_param_variants_deduplicate():
    """Same article with different tracking params maps to the same document ID."""
    id1 = article_id(canonicalize_url("https://npr.org/story?utm_source=rss"))
    id2 = article_id(canonicalize_url("https://npr.org/story?utm_campaign=email"))
    assert id1 == id2


def test_fragment_variants_deduplicate():
    id1 = article_id(canonicalize_url("https://npr.org/story#top"))
    id2 = article_id(canonicalize_url("https://npr.org/story#bottom"))
    assert id1 == id2


def test_trailing_slash_deduplicates():
    id1 = article_id(canonicalize_url("https://npr.org/story"))
    id2 = article_id(canonicalize_url("https://npr.org/story/"))
    assert id1 == id2


def test_different_articles_different_ids():
    id1 = article_id(canonicalize_url("https://npr.org/story-a"))
    id2 = article_id(canonicalize_url("https://npr.org/story-b"))
    assert id1 != id2


def test_different_sources_different_ids():
    id1 = article_id(canonicalize_url("https://npr.org/story"))
    id2 = article_id(canonicalize_url("https://bbc.co.uk/story"))
    assert id1 != id2
