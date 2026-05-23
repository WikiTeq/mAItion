"""
title: Video Inject Filter
author: WikiTeq
date: 2025-05-20
version: 1.0
license: MIT
description: Reads <!--VIDEO:url--> markers from tool messages and appends an inline <video> player after the assistant response.
"""

import logging
import re
from html import unescape
from urllib.parse import quote as url_quote

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
        for msg in reversed(messages):
            role = msg.get("role")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if not isinstance(content, str):
                continue
            log.debug("video_inject role=%s content_len=%d", role, len(content))
            m = re.search(r"<!--VIDEO:(https://[^\s>]+)-->", unescape(content))
            if m:
                video_url = m.group(1)
                log.info("video_inject: found marker in role=%s", role)
                break

        if not video_url:
            log.info("video_inject: no video marker found")
            return body

        safe_url = url_quote(video_url, safe=":/?=&%#@!$,;~")
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
