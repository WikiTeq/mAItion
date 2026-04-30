"""
title: MediaWiki Write Tool
author: WikiTeq
date: 2025-04-30
version: 1.1
license: MIT
description: Allows the AI to save content as a new or updated MediaWiki page when the user asks to save something to the wiki or knowledge base.
requirements: mwclient>=0.10.1, pydantic>=2.0.0
"""

import asyncio
import ipaddress
import logging
import socket
from typing import Callable, Awaitable, Optional
from urllib.parse import urlparse, quote
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("::ffff:0:0/96"),
]

MAX_TITLE_LENGTH = 255
MAX_CONTENT_LENGTH = 2_000_000  # 2 MB, MediaWiki default max

_SSRF_ALLOWLIST = {"host.docker.internal", "localhost", "127.0.0.1"}

_BLOCKED_NAMESPACES = {
    "mediawiki", "template", "module", "gadget", "gadget definition",
}


def _parse_wiki_url(wiki_url: str) -> tuple[str, str, str]:
    """
    Parse a wiki URL into (host, path, scheme) for mwclient.Site.

    Requires a full URL with scheme (http:// or https://).
    Accepts forms like:
      https://example.com/w/         -> ("example.com", "/w/", "https")
      https://example.com/wiki/      -> ("example.com", "/w/", "https")
      https://example.com/index.php  -> ("example.com", "/", "https")
      https://example.com            -> ("example.com", "/w/", "https")
      http://localhost:8080          -> ("localhost:8080", "/w/", "http")
    """
    wiki_url = wiki_url.strip()

    # Require explicit scheme to avoid mis-parsing bare hostnames
    if not wiki_url.startswith("http://") and not wiki_url.startswith("https://"):
        raise ValueError(
            "wiki_url must start with http:// or https://. "
            "Example: https://wiki.example.com"
        )

    parsed = urlparse(wiki_url)
    scheme = parsed.scheme  # guaranteed to be http or https now

    # Strip userinfo from netloc (user:pass@host -> host)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    host = netloc

    path = parsed.path.rstrip("/") or ""
    if path == "/wiki" or path.startswith("/wiki/"):
        path = "/w/"
    elif path == "/index.php" or path.startswith("/index.php/"):
        path = "/"
    elif path == "" or path == "/":
        path = "/w/"
    else:
        path = path.rstrip("/") + "/"

    return host, path, scheme


def _check_ssrf(host: str) -> None:
    """Raise ValueError if host resolves to any private/loopback address.

    Checks ALL returned addresses (not just the first) to prevent bypass
    via multi-A DNS records. localhost and host.docker.internal are
    explicitly allowed for local dev/testing.
    """
    # host may include port (e.g. "localhost:8080") — strip it
    hostname = host.rsplit(":", 1)[0].strip("[]")  # handles IPv6 [::1]:port too

    if hostname in _SSRF_ALLOWLIST:
        return

    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        raise ValueError(f"Could not resolve wiki host: {hostname!r}")

    for result in results:
        addr_str = result[4][0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        # Unwrap IPv4-mapped IPv6 addresses (::ffff:x.x.x.x)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                raise ValueError(
                    "Wiki URL resolves to a private/internal address and is not allowed."
                )


def _check_namespace(title: str) -> None:
    """Raise ValueError if title targets a restricted MediaWiki namespace."""
    if ":" in title:
        ns = title.split(":", 1)[0].strip().lower()
        if ns in _BLOCKED_NAMESPACES:
            raise ValueError(
                f"Writing to the '{ns.capitalize()}:' namespace is not allowed."
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
            description="Full URL to the MediaWiki instance, e.g. https://wiki.example.com/w/ or https://wiki.example.com. Must include http:// or https://.",
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
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
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

        Args:
            title: The wiki page title (e.g. "Meeting Notes 2025-04-30")
            content: The page content formatted as MediaWiki markup

        Returns:
            A URL to the created or updated wiki page, or an error message.
        """
        import mwclient

        async def emit(message: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": message, "done": done}}
                )

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

        # --- Namespace guard ---
        try:
            _check_namespace(title)
        except ValueError as e:
            await emit(str(e), done=True)
            return f"Error: {e}"

        # --- Parse and validate wiki URL ---
        try:
            host, path, scheme = _parse_wiki_url(self.valves.wiki_url)
        except ValueError as e:
            await emit(str(e), done=True)
            return f"Error: {e}"

        # --- SSRF check (runs in thread — getaddrinfo blocks) ---
        try:
            await asyncio.to_thread(_check_ssrf, host)
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
        def _get_article_path():
            result = site.api("query", meta="siteinfo", siprop="general")
            return result["query"]["general"].get("articlepath", "/wiki/$1")

        try:
            article_path = await asyncio.to_thread(_get_article_path)
            page_url = _build_page_url(scheme, host, article_path, title)
        except Exception:
            page_url = _build_page_url(scheme, host, "/wiki/$1", title)

        await emit(f"Saved: {page_url}", done=True)
        return f"Page saved successfully: {page_url}"
