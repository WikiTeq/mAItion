"""
title: MediaWiki Write Tool
author: WikiTeq
date: 2025-04-30
version: 1.0
license: MIT
description: Allows the AI to save content as a new or updated MediaWiki page when the user asks to save something to the wiki or knowledge base.
requirements: mwclient>=0.10.1, pydantic>=2.0.0
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlparse

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255
MAX_CONTENT_LENGTH = 2_000_000  # 2 MB, MediaWiki default max

# Characters illegal in MediaWiki page titles: #<>[]|{} plus control chars 0-31 and DEL (127)
_ILLEGAL_TITLE_CHARS = re.compile(r"[#<>\[\]|{}\x00-\x1f\x7f]")


def _parse_wiki_url(wiki_url: str) -> tuple[str, str, str]:
    """
    Parse an api.php URL into (host, path, scheme) for mwclient.Site.

    Requires the full URL to the api.php script, e.g.:
      https://example.com/w/api.php   -> ("example.com", "/w/", "https")
      http://example.com/api.php      -> ("example.com", "/", "http")
      https://example.com/abc/api.php -> ("example.com", "/abc/", "https")
    """
    wiki_url = wiki_url.strip()

    if not wiki_url.startswith("http://") and not wiki_url.startswith("https://"):
        raise ValueError("wiki_url must start with http:// or https://. Example: https://wiki.example.com/w/api.php")

    parsed = urlparse(wiki_url)
    scheme = parsed.scheme

    netloc = parsed.hostname or ""
    if not netloc:
        raise ValueError("wiki_url has no host. Example: https://wiki.example.com/w/api.php")
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    host = netloc

    # Strip api.php (with optional trailing slash) from path, then ensure trailing slash
    path = parsed.path
    # Remove trailing slash before checking for api.php suffix
    path_stripped = path.rstrip("/")
    if path_stripped.endswith("/api.php"):
        path = path_stripped[: -len("/api.php")] + "/"
    elif path_stripped == "api.php":
        path = "/"
    else:
        path = path_stripped.rstrip("/") + "/"

    return host, path, scheme


def _validate_title(title: str) -> None:
    """Raise ValueError if title is invalid for NS_MAIN writes."""
    if ":" in title:
        raise ValueError(
            "Page title must not contain ':'. Only NS_MAIN (main namespace) pages are supported. "
            "Use a plain title like 'Meeting Notes 2025-04-30'."
        )
    m = _ILLEGAL_TITLE_CHARS.search(title)
    if m:
        raise ValueError(
            f"Page title contains an illegal character: {m.group()!r}. "
            "Titles must not contain: # < > [ ] | { } or control characters."
        )


def _build_page_url(scheme: str, host: str, article_path: str, title: str) -> str:
    """Build a canonical page URL with proper title encoding."""
    # MediaWiki uses underscores and percent-encoding in URLs
    encoded = quote(title.replace(" ", "_"), safe="/:")
    return f"{scheme}://{host}{article_path.replace('$1', encoded)}"


class Tools:
    class Valves(BaseModel):
        wiki_url: str = Field(
            default="",
            description="Full URL to the MediaWiki api.php script, e.g. https://wiki.example.com/w/api.php or http://wiki.example.com/api.php. Must include http:// or https://.",
        )
        username: str = Field(
            default="",
            description="MediaWiki username. For production wikis, use a BotPassword (Special:BotPasswords) in the format 'Username@BotName'.",
        )
        password: str = Field(
            default="",
            description="MediaWiki password or BotPassword token.",
        )
        timeout: int = Field(
            default=30,
            description="Request timeout in seconds.",
        )
        edit_summary: str = Field(
            default="Saved via mAItion AI assistant",
            description="Edit summary recorded in the wiki page history.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def save_to_wiki(
        self,
        title: str,
        content: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Save content to a MediaWiki page. Use this tool when the user asks to:
        - "save into wiki" / "save into knowledge base"
        - "write to wiki" / "create a wiki page"
        - "update the wiki page" / "add this to the wiki"

        The tool creates a new page or updates an existing one with the given title and content.

        IMPORTANT: Before calling this tool, convert the content to MediaWiki markup format.
        Use == Headings ==, '''bold''', ''italic'', * bullet lists, # numbered lists,
        [[Internal links]], and [https://example.com External links] as appropriate.

        Title rules (MUST follow):
        - Only main-namespace pages are supported — the title must NOT contain ':'
        - Maximum length is 255 characters
        - The following characters are ILLEGAL and must not appear in the title:
          # < > [ ] | { } and any control characters (ASCII 0-31 and 127)

        After this tool returns successfully, respond with only the page URL.
        Do NOT repeat or summarise the page content.

        Args:
            title: The wiki page title (e.g. "Meeting Notes 2025-04-30")
            content: The page content formatted as MediaWiki markup

        Returns:
            A URL to the created or updated wiki page, or an error message.
        """
        import mwclient

        async def emit(message: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": message, "done": done}})

        # --- Validate configuration ---
        if not self.valves.wiki_url:
            await emit("MediaWiki URL is not configured in Tool Valves.", done=True)
            return "Error: wiki_url is not configured."
        if not self.valves.username or not self.valves.password:
            await emit("MediaWiki credentials are not configured in Tool Valves.", done=True)
            return "Error: username and password are not configured."

        # --- Validate inputs ---
        title = title.strip()
        if not title:
            return "Error: page title cannot be empty."
        if len(title) > MAX_TITLE_LENGTH:
            return f"Error: page title exceeds maximum length of {MAX_TITLE_LENGTH} characters."
        if len(content.encode("utf-8")) > MAX_CONTENT_LENGTH:
            return f"Error: content exceeds maximum allowed size of {MAX_CONTENT_LENGTH // 1_000_000} MB."

        # --- Title validation (namespace + illegal chars) ---
        try:
            _validate_title(title)
        except ValueError as e:
            await emit(str(e), done=True)
            return f"Error: {e}"

        # --- Parse wiki URL ---
        try:
            host, path, scheme = _parse_wiki_url(self.valves.wiki_url)
        except ValueError as e:
            await emit(str(e), done=True)
            return f"Error: {e}"

        await emit(f"Connecting to {host}…")

        # --- Connect and authenticate (blocking — run in thread) ---
        def _connect():
            site = mwclient.Site(
                host,
                path=path,
                scheme=scheme,
                reqs={"timeout": self.valves.timeout},
            )
            site.login(self.valves.username, self.valves.password)
            return site

        try:
            site = await asyncio.to_thread(_connect)
        except mwclient.errors.LoginError:
            await emit("Authentication failed. Check your username and password in Tool Valves.", done=True)
            return "Error: authentication failed. If using a BotPassword, the format is 'Username@BotName'."
        except Exception:
            log.error("mwclient connection error", exc_info=True)
            await emit("Could not connect to the wiki.", done=True)
            return "Error: could not connect to the wiki. Check the wiki_url in Tool Valves."

        await emit(f"Saving page «{title}»…")

        # --- Save the page (blocking — run in thread) ---
        def _save():
            page = site.pages[title]
            page.save(content, summary=self.valves.edit_summary)

        try:
            await asyncio.to_thread(_save)
        except mwclient.errors.ProtectedPageError:
            await emit(f"Page «{title}» is protected and cannot be edited.", done=True)
            return f"Error: page «{title}» is protected."
        except mwclient.errors.APIError as e:
            log.error("MediaWiki API error: %s", e.code)
            await emit("Wiki API error while saving.", done=True)
            return f"Error: wiki API returned an error ({e.code}). Check page title and permissions."
        except Exception:
            log.error("Unexpected error saving page", exc_info=True)
            await emit("An unexpected error occurred while saving.", done=True)
            return "Error: an unexpected error occurred. Check the server logs for details."

        # --- Build canonical page URL (blocking — run in thread) ---
        await emit("Fetching page URL…")

        def _get_article_path():
            result = site.api("query", meta="siteinfo", siprop="general")
            return result["query"]["general"].get("articlepath", "/wiki/$1")

        try:
            article_path = await asyncio.to_thread(_get_article_path)
            page_url = _build_page_url(scheme, host, article_path, title)
        except Exception:
            page_url = _build_page_url(scheme, host, "/wiki/$1", title)

        await emit(f"Saved: {page_url}", done=True)
        return page_url
