"""
title: Web Search Tool
author: WikiTeq
date: 2025-07-09
version: 1.0
license: MIT
description: Web search tool for OpenWebUI using the Tavily search API. Lets the AI search the web for up-to-date information when the user asks to look something up online.
requirements: tavily-python>=0.5.0, pydantic>=2.0.0
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Hard caps enforced by the Tavily API
MAX_RESULTS_LIMIT = 20
MAX_TIMEOUT_SECONDS = 120
DEFAULT_CONTENT_CAP = 4000


class Tools:
    """OpenWebUI tool exposing Tavily web search to chat models."""

    class Valves(BaseModel):
        tavily_api_key: str = Field(
            default="",
            description="Tavily API key (starts with 'tvly-'). Get one at https://tavily.com.",
        )
        max_results: int = Field(
            default=5,
            ge=1,
            le=MAX_RESULTS_LIMIT,
            description=(
                f"Maximum number of search results per query (1–{MAX_RESULTS_LIMIT}). "
                "Higher values are slower."
            ),
        )
        search_depth: Literal["basic", "advanced"] = Field(
            default="basic",
            description=(
                "Search depth. 'basic' is fast and cheap; 'advanced' is more thorough "
                "and returns richer content."
            ),
        )
        include_answer: bool = Field(
            default=True,
            description="Include Tavily's synthesized answer to the query in the output.",
        )
        topic: Literal["general", "news", "finance"] = Field(
            default="general",
            description="Search content category. 'news' for recent headlines, 'finance' for market data.",
        )
        timeout: int = Field(
            default=30,
            ge=1,
            le=MAX_TIMEOUT_SECONDS,
            description="HTTP request timeout in seconds (capped at 120 by Tavily).",
        )
        max_content_chars: int = Field(
            default=DEFAULT_CONTENT_CAP,
            ge=100,
            le=50_000,
            description="Maximum characters of content to include per result. Longer snippets are truncated.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def web_search(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Search the web for up-to-date information using the Tavily search API.

        Use this tool when the user asks to:
        - "search the web for ..." / "look up ... online"
        - "what's the latest on ..." / "find recent news about ..."
        - "google ..." / "search for ..."

        Returns a formatted block with each result's title, URL, and content
        snippet, plus (optionally) a synthesized answer to the query.

        Args:
            query: The search query string.

        Returns:
            Formatted search results string, or an error message.
        """
        # Lazy import: OpenWebUI loads this file before installing requirements,
        # so tavily is only available at call time.
        from tavily import AsyncTavilyClient
        from tavily.errors import (
            BadRequestError,
            InvalidAPIKeyError,
            UsageLimitExceededError,
        )
        from tavily.errors import TimeoutError as TavilyTimeoutError

        async def emit(message: str, done: bool = False, hidden: bool = True) -> None:
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
        if not self.valves.tavily_api_key.strip():
            await emit(
                "Error: Tavily API key is not configured in Tool Valves.",
                done=True,
                hidden=False,
            )
            return "Error: tavily_api_key is not configured."

        query = (query or "").strip()
        if not query:
            await emit("Error: search query cannot be empty.", done=True, hidden=False)
            return "Error: search query cannot be empty."

        await emit(f"Searching the web for '{query}'...")

        client = AsyncTavilyClient(api_key=self.valves.tavily_api_key.strip())

        try:
            response = await client.search(
                query=query,
                topic=self.valves.topic,
                search_depth=self.valves.search_depth,
                max_results=self.valves.max_results,
                include_answer=self.valves.include_answer,
                timeout=float(self.valves.timeout),
            )
        except InvalidAPIKeyError:
            await emit(
                "Error: Tavily API key is invalid.",
                done=True,
                hidden=False,
            )
            return "Error: Tavily API key is invalid. Check the tavily_api_key in Tool Valves."
        except UsageLimitExceededError:
            await emit(
                "Error: Tavily usage limit exceeded.",
                done=True,
                hidden=False,
            )
            return "Error: Tavily usage limit exceeded (rate limit or quota). Try again later."
        except BadRequestError as e:
            log.error("Tavily bad request: %s", e)
            await emit(
                f"Error: invalid search parameters ({e}).",
                done=True,
                hidden=False,
            )
            return (
                f"Error: Tavily rejected the request ({e}). Check the query and valves."
            )
        except TavilyTimeoutError:
            await emit(
                f"Error: search timed out after {self.valves.timeout}s.",
                done=True,
                hidden=False,
            )
            return (
                f"Error: Tavily search timed out after {self.valves.timeout} seconds."
            )
        except Exception:
            log.error("Unexpected error during Tavily search", exc_info=True)
            await emit(
                "Error: unexpected error during web search.",
                done=True,
                hidden=False,
            )
            return "Error: unexpected error during web search. Check the server logs for details."
        finally:
            # Always release the underlying HTTP connection pool.
            # AsyncTavilyClient exposes close() (not aclose()); the bare except
            # previously swallowed an AttributeError here, leaking connections.
            try:
                await client.close()
            except Exception:
                log.debug("Ignoring error closing Tavily client", exc_info=True)

        results = response.get("results", []) or []
        if not results:
            await emit("No results found.", done=True, hidden=False)
            return f"No web results found for '{query}'."

        cap = self.valves.max_content_chars
        sections = []
        for i, item in enumerate(results, start=1):
            title = item.get("title", "(untitled)") or "(untitled)"
            url = item.get("url", "") or ""
            content = item.get("content", "") or ""

            # Emit a source/citation event so OpenWebUI renders the page
            # as a clickable reference under the response. Short name
            # "source" is required for DB persistence (not "citation").
            if url and __event_emitter__:
                # Document passage: use the (possibly pre-truncation) snippet,
                # capped so the citation stays readable in the UI.
                doc_passage = content if len(content) <= cap else content[:cap]
                try:
                    await __event_emitter__(
                        {
                            "type": "source",
                            "data": {
                                "source": {"name": title, "id": url},
                                "document": [doc_passage],
                                "metadata": [
                                    {"source": url, "name": title, "url": url}
                                ],
                            },
                        }
                    )
                except Exception:
                    log.debug("Failed to emit source event", exc_info=True)

            if len(content) > cap:
                content = content[:cap] + f"\n...(truncated {len(content) - cap} chars)"
            block = f"=== Result {i}: {title} ===\nURL: {url}\n\n{content}"
            sections.append(block)

        parts = [f"Search results for '{query}' ({len(results)} result(s)):\n"]

        answer = response.get("answer")
        if self.valves.include_answer and answer:
            parts.append(f"Answer: {answer}\n")

        parts.append("\n---\n\n".join(sections))

        await emit(f"Found {len(results)} result(s) for '{query}'.", done=True)
        return "\n".join(parts)
