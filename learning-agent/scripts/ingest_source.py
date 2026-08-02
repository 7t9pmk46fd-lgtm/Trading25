"""
Ingest a single source (news article or YouTube video) and store a
structured research note.

Usage:
    python scripts/ingest_source.py https://example.com/some-article
    python scripts/ingest_source.py https://youtube.com/watch?v=XXXXXXXXXXX

Auto-detects source type from the URL. Does NOT write to the signals table
or interact with Alpaca in any way -- output only ever lands in
research_notes for manual review.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import db
from ingest.news import fetch_article
from ingest.youtube import fetch_transcript
from extract import extract_research_note


def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def ingest_source(url: str) -> int:
    if is_youtube_url(url):
        video = fetch_transcript(url)
        source_type = "youtube_video"
        title = video.title
        text = video.transcript_text
        published_at = None
    else:
        article = fetch_article(url)
        source_type = "news_article"
        title = article.title
        text = article.text
        published_at = article.published_at

    if not text.strip():
        raise RuntimeError(f"No content extracted from {url} -- nothing to analyze.")

    extracted = extract_research_note(text, source_title=title)

    with db.db_session() as conn:
        note_id = db.insert_research_note(
            conn,
            source_type=source_type,
            source_url=url,
            source_title=title,
            published_at=published_at,
            strategy_summary=extracted.get("strategy_summary", ""),
            tickers_mentioned=extracted.get("tickers_mentioned", ""),
            specificity=extracted.get("specificity", "vague"),
            raw_excerpt=extracted.get("raw_excerpt", ""),
        )

    return note_id


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python ingest_source.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Ingesting: {url}")
    note_id = ingest_source(url)
    print(f"Stored research note #{note_id}. Review with scripts/review_notes.py")
