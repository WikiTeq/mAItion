"""
title: MediaWiki Search & Write Tool
author: WikiTeq
date: 2025-04-30
version: 1.1
license: MIT
description: Allows creating new or updating existing MediaWiki pages when the user asks to save or update something to the wiki/knowledge base. Allows AI to search the wiki for pages.
requirements: mwclient>=0.11.0, pydantic>=2.0.0, requests>=2.0.0, markdownify>=0.13.1
"""

import asyncio
import ipaddress
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlparse

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255
MAX_CONTENT_LENGTH = 2_000_000  # 2 MB, MediaWiki default max
MAX_SEARCH_RESULTS = 20
MAX_ERROR_DETAIL_CHARS = 500


def _truncate(text: str, limit: int = MAX_ERROR_DETAIL_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    suffix = "... (truncated)"
    return text[: limit - len(suffix)] + suffix


# Characters illegal in MediaWiki page titles: #<>[]|{} plus control chars 0-31 and DEL (127)
_ILLEGAL_TITLE_CHARS = re.compile(r"[#<>\[\]|{}\x00-\x1f\x7f]")


def to_source_id(text: str) -> str:
    """Slugify a source name into an id: spaces -> hyphens, strip non-alnum/hyphen, lowercase,
    with a short digest of the original text appended for uniqueness.

    The digest is always appended, not just when the slug is empty: stripping
    punctuation means distinct titles like "C++", "C#", and "C" would otherwise
    all slugify to the same "c" and collide. Titles made up entirely of
    non-ASCII characters (e.g. "日本語") strip down to an empty slug, in which
    case the id falls back to the digest alone.

    Duplicated verbatim in roat_retrieval.py — OWUI loads each tool's source
    as an independent module (no shared import path between tools), so keep
    both copies in sync if this changes.
    """
    text_with_hyphens = text.replace(" ", "-")
    slug = re.sub(r"[^a-zA-Z0-9-]", "", text_with_hyphens).lower()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    if slug:
        return f"{slug}-{digest}"
    return f"src-{digest}"


def _format_ip_for_netloc(ip: str) -> str:
    """Bracket IPv6 addresses for use in a URL netloc (IPv4 / already-bracketed unchanged)."""
    ip = (ip or "").strip()
    if not ip:
        return ip
    if ":" in ip and not ip.startswith("["):
        return f"[{ip}]"
    return ip


def _host_header_from_url(parsed) -> str:
    """
    Build a Host header from a parsed URL: hostname + explicit port, no userinfo.

    Uses netloc (minus userinfo) so non-default ports are preserved, unlike parsed.hostname.
    """
    netloc = parsed.netloc or ""
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[-1]
    return netloc


def _netloc_with_ip(parsed, dest_ip: str) -> str:
    """Replace the host in a parsed URL with dest_ip, keeping the original port."""
    dest = _format_ip_for_netloc(dest_ip)
    if parsed.port is not None:
        return f"{dest}:{parsed.port}"
    return dest


class HostOverrideAdapter:
    """
    requests adapter that resolves a hostname to a fixed IP (curl --resolve style).

    TCP connects to the override IP; TLS SNI and cert validation keep the original hostname.
    Implemented as a thin subclass factory so requests is only imported at call time
    (OWUI installs requirements after loading the tool module).
    """

    @staticmethod
    def create(dest_ip: str, dest_hostname: str):
        from requests.adapters import HTTPAdapter

        class _HostOverrideAdapter(HTTPAdapter):
            def __init__(self, dest_ip: str, dest_hostname: str, **kwargs):
                self._dest_ip = dest_ip
                self._dest_hostname = dest_hostname
                super().__init__(**kwargs)

            def init_poolmanager(self, *args, **kwargs):
                """Keep original hostname for TLS SNI / cert validation."""
                kwargs["server_hostname"] = self._dest_hostname
                super().init_poolmanager(*args, **kwargs)

            def send(self, request, **kwargs):
                """Rewrite URL hostname -> override IP; force the original Host header (incl. port)."""
                parsed = urlparse(request.url)
                # Force (not setdefault) so a Host key from the headers valve
                # can't silently defeat the Host/SNI guarantee this adapter exists for.
                request.headers["Host"] = _host_header_from_url(parsed)
                if parsed.hostname:
                    request.url = parsed._replace(
                        netloc=_netloc_with_ip(parsed, self._dest_ip)
                    ).geturl()
                return super().send(request, **kwargs)

        return _HostOverrideAdapter(
            dest_ip=dest_ip, dest_hostname=dest_hostname
        )


def _hostname_for_sni(host: str) -> str:
    """
    Strip optional port from host netloc for TLS SNI.

    Expects host in the shape produced by _parse_wiki_url: "hostname[:port]",
    "ipv4[:port]", or "[ipv6][:port]" (IPv6 literals always bracketed).
    """
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host
    # hostname:port or ipv4:port — never a bare (unbracketed) IPv6 literal,
    # since _parse_wiki_url always brackets those.
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def _parse_headers(raw: str | dict | None) -> dict[str, str] | None:
    """Parse optional extra headers from a JSON string or dict. Empty -> None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        if not raw:
            return None
        return {str(k): str(v) for k, v in raw.items()}
    text = str(raw).strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"headers must be a valid JSON object, e.g. "
            f'{{"Authorization": "Bearer token"}}. Parse error: {e}'
        ) from e
    if not isinstance(data, dict):
        raise ValueError(
            'headers must be a JSON object, e.g. {"Authorization": "Bearer token"}.'
        )
    return {str(k): str(v) for k, v in data.items()}


def _parse_wiki_url(wiki_url: str) -> tuple[str, str, str]:
    """
    Parse an api.php URL into (host, path, scheme) for mwclient.Site.

    Requires the full URL to the api.php script, e.g.:
      https://example.com/w/api.php   -> ("example.com", "/w/", "https")
      http://example.com/api.php      -> ("example.com", "/", "http")
      https://example.com/abc/api.php -> ("example.com", "/abc/", "https")
    """
    wiki_url = wiki_url.strip()

    if not wiki_url.startswith("http://") and not wiki_url.startswith(
        "https://"
    ):
        raise ValueError(
            "wiki_url must start with http:// or https://. Example: https://wiki.example.com/w/api.php"
        )

    parsed = urlparse(wiki_url)
    scheme = parsed.scheme

    netloc = parsed.hostname or ""
    if not netloc:
        raise ValueError(
            "wiki_url has no host. Example: https://wiki.example.com/w/api.php"
        )
    # Bracket bare IPv6 literals so a later host:port split is unambiguous
    # (urlparse.hostname always returns IPv6 addresses unbracketed).
    if ":" in netloc:
        netloc = f"[{netloc}]"
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


def _build_page_url(
    title: str, article_path: str, origin: str | None
) -> str | None:
    """Build a canonical, absolute page URL with proper title encoding.

    Combines 'origin' (an absolute scheme+host, e.g. 'https://example.com')
    with 'articlepath' (e.g. '/wiki/$1') to produce the full public-facing
    page URL. Returns None when origin is unavailable — a relative path
    would resolve against the OpenWebUI origin instead of the wiki's,
    which is worse than no URL at all.

    MediaWiki's own naming restrictions reject titles starting with './'
    or '../' (dot-segments), but a bare leading '/' (e.g. '/Foo') is not
    banned (see mediawiki.org/wiki/Manual:Page_title, "Naming restrictions").
    Combined with a root short-URL articlepath ('/$1'), such a title would
    otherwise produce a path like '//Foo', which urljoin() interprets as a
    network-path reference (resolving against a different host entirely)
    rather than a path on 'origin'. Collapsing leading slashes keeps the
    joined path anchored to origin regardless of title/articlepath shape.
    """
    if not origin:
        return None
    encoded = quote(title.replace(" ", "_"), safe="/:")
    path = article_path.replace("$1", encoded)
    path = "/" + path.lstrip("/")
    return urljoin(origin, path)


def _get_site_info(site) -> tuple[str, str | None]:
    """Fetch articlepath and the wiki's public origin from MediaWiki siteinfo.

    Returns (article_path, origin). origin is an absolute scheme+host
    (e.g. 'https://example.com') derived from siteinfo's 'base' field,
    which always contains the full absolute public wiki URL including
    scheme (e.g. 'https://example.com/wiki/Main_Page') — unlike 'server',
    which is commonly protocol-relative and, for tools connecting via a
    private/in-cluster wiki_url, may not even share the public scheme.
    Returns (article_path, None) if the API call fails or 'base' is
    missing/unparseable.
    """
    try:
        result = site.api("query", meta="siteinfo", siprop="general")
        general = result["query"]["general"]
        article_path = general.get("articlepath", "/wiki/$1")

        base = general.get("base", "")
        parsed_base = urlparse(base) if base else None
        if parsed_base and parsed_base.scheme and parsed_base.netloc:
            return article_path, f"{parsed_base.scheme}://{parsed_base.netloc}"

        return article_path, None
    except Exception:
        log.warning(
            "Could not fetch siteinfo; falling back to defaults", exc_info=True
        )
        return "/wiki/$1", None


def _store_turn_sources(request, sources: list) -> None:
    """Append sources to __request__.state._wikiteq_sources for get_sources tool.

    Duplicated verbatim in roat_retrieval.py — see to_source_id() above for why.
    """
    if not request or not sources:
        return
    try:
        turn_sources = getattr(request.state, "_wikiteq_sources", None)
        if turn_sources is None:
            turn_sources = []
            request.state._wikiteq_sources = turn_sources
        turn_sources.extend(sources)
    except Exception:
        log.debug("Could not store sources on request state", exc_info=True)


def _connect_site(
    host: str,
    path: str,
    scheme: str,
    timeout: int,
    username: str,
    password: str,
    *,
    verify_ssl: bool = True,
    resolve_ip: str = "",
    user_agent: str = "",
    headers: str | dict | None = None,
):
    """
    Connect to a MediaWiki site, logging in only when credentials are provided.

    Optional network overrides (off by default): verify_ssl, resolve_ip, user_agent, headers.
    When any override is set, builds a custom requests Session so Host/SNI, TLS, and headers
    apply to every API call (same approach as the RAGacy MediaWiki connector).
    """
    # Lazy import: OWUI loads this file before installing requirements, so mwclient
    # is not available at module load time — only at call time.
    import mwclient
    import requests
    from mwclient.client import USER_AGENT

    has_credentials = bool(username and password)
    resolve_ip = (resolve_ip or "").strip() or None
    if resolve_ip is not None:
        try:
            parsed_ip = ipaddress.ip_address(resolve_ip)
        except ValueError as e:
            raise ValueError(
                f"resolve_ip must be a valid IPv4 or IPv6 address, got {resolve_ip!r}"
            ) from e
        # Reject zone IDs (e.g. "fe80::1%eth0"): only meaningful for local
        # link-local interfaces, not a remote connect target, and not
        # representable cleanly in a URL netloc.
        if getattr(parsed_ip, "scope_id", None) is not None:
            raise ValueError(
                f"resolve_ip must not include a zone ID (scope), got {resolve_ip!r}"
            )
    user_agent = (user_agent or "").strip() or None
    custom_headers = _parse_headers(headers)

    use_custom_session = (
        (not verify_ssl)
        or bool(resolve_ip)
        or bool(custom_headers)
        or bool(user_agent)
    )

    if not use_custom_session:
        site = mwclient.Site(
            host,
            path=path,
            scheme=scheme,
            force_login=has_credentials,
            connection_options={"timeout": timeout},
        )
    else:
        session = requests.Session()
        # When pool is set, mwclient skips its default User-Agent — restore it
        # (or use the configured override). Dedicated user_agent wins over headers.
        session.headers["User-Agent"] = user_agent or USER_AGENT
        if custom_headers:
            session.headers.update(custom_headers)
            if user_agent:
                session.headers["User-Agent"] = user_agent

        # Passed to every session.request() by mwclient (timeout, verify, …)
        connection_options: dict[str, Any] = {"timeout": timeout}
        if not verify_ssl:
            session.verify = False
            connection_options["verify"] = False
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            log.warning(
                "SSL certificate verification is disabled for MediaWiki connection"
            )

        if resolve_ip:
            # curl --resolve: TCP to IP, Host/SNI still use original hostname
            sni_host = _hostname_for_sni(host)
            adapter = HostOverrideAdapter.create(
                dest_ip=resolve_ip, dest_hostname=sni_host
            )
            session.mount(f"{scheme}://{host}", adapter)
            log.info("DNS override for MediaWiki: %s -> %s", host, resolve_ip)

        site = mwclient.Site(
            host,
            path=path,
            scheme=scheme,
            force_login=has_credentials,
            pool=session,
            connection_options=connection_options,
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
        # Optional network overrides (reverse-proxy bypass / custom TLS). All off/default.
        verify_ssl: bool = Field(
            default=True,
            description="Verify TLS certificates when connecting to the wiki. Set false only for self-signed/dev certs.",
        )
        resolve_ip: str = Field(
            default="",
            description=(
                "Optional IP to connect to while keeping the wiki hostname for Host/SNI "
                "(like curl --resolve). Leave empty for normal DNS resolution."
            ),
        )
        user_agent: str = Field(
            default="",
            description="Optional HTTP User-Agent override. Leave empty to use the mwclient default.",
        )
        headers: str = Field(
            default="",
            description=(
                "Optional extra HTTP headers as a JSON object, e.g. "
                '{"Authorization": "Bearer token"}. Leave empty for none. '
                "user_agent valve wins over a User-Agent key here."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    async def search_wiki(
        self,
        query: str,
        content_format: Literal["wikitext", "html", "markdown"] = "wikitext",
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __request__=None,
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

        async def emit(
            message: str, done: bool = False, hidden: bool = True
        ) -> None:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": message,
                            "done": done,
                            "hidden": hidden,
                        },
                    }
                )

        # --- Validate configuration ---
        if not self.valves.wiki_url:
            await emit(
                "Error: MediaWiki URL is not configured in Tool Valves.",
                done=True,
                hidden=False,
            )
            return "Error: wiki_url is not configured."
        query = query.strip()
        if not query:
            await emit(
                "Error: search query cannot be empty.", done=True, hidden=False
            )
            return "Error: search query cannot be empty."

        effective_limit = max(
            1, min(self.valves.max_search_results, MAX_SEARCH_RESULTS)
        )

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
                verify_ssl=self.valves.verify_ssl,
                resolve_ip=self.valves.resolve_ip,
                user_agent=self.valves.user_agent,
                headers=self.valves.headers,
            )
        except ValueError as e:
            # Invalid resolve_ip / headers JSON / valve config
            await emit(f"Error: {e}", done=True, hidden=False)
            return f"Error: {e}"
        except mwclient.errors.LoginError as e:
            await emit(
                "Error: authentication failed. Check your username and password in Tool Valves.",
                done=True,
                hidden=False,
            )
            return (
                "Error: authentication failed. If using a BotPassword, the format is 'Username@BotName'. "
                f"Details: {_truncate(str(e))}"
            )
        except Exception as e:
            log.error("mwclient connection error", exc_info=True)
            await emit(
                "Error: could not connect to the wiki.",
                done=True,
                hidden=False,
            )
            return f"Error: could not connect to the wiki. Check the wiki_url in Tool Valves. Details: {_truncate(str(e))}"

        await emit("Fetching wiki site info…")
        article_path, origin = await asyncio.to_thread(_get_site_info, site)

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
                await emit(
                    "Error: this wiki requires login to search.",
                    done=True,
                    hidden=False,
                )
                return "Error: this wiki requires authentication to search. Please configure username and password in Tool Valves."
            log.error("MediaWiki API error: %s", e.code)
            await emit(
                f"Error: wiki search failed ({e.code}).",
                done=True,
                hidden=False,
            )
            return f"Error: wiki API returned an error ({e.code})."
        except Exception as e:
            log.error("Unexpected error during search: %s", e, exc_info=True)
            await emit(
                "Error: unexpected error during search.",
                done=True,
                hidden=False,
            )
            return f"Error: unexpected error during search. Details: {_truncate(str(e))}"

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
                result = site.api(
                    "parse", page=page.name, prop="text", redirects=True
                )
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
            *[
                asyncio.to_thread(_fetch_page, t, content_format)
                for t in titles
            ]
        )

        sections = []
        sources = []
        cap = self.valves.max_page_chars
        _fetch_errors = {
            "(Page not found — may have been deleted)",
            "(Content unavailable)",
        }
        for i, (title, content) in enumerate(pages, start=1):
            # Check for a fetch failure before truncating — a low max_page_chars
            # valve can truncate a sentinel string so it no longer matches
            # _fetch_errors, which would wrongly treat the failure as real content.
            is_fetch_error = content in _fetch_errors
            if len(content) > cap:
                content = (
                    content[:cap]
                    + f"\n...(truncated {len(content) - cap} chars)"
                )
            url = _build_page_url(title, article_path, origin)
            url_line = f"\nURL: {url}" if url else ""
            sections.append(
                f"=== Result {i}: {title} ===\nSource id:{to_source_id(title)}{url_line}\n\nPage content: {content}\n"
            )
            if not is_fetch_error:
                source_entry = {"name": title, "id": to_source_id(title)}
                metadata_entry = {"source": title}
                if url:
                    source_entry["url"] = url
                    metadata_entry["url"] = url
                sources.append(
                    {
                        "source": source_entry,
                        "document": [content],
                        "metadata": [metadata_entry],
                    }
                )

        if __event_emitter__:
            for src in sources:
                await __event_emitter__({"type": "source", "data": src})
        _store_turn_sources(__request__, sources)

        await emit(f"Found {len(pages)} result(s) for '{query}'.", done=True)
        return (
            f"Search results for '{query}' ({len(pages)} page(s)):\n\n"
            + "\n---\n\n".join(sections)
        )

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

        async def emit(
            message: str, done: bool = False, hidden: bool = True
        ) -> None:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": message,
                            "done": done,
                            "hidden": hidden,
                        },
                    }
                )

        # --- Validate configuration ---
        if not self.valves.wiki_url:
            await emit(
                "Error: MediaWiki URL is not configured in Tool Valves.",
                done=True,
                hidden=False,
            )
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
                verify_ssl=self.valves.verify_ssl,
                resolve_ip=self.valves.resolve_ip,
                user_agent=self.valves.user_agent,
                headers=self.valves.headers,
            )
        except ValueError as e:
            # Invalid resolve_ip / headers JSON / valve config
            await emit(f"Error: {e}", done=True, hidden=False)
            return f"Error: {e}"
        except mwclient.errors.LoginError as e:
            await emit(
                "Error: authentication failed. Check your username and password in Tool Valves.",
                done=True,
                hidden=False,
            )
            return (
                "Error: authentication failed. If using a BotPassword, the format is 'Username@BotName'. "
                f"Details: {_truncate(str(e))}"
            )
        except Exception as e:
            log.error("mwclient connection error", exc_info=True)
            await emit(
                "Error: could not connect to the wiki.",
                done=True,
                hidden=False,
            )
            return f"Error: could not connect to the wiki. Check the wiki_url in Tool Valves. Details: {_truncate(str(e))}"

        await emit(f"Saving page '{title}'…")

        # --- Save the page (blocking — run in thread) ---
        def _save():
            page = site.pages[title]
            page.save(content, summary=self.valves.edit_summary)

        try:
            await asyncio.to_thread(_save)
        except mwclient.errors.ProtectedPageError:
            await emit(
                f"Error: page '{title}' is protected and cannot be edited.",
                done=True,
                hidden=False,
            )
            return f"Error: page '{title}' is protected."
        except mwclient.errors.APIError as e:
            if e.code in ("writeapidenied", "permissiondenied"):
                await emit(
                    "Error: this wiki requires login to write.",
                    done=True,
                    hidden=False,
                )
                return "Error: this wiki requires authentication to write. Please configure username and password in Tool Valves."
            log.error("MediaWiki API error: %s", e.code)
            await emit(
                f"Error: wiki save failed ({e.code}).", done=True, hidden=False
            )
            return (
                f"Error: wiki API returned an error ({e.code}). Check page title and permissions. "
                f"Details: {_truncate(str(e))}"
            )
        except Exception as e:
            log.error("Unexpected error saving page: %s", e, exc_info=True)
            await emit(
                "Error: an unexpected error occurred while saving.",
                done=True,
                hidden=False,
            )
            return f"Error: an unexpected error occurred while saving. Details: {_truncate(str(e))}"

        # --- Build canonical page URL (blocking — run in thread) ---
        await emit("Fetching page URL…")

        article_path, origin = await asyncio.to_thread(_get_site_info, site)
        page_url = _build_page_url(title, article_path, origin)

        if page_url:
            await emit(f"Saved: {page_url}", done=True)
            return page_url

        await emit(
            f'Saved "{title}", but could not determine its URL.', done=True
        )
        return f'Saved "{title}" to the wiki, but the page URL could not be determined.'
