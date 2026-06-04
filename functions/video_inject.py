"""
title: Video Inject Filter
author: WikiTeq
date: 2025-05-20
version: 1.0
license: MIT
description: Reads VIDEO markers from tool messages and appends an inline <video> player after the assistant response.
"""

import logging
import re
from html import escape as html_escape
from html import unescape

from pydantic import BaseModel

log = logging.getLogger(__name__)


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
