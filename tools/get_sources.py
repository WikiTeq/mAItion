"""
title: Get Current Sources
author: WikiTeq
date: 2025-07-10
version: 2.0
license: MIT
description: Retrieves sources/citations emitted by tools in the current chat turn. Useful when the model needs to reference sources for inline citations.
requirements: pydantic>=2.0.0
"""

import hashlib
import logging

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class Tools:
    """OpenWebUI tool that collects sources emitted during the current chat turn."""

    class Valves(BaseModel):
        max_sources: int = Field(
            default=50,
            ge=1,
            le=500,
            description="Maximum number of sources to return (1-500).",
        )
        max_excerpt_chars: int = Field(
            default=200,
            ge=1,
            le=500,
            description="Maximum characters of each source's excerpt to include (1-500).",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def get_sources(
        self,
        __event_emitter__=None,
        __request__=None,
    ) -> str:
        """
        Retrieve all sources/citations emitted by tools during the current chat turn.

        Sources are collected from __request__.state, where source-emitting tools
        (web_search, wiki_search, retrieval, etc.) store them as they run.
        Only sources from the current turn are returned - previous turns are not
        included since they are already visible in the chat history.

        Use this tool when you need to:
        - Review available sources for inline citations
        - List references found so far in this response
        - Get source IDs/names for citation formatting

        Args: none

        Returns:
            A formatted list of current-turn sources, or a message if none found.
        """

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

        await emit("Collecting sources from current turn...")

        # Current-turn sources are stored on __request__.state by
        # source-emitting tools (web_search, wiki_search, retrieval).
        # They share the same Request object within a single turn.
        current_sources = []
        if __request__:
            try:
                current_sources = getattr(__request__.state, "_wikiteq_sources", [])
            except Exception:
                log.debug("No request state sources found", exc_info=True)

        if not current_sources:
            await emit("No sources found in current turn.", done=True, hidden=False)
            return "No sources have been emitted in this turn so far."

        # Deduplicate by (name, url), preserving insertion order.
        all_sources = []
        seen = set()
        for source in current_sources:
            source = _normalize_source(source)
            if source is None:
                continue
            name = source.get("source", {}).get("name", "") or source.get("name", "")
            url = _extract_url(source)
            key = (name, url) if url else (name, _content_fingerprint(source))
            if key in seen:
                continue
            seen.add(key)
            all_sources.append(source)

        if not all_sources:
            await emit("No sources found in current turn.", done=True, hidden=False)
            return "No sources have been emitted in this turn so far."

        # Apply limit (keep most recent if over cap)
        if len(all_sources) > self.valves.max_sources:
            all_sources = all_sources[-self.valves.max_sources :]

        # Format output. Sources were already normalized above (source.source
        # is a dict, document[0]/url are strings if present), so no further
        # isinstance guarding is needed here.
        sections = []
        for i, source in enumerate(all_sources, start=1):
            name = source.get("source", {}).get("name", "") or source.get(
                "name", "(unnamed)"
            )
            source_id = source.get("source", {}).get("id", "")
            url = _extract_url(source)
            documents = source.get("document", [])

            lines = [f"=== Source {i}: {name} ==="]
            # source_id may be a real URL (web_search.py) or a to_source_id()
            # slug (roat_retrieval.py/mediawiki_tool.py) — either way it's
            # useful for citation formatting, so always surface it distinctly
            # from the URL line below rather than dropping it when the two
            # happen to match.
            if source_id:
                lines.append(f"Source id: {source_id}")
            if url:
                lines.append(f"URL: {url}")
            if documents:
                doc_text = documents[0]
                cap = self.valves.max_excerpt_chars
                if len(doc_text) > cap:
                    doc_text = doc_text[:cap] + "..."
                lines.append(f"Excerpt: {doc_text}")
            sections.append("\n".join(lines))

        await emit(f"Found {len(all_sources)} source(s).", done=True)
        return (
            f"Sources from current turn ({len(all_sources)} total):\n\n"
            + "\n---\n\n".join(sections)
        )


def _normalize_source(source) -> dict | None:
    """Validate a raw entry from _wikiteq_sources against the shared source
    schema, returning it unchanged or None if malformed.

    Every source-emitting tool (web_search, roat_retrieval, mediawiki_tool)
    is expected to produce the same shape: {"source": {"name", "id", "url"?},
    "document": [str, ...], "metadata": [...]}. A malformed entry (source.source
    not a dict, document not a non-empty list of strings) would otherwise raise
    downstream and abort processing of every other, valid source in the same
    turn — so it's rejected here instead of being coerced or partially salvaged.
    """
    if not isinstance(source, dict):
        return None

    inner = source.get("source")
    if not isinstance(inner, dict):
        return None

    # name feeds the dedup key directly (see get_sources()); a non-string,
    # truthy name (e.g. a stray dict/list) would make that key unhashable
    # and crash the whole turn's dedup pass, not just this one malformed
    # entry. A missing/empty name is fine — it falls back to "(unnamed)".
    for name_source in (inner, source):
        name = name_source.get("name")
        if name and not isinstance(name, str):
            return None

    documents = source.get("document")
    if (
        not isinstance(documents, list)
        or not documents
        or not isinstance(documents[0], str)
    ):
        return None

    return source


def _extract_url(source: dict) -> str:
    """Best-effort URL extraction from a source dict.

    Tries real URL fields first — top-level "url", then source.url — before
    falling back to source.id, since source.id is not trustworthy as a URL
    for every emitter: web_search.py populates it with a real URL, but
    roat_retrieval.py/mediawiki_tool.py populate it with a to_source_id()
    slug (e.g. "some-kb-doc"), which is not a URL. Only accept source.id
    when it actually looks like one, and only once url/source.url are absent.

    metadata[0]["source"] is deliberately excluded: in roat_retrieval it
    holds the document title, not a URL, so treating it as one would
    mislabel a title as a link.

    Assumes source has already passed through _normalize_source (source.source
    is a dict); top-level "url" is not normalized, so it's coerced here.
    """
    url = source.get("url")
    if isinstance(url, str) and url:
        return url

    url = source.get("source", {}).get("url")
    if isinstance(url, str) and url:
        return url

    source_id = source.get("source", {}).get("id", "")
    if isinstance(source_id, str) and source_id.startswith(("http://", "https://")):
        return source_id

    return ""


def _content_fingerprint(source: dict) -> str:
    """Fallback dedup key for sources with no URL.

    Without this, two distinct chunks of the same document (e.g. two
    non-adjacent ROAT KB passages with the same title, no url extra) would
    dedup-collide on (name, "") and silently drop one from the output.
    Hashes the full document text (not a truncated prefix, and not the raw
    text itself as the dict key) so two sources that happen to share a
    common prefix but diverge later still get distinct keys. Assumes source
    has already passed through _normalize_source.
    """
    documents = source.get("document", [])
    if not documents:
        return ""
    return hashlib.sha256(documents[0].encode("utf-8")).hexdigest()
