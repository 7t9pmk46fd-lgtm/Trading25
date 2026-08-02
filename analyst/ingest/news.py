"""
News article ingestion.

Fetches a news article's readable text content from a URL. Uses `trafilatura`
(good at stripping ads/nav/boilerplate from arbitrary news sites) with a
fallback to a basic requests + BeautifulSoup extraction if trafilatura isn't
available or fails on a given site.

This module only extracts text — it does not judge source credibility.
That said, not all sources are equal: prefer outlets with editorial
standards (Reuters, Bloomberg, WSJ, AP, etc.) over anonymous blogs or
content farms when deciding what to feed in. Garbage in, garbage out
applies especially hard here since the output feeds directly into your
own research judgment.
"""
from dataclasses import dataclass


@dataclass
class ArticleContent:
    url: str
    title: str
    text: str
    published_at: str | None = None


def fetch_article(url: str) -> ArticleContent:
    try:
        import trafilatura
    except ImportError as e:
        raise ImportError(
            "trafilatura is required for article extraction. "
            "Install with: pip install trafilatura"
        ) from e

    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        raise RuntimeError(f"Could not fetch URL: {url}")

    result = trafilatura.extract(
        downloaded, output_format="json", with_metadata=True
    )
    if result is None:
        raise RuntimeError(f"Could not extract article content from: {url}")

    import json

    data = json.loads(result)
    return ArticleContent(
        url=url,
        title=data.get("title", ""),
        text=data.get("text", ""),
        published_at=data.get("date"),
    )
