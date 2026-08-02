# Analyst Agent

## Purpose
Ingests investment-related content from news articles and YouTube videos,
extracts structured claims using Claude, and stores them as research notes
for human review.

## Explicit boundaries (read this first)
- This agent **never writes to the `signals` table** and **never talks to
  Alpaca**. Its output has no path to becoming a trade on its own.
- News and YouTube content is treated as *leads to investigate*, not
  validated strategies. Quality varies enormously — prefer editorially
  vetted sources (Reuters, Bloomberg, WSJ, AP) over anonymous blogs or
  hype-driven channels when choosing what to feed in.
- The only way an idea from this agent reaches the execution agent is:
  1. A human reviews the research note (`analyst/review_notes.py`).
  2. If it seems worth pursuing, someone writes an actual, coded strategy
     in `signals/screeners/`.
  3. That strategy gets backtested like any other (see signals/).
  4. Only then can it generate real signals.

## Components
- `analyst/ingest/news.py` — fetches and extracts readable text from a news article URL.
- `analyst/ingest/youtube.py` — fetches a YouTube video's transcript.
- `analyst/extract.py` — sends raw text to Claude, gets back a structured summary
  (strategy summary, tickers mentioned, specificity, short excerpt).
- `analyst/ingest_source.py` — end-to-end: URL in, research note stored.
- `analyst/review_notes.py` — list or interactively triage unreviewed notes.
- `analyst/daily_review.py` (added 2026-07-27) — generates a daily PDF
  report to `A:/trading-desk/Reports/daily_review_<date>.pdf`: P&L,
  positions, orders/signals, stop-management activity, watchlist
  movement (with charts), and a "mistakes & corrected actions" section.
  Two-phase by design: `gather` is a pure script (DB queries, log
  parsing, market data, charts — no LLM needed), `build` assembles the
  PDF from a narrative JSON the calling agent writes after reading
  `gather`'s output. Split this way because `ANTHROPIC_API_KEY` isn't
  configured in this environment, so the narrative synthesis (which
  needs real judgment, not a template) is written by whichever agent
  runs the daily cron, not by an API call inside the script. Scheduled
  daily, weekdays after market close.

## Setup
```bash
pip install -r ../requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage
```bash
# Ingest a single source
python analyst/ingest_source.py https://www.reuters.com/some-article
python analyst/ingest_source.py https://www.youtube.com/watch?v=XXXXXXXXXXX

# Review what's been collected
python analyst/review_notes.py
python analyst/review_notes.py --interactive
```

## Data model
Rows land in `research_notes` (see `shared/db.py`), with a `specificity`
field (`vague` / `moderate` / `concrete`) to help you triage quickly —
vague sentiment pieces are usually not worth pursuing further, while a
concrete, testable claim might be worth coding up and backtesting.

## Known limitations
- Article extraction (`trafilatura`) works well on most mainstream news
  sites but can fail on heavily paywalled or JS-rendered pages.
- YouTube transcript fetching requires the video to have captions
  (auto-generated or manual). Videos without any captions will fail.
- Extraction quality depends on the underlying content — Claude summarizes
  faithfully, but can't validate whether a claim in the source is actually
  true or sound.

## Confirmed against the real API (2026-07-27)
First-ever real run of `analyst/ingest/youtube.py` turned up a real bug:
it called `YouTubeTranscriptApi.get_transcript(video_id)`, a classmethod
that the installed v1.x library no longer has — replaced with an
instance-based `YouTubeTranscriptApi().fetch(video_id)` returning a
`FetchedTranscript` of `FetchedTranscriptSnippet` objects instead of a
list of dicts. Fixed and confirmed working (pulled a real ~25k-character
transcript successfully). `analyst/ingest/news.py`'s real-API path is still
unverified.
