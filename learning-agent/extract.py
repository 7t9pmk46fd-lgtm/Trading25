"""
Extraction: turns raw article text or video transcript into a structured
research note using the Anthropic API.

Deliberately conservative extraction goals:
  - Summarize the strategy/claim in plain language (not a lift of the
    original wording).
  - Flag how concrete/actionable the claim actually is, since "buy tech
    stocks" and "a documented, testable entry/exit rule" are very
    different things even if both get posted confidently.
  - Pull at most a short supporting excerpt, not long passages.
  - Never output a buy/sell recommendation — this module's job is
    summarization for human review, not signal generation.
"""
import json

from anthropic import Anthropic

from shared.config import ANTHROPIC_API_KEY, require_anthropic_credentials

EXTRACTION_MODEL = "claude-sonnet-5"  # swap to "claude-haiku-4-5-20251001" for cheaper bulk extraction

SYSTEM_PROMPT = """You are helping a retail investor triage investment-related \
content (news articles, YouTube video transcripts) into structured research \
notes for later human review. You are NOT deciding whether to trade anything.

Given the source text, extract:
- strategy_summary: 2-3 sentences, in your own words, describing what \
investment idea/strategy/thesis is being presented. If there isn't really \
one (e.g. it's general news with no actionable thesis), say so plainly.
- tickers_mentioned: comma-separated list of stock tickers or sectors/themes \
mentioned in connection with the idea (empty string if none).
- specificity: one of "vague" (general sentiment/opinion, no concrete rule), \
"moderate" (a thesis with some reasoning but no precise entry/exit criteria), \
or "concrete" (a specific, testable rule e.g. exact indicators/thresholds).
- raw_excerpt: at most one short supporting phrase (under 15 words), or \
empty string if none is needed to support your summary.

Respond ONLY with valid JSON matching this shape, no other text:
{"strategy_summary": "...", "tickers_mentioned": "...", "specificity": "...", "raw_excerpt": "..."}
"""


def extract_research_note(text: str, source_title: str = "") -> dict:
    require_anthropic_credentials()
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Trim very long transcripts/articles to a reasonable context size.
    trimmed = text[:15000]

    user_content = f"Title: {source_title}\n\nContent:\n{trimmed}"

    response = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    raw_text = "".join(
        block.text for block in response.content if block.type == "text"
    )

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        # Model didn't return clean JSON -- surface the raw text rather than
        # silently failing, so a human can see what went wrong.
        return {
            "strategy_summary": f"[EXTRACTION PARSE ERROR] raw response: {raw_text[:500]}",
            "tickers_mentioned": "",
            "specificity": "vague",
            "raw_excerpt": "",
        }
