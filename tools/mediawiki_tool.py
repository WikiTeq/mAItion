"""
title: MediaWiki Search & Write Tool
author: WikiTeq
date: 2025-04-30
version: 1.0
license: MIT
description: Allows creating new or updating existing MediaWiki pages when the user asks to save or update something to the wiki/knowledge base. Allows AI to search the wiki for pages.
requirements: mwclient>=0.10.1, pydantic>=2.0.0, markdownify>=0.13.1
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255
MAX_CONTENT_LENGTH = 2_000_000  # 2 MB, MediaWiki default max
MAX_SEARCH_RESULTS = 20

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


def _get_article_path(site) -> str:
    try:
        result = site.api("query", meta="siteinfo", siprop="general")
        return result["query"]["general"].get("articlepath", "/wiki/$1")
    except Exception:
        log.warning("Could not fetch articlepath from siteinfo; falling back to /wiki/$1", exc_info=True)
        return "/wiki/$1"


def _connect_site(host: str, path: str, scheme: str, timeout: int, username: str, password: str):
    """Connect to a MediaWiki site, logging in only when credentials are provided."""
    # Lazy import: OWUI loads this file before installing requirements, so mwclient
    # is not available at module load time — only at call time.
    import mwclient

    has_credentials = bool(username and password)
    site = mwclient.Site(
        host,
        path=path,
        scheme=scheme,
        force_login=has_credentials,
        reqs={"timeout": timeout},
    )
    if has_credentials:
        site.login(username, password)
    return site


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
        max_search_results: int = Field(
            default=10,
            description=f"Maximum number of search results to return (1–{MAX_SEARCH_RESULTS}).",
        )
        max_page_chars: int = Field(
            default=20_000,
            ge=1,
            le=500_000,
            description=(
                "Maximum characters to include per page in search results."
                " Longer pages are truncated. Range: 1–500,000."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    async def search_wiki(
        self,
        query: str,
        content_format: Literal["wikitext", "html", "markdown"] = "wikitext",
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Search the MediaWiki wiki for pages matching a query and return their page content.

        Use this tool when the user asks to:
        - "search the wiki for ..." / "find wiki pages about ..."
        - "look up ... in the knowledge base"
        - "what does the wiki say about ..."

        The tool runs a full-text search (equivalent to Special:Search) and fetches the
        content of each matching page. The content_format argument selects the output:
        'wikitext' (raw markup, default), 'html' (parsed HTML), or 'markdown'
        (parsed HTML converted to Markdown). Prefer 'html' or 'markdown' when pages may
        contain templates, transclusions, or query results, since raw wikitext does not
        show their rendered output.

        Args:
            query: The search query string.
            content_format: Output format for page content — 'wikitext' (raw MediaWiki
                markup, default), 'html' (parsed HTML via the parse API), or 'markdown'
                (parsed HTML converted to Markdown).

        Returns:
            A formatted string with each result's title, URL, and page content, or an error.
        """
        import mwclient

        async def emit(message: str, done: bool = False, hidden: bool = True) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": message, "done": done, "hidden": hidden}})

        # --- Validate configuration ---
        if not self.valves.wiki_url:
            await emit("Error: MediaWiki URL is not configured in Tool Valves.", done=True, hidden=False)
            return "Error: wiki_url is not configured."
        query = query.strip()
        if not query:
            await emit("Error: search query cannot be empty.", done=True, hidden=False)
            return "Error: search query cannot be empty."

        effective_limit = max(1, min(self.valves.max_search_results, MAX_SEARCH_RESULTS))

        # --- Parse wiki URL ---
        try:
            host, path, scheme = _parse_wiki_url(self.valves.wiki_url)
        except ValueError as e:
            await emit(f"Error: {e}", done=True, hidden=False)
            return f"Error: {e}"

        await emit(f"Connecting to {host}…")

        try:
            site = await asyncio.to_thread(
                _connect_site,
                host,
                path,
                scheme,
                self.valves.timeout,
                self.valves.username,
                self.valves.password,
            )
        except mwclient.errors.LoginError:
            await emit("Error: authentication failed. Check your username and password in Tool Valves.", done=True, hidden=False)
            return "Error: authentication failed. If using a BotPassword, the format is 'Username@BotName'."
        except Exception:
            log.error("mwclient connection error", exc_info=True)
            await emit("Error: could not connect to the wiki.", done=True, hidden=False)
            return "Error: could not connect to the wiki. Check the wiki_url in Tool Valves."

        await emit("Fetching wiki article path…")
        article_path = await asyncio.to_thread(_get_article_path, site)

        await emit(f"Searching for '{query}'...")

        def _search():
            results = []
            for item in site.search(query, what="text", limit=effective_limit):
                results.append(item["title"])
                if len(results) >= effective_limit:
                    break
            return results

        try:
            titles = await asyncio.to_thread(_search)
        except mwclient.errors.APIError as e:
            if e.code in ("readapidenied", "permissiondenied"):
                await emit("Error: this wiki requires login to search.", done=True, hidden=False)
                return "Error: this wiki requires authentication to search. Please configure username and password in Tool Valves."
            log.error("MediaWiki API error: %s", e.code)
            await emit(f"Error: wiki search failed ({e.code}).", done=True, hidden=False)
            return f"Error: wiki API returned an error ({e.code})."
        except Exception:
            log.error("Unexpected error during search", exc_info=True)
            await emit("Error: unexpected error during search.", done=True, hidden=False)
            return "Error: unexpected error during search."

        if not titles:
            await emit("No results found.", done=True, hidden=False)
            return f"No wiki pages found matching '{query}'."

        await emit(f"Fetching content for {len(titles)} page(s)...")

        def _fetch_page(title: str, fmt: str) -> tuple[str, str]:
            try:
                page = site.pages[title]
                if not page.exists:
                    return title, "(Page not found — may have been deleted)"
                if fmt == "wikitext":
                    # Return wikitext as-is, including #REDIRECT stubs — models
                    # can parse the notation and follow up, and silently
                    # resolving it here would hide redirect pages from queries
                    # that ask for them specifically.
                    return title, page.text()
                # For html/markdown, resolve #REDIRECT pages to their target so
                # the parse API call below uses the target's canonical page.name
                # (redirects=True also follows redirects server-side, but
                # resolving client-side here keeps page.name accurate for URLs).
                target = page.redirects_to()
                if target is not None:
                    page = target
                result = site.api("parse", page=page.name, prop="text", redirects=True)
                html = result["parse"]["text"]["*"]
                if fmt == "markdown":
                    # Lazy import: only needed in markdown mode.
                    from markdownify import markdownify as md

                    # Strip MediaWiki edit-section spans so "[edit]" links
                    # don't leak into the markdown output.
                    # HTML structure:
                    #   <span class="mw-editsection">
                    #     <span class="mw-editsection-bracket">[</span>
                    #     <a ...>edit source</a>
                    #     <span class="mw-editsection-bracket">]</span>
                    #   </span>
                    # The non-greedy regex stops at the FIRST </span>, so
                    # remove inner brackets first — then the outer span has
                    # no nested spans and matches correctly.
                    html = re.sub(
                        r'<span class="mw-editsection-bracket">.*?</span>',
                        "",
                        html,
                        flags=re.DOTALL,
                    )
                    html = re.sub(
                        r'<span class="mw-editsection">.*?</span>',
                        "",
                        html,
                        flags=re.DOTALL,
                    )
                    # Do NOT pass strip=["script", "style"] here: markdownify
                    # already drops <script>/<style> content via dedicated
                    # no-op converters, but explicitly stripping those tags
                    # disables that converter and falls back to naive text
                    # extraction — leaking raw CSS/JS (e.g. Wikipedia's
                    # TemplateStyles <style> blocks) into the output instead
                    # of removing it.
                    content = md(html)
                else:
                    content = html
                return title, content
            except Exception as e:
                log.warning("Failed to fetch %r: %s", title, e)
                return title, "(Content unavailable)"

        pages: list[tuple[str, str]] = await asyncio.gather(
            *[asyncio.to_thread(_fetch_page, t, content_format) for t in titles]
        )

        sections = []
        sources = []
        cap = self.valves.max_page_chars
        _fetch_errors = {"(Page not found — may have been deleted)", "(Content unavailable)"}
        for i, (title, content) in enumerate(pages, start=1):
            if len(content) > cap:
                content = content[:cap] + f"\n...(truncated {len(content) - cap} chars)"
            url = _build_page_url(scheme, host, article_path, title)
            sections.append(f"=== Result {i}: {title} ===\nURL: {url}\n\nPage content: {content}\n")
            if content not in _fetch_errors:
                sources.append(
                    {
                        "source": {"name": title, "url": url},
                        "document": [content],
                        "metadata": [{"source": title, "url": url}],
                    }
                )

        if __event_emitter__:
            for src in sources:
                await __event_emitter__({"type": "source", "data": src})

        await emit(f"Found {len(pages)} result(s) for '{query}'.", done=True)
        return f"Search results for '{query}' ({len(pages)} page(s)):\n\n" + "\n---\n\n".join(sections)

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

        Content size limit: 2,000,000 characters maximum.

        Args:
            title: The wiki page title (e.g. "Meeting Notes 2025-04-30")
            content: The page content formatted as MediaWiki markup

        Returns:
            A URL to the created or updated wiki page, or an error message.
        """
        import mwclient

        async def emit(message: str, done: bool = False, hidden: bool = True) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": message, "done": done, "hidden": hidden}})

        # --- Validate configuration ---
        if not self.valves.wiki_url:
            await emit("Error: MediaWiki URL is not configured in Tool Valves.", done=True, hidden=False)
            return "Error: wiki_url is not configured."
        # --- Validate inputs ---
        title = title.strip()
        if not title:
            msg = "Error: page title cannot be empty."
            await emit(msg, done=True, hidden=False)
            return msg
        if len(title) > MAX_TITLE_LENGTH:
            msg = f"Error: page title exceeds maximum length of {MAX_TITLE_LENGTH} characters."
            await emit(msg, done=True, hidden=False)
            return msg
        if len(content.encode("utf-8")) > MAX_CONTENT_LENGTH:
            msg = f"Error: content exceeds maximum allowed size of {MAX_CONTENT_LENGTH // 1_000_000} MB."
            await emit(msg, done=True, hidden=False)
            return msg

        # --- Title validation (namespace + illegal chars) ---
        try:
            _validate_title(title)
        except ValueError as e:
            await emit(f"Error: {e}", done=True, hidden=False)
            return f"Error: {e}"

        # --- Parse wiki URL ---
        try:
            host, path, scheme = _parse_wiki_url(self.valves.wiki_url)
        except ValueError as e:
            await emit(f"Error: {e}", done=True, hidden=False)
            return f"Error: {e}"

        await emit(f"Connecting to {host}…")

        # --- Connect and optionally authenticate (blocking — run in thread) ---
        try:
            site = await asyncio.to_thread(
                _connect_site,
                host,
                path,
                scheme,
                self.valves.timeout,
                self.valves.username,
                self.valves.password,
            )
        except mwclient.errors.LoginError:
            await emit("Error: authentication failed. Check your username and password in Tool Valves.", done=True, hidden=False)
            return "Error: authentication failed. If using a BotPassword, the format is 'Username@BotName'."
        except Exception:
            log.error("mwclient connection error", exc_info=True)
            await emit("Error: could not connect to the wiki.", done=True, hidden=False)
            return "Error: could not connect to the wiki. Check the wiki_url in Tool Valves."

        await emit(f"Saving page «{title}»…")

        # --- Save the page (blocking — run in thread) ---
        def _save():
            page = site.pages[title]
            page.save(content, summary=self.valves.edit_summary)

        try:
            await asyncio.to_thread(_save)
        except mwclient.errors.ProtectedPageError:
            await emit(f"Error: page «{title}» is protected and cannot be edited.", done=True, hidden=False)
            return f"Error: page «{title}» is protected."
        except mwclient.errors.APIError as e:
            if e.code in ("writeapidenied", "permissiondenied"):
                await emit("Error: this wiki requires login to write.", done=True, hidden=False)
                return "Error: this wiki requires authentication to write. Please configure username and password in Tool Valves."
            log.error("MediaWiki API error: %s", e.code)
            await emit(f"Error: wiki save failed ({e.code}).", done=True, hidden=False)
            return f"Error: wiki API returned an error ({e.code}). Check page title and permissions."
        except Exception:
            log.error("Unexpected error saving page", exc_info=True)
            await emit("Error: an unexpected error occurred while saving.", done=True, hidden=False)
            return "Error: an unexpected error occurred. Check the server logs for details."

        # --- Build canonical page URL (blocking — run in thread) ---
        await emit("Fetching page URL…")

        article_path = await asyncio.to_thread(_get_article_path, site)
        page_url = _build_page_url(scheme, host, article_path, title)

        await emit(f"Saved: {page_url}", done=True)
        return page_url
