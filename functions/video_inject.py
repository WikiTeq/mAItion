"""
title: Video Inject Filter
author: WikiTeq
date: 2025-05-20
version: 1.1
license: MIT
description: Reads VIDEO markers from tool messages and appends an inline player after the assistant response. Direct video file URLs use an HTML5 <video> tag; YouTube URLs use a clickable thumbnail linking out to YouTube (OWUI strips raw <iframe> embeds, so an inline player isn't available for YouTube on our pinned version).
"""

import logging
import re
from html import escape as html_escape
from html import unescape
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import BaseModel

log = logging.getLogger(__name__)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


def _extract_youtube_id(video_url: str) -> tuple[str | None, str | None]:
    """Return (video_id, start_time) if video_url is a YouTube URL, else (None, None).

    Handles watch/shorts/embed/live/youtu.be shapes, `v=` anywhere in the
    query string, and preserves a `t=`/`start=` timestamp if present.
    """
    try:
        parsed = urlparse(video_url)
    except ValueError:
        return None, None

    host = parsed.hostname or ""
    video_id = None

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif host in YOUTUBE_HOSTS:
        path_parts = [p for p in parsed.path.split("/") if p]
        if path_parts and path_parts[0] in ("shorts", "embed", "live") and len(path_parts) > 1:
            video_id = path_parts[1]
        elif parsed.path in ("/watch", "/"):
            query = parse_qs(parsed.query)
            video_id = (query.get("v") or [None])[0]

    if not video_id or not _VIDEO_ID_RE.match(video_id):
        return None, None

    query = parse_qs(parsed.query)
    start_time = (query.get("t") or query.get("start") or [None])[0]
    return video_id, start_time


class Filter:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def outlet(self, body: dict, __user__=None, __event_emitter__=None) -> dict:
        messages = body.get("messages", [])
        log.info("video_inject outlet called, %d messages", len(messages))

        video_url = None
        marker_re = re.compile(r"<!--VIDEO:(https://[^\s>]+)-->")
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if not isinstance(content, str):
                continue
            m = marker_re.search(unescape(content))
            if m:
                video_url = m.group(1)
                log.info("video_inject: found marker in role=%s", msg.get("role"))
                break

        if not video_url:
            log.info("video_inject: no video marker found")
            return body

        log.info("video_inject: injecting player for %s", video_url[:80])

        # Strip markers in all encoded forms from all messages.
        # 1. Raw:          <!--VIDEO:https://...-->
        # 2. HTML-escaped: &lt;!--VIDEO:https://...--&gt;  (inside <details result="...">)
        # 3. JSON+HTML:    <!--VIDEO:https://...->  (JSON-encoded in result attr)
        strip_patterns = [
            re.compile(r"<!--VIDEO:https://[^\s>]+-->"),
            re.compile(r"&lt;!--VIDEO:https://[^\s&]+--&gt;"),
            re.compile(r"\\u003c!--VIDEO:https://[^\s\\]+--\\u003e"),
        ]

        def strip_markers(text: str) -> str:
            for p in strip_patterns:
                text = p.sub("", text)
            return text

        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = strip_markers(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = strip_markers(part["text"])

        video_id, start_time = _extract_youtube_id(video_url)
        if video_id:
            query_params = {"v": video_id}
            if start_time:
                query_params["t"] = start_time
            watch_url = f"https://www.youtube.com/watch?{urlencode(query_params)}"
            safe_watch_url = html_escape(watch_url, quote=True)
            safe_thumb_url = html_escape(f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg", quote=True)
            # Note: OWUI's markdown renderer only special-cases raw HTML blocks
            # containing "<video" — any other raw HTML (e.g. a plain <img>) is
            # printed as literal text, not rendered. Use markdown image/link
            # syntax instead; marked renders both natively.
            #
            # The thumbnail is deliberately NOT wrapped in a link to the video:
            # OWUI renders every markdown image through its own lightbox
            # component, and wrapping it in an <a> caused the lightbox to load
            # the YouTube watch page inside the overlay while a second tab also
            # opened — a confusing double-open. The plain "Watch on YouTube"
            # link below is the only clickable way to reach the video.
            video_block = (
                f"![YouTube video thumbnail]({safe_thumb_url})\n\n"
                f"[▶ Watch on YouTube]({safe_watch_url})"
            )
        else:
            safe_url = html_escape(video_url, quote=True)
            ext = video_url.rsplit(".", 1)[-1].split("?")[0].lower()
            mime_types = {"mp4": "video/mp4", "webm": "video/webm", "ogg": "video/ogg", "mov": "video/quicktime"}
            mime = mime_types.get(ext, "video/mp4")
            video_block = f'<video controls style="max-width:100%">\n<source src="{safe_url}" type="{mime}">\n</video>'

        for msg in reversed(messages):
            if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
                msg["content"] = f"{msg['content']}\n\n{video_block}"
                log.info("video_inject: appended player for %s", video_url[:80])
                break

        return body
