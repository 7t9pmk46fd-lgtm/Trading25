"""
YouTube video ingestion.

Fetches the transcript/captions of a YouTube video for extraction. Uses
`youtube-transcript-api`, which pulls YouTube's own auto-generated or
creator-provided captions — no audio download/transcription needed.

Note on reliability: a transcript tells you what was *said*, not whether
it was correct or well-reasoned. Finance YouTube ranges from genuinely
informative to pure entertainment/hype (sometimes tied to the creator's own
positions). Treat extracted claims as leads to investigate, not conclusions.
"""
import re
from dataclasses import dataclass


@dataclass
class VideoContent:
    url: str
    video_id: str
    title: str
    transcript_text: str


def extract_video_id(url: str) -> str:
    patterns = [
        r"(?:v=|\/)([0-9A-Za-z_-]{11}).*",
        r"youtu\.be\/([0-9A-Za-z_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract a YouTube video ID from: {url}")


def fetch_transcript(url: str, title: str = "") -> VideoContent:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as e:
        raise ImportError(
            "youtube-transcript-api is required for video ingestion. "
            "Install with: pip install youtube-transcript-api"
        ) from e

    video_id = extract_video_id(url)
    # v1.x of this library replaced the old YouTubeTranscriptApi.get_transcript()
    # classmethod (list of dicts) with an instance method returning a
    # FetchedTranscript of FetchedTranscriptSnippet objects.
    transcript = YouTubeTranscriptApi().fetch(video_id)
    text = " ".join(snippet.text for snippet in transcript)

    return VideoContent(
        url=url,
        video_id=video_id,
        title=title,
        transcript_text=text,
    )
