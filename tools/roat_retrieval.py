"""
title: Knowledge Base Search
author: WikiTeq
date: 2025-05-01
version: 1.0
license: MIT
description: Searches the RAG-of-All-Trades knowledge base and returns relevant context for the user's query.
requirements: requests, pyyaml
"""

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import Awaitable, Callable

import requests
import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

_CLOSING_DOCUMENT_TAG_RE = re.compile(r"</document\s*>", re.IGNORECASE)
_OPENING_DOCUMENT_TAG_RE = re.compile(r"<document\b", re.IGNORECASE)


def _escape_document_tags(text: str) -> str:
    text = _CLOSING_DOCUMENT_TAG_RE.sub(lambda m: "<\\/document>", text)
    text = _OPENING_DOCUMENT_TAG_RE.sub(lambda m: "<\\document", text)
    return text


MAX_ERROR_DETAIL_CHARS = 500


def _truncate(text: str, limit: int = MAX_ERROR_DETAIL_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    suffix = "... (truncated)"
    return text[: limit - len(suffix)] + suffix


def _extract_http_error_detail(err: requests.HTTPError) -> str:
    resp = err.response
    if resp is None:
        return _truncate(str(err))
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
        return _truncate(str(detail))
    except ValueError:
        return _truncate(resp.text)


def to_source_id(text: str) -> str:
    """Slugify a source name into an id: spaces -> hyphens, strip non-alnum/hyphen, lowercase,
    with a short digest of the original text appended for uniqueness.

    The digest is always appended, not just when the slug is empty: stripping
    punctuation means distinct titles like "C++", "C#", and "C" would otherwise
    all slugify to the same "c" and collide. Titles made up entirely of
    non-ASCII characters (e.g. "日本語") strip down to an empty slug, in which
    case the id falls back to the digest alone.

    Duplicated verbatim in mediawiki_tool.py — OWUI loads each tool's source
    as an independent module (no shared import path between tools), so keep
    both copies in sync if this changes.
    """
    text_with_hyphens = text.replace(" ", "-")
    slug = re.sub(r"[^a-zA-Z0-9-]", "", text_with_hyphens).lower()
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    if slug:
        return f"{slug}-{digest}"
    return f"src-{digest}"


def _parse_raw_chunk(raw_text: str) -> dict:
    match = re.match(r"Score:\s*([\d.]+)\s*\|\s*Text:\s*(.*)", raw_text, re.DOTALL)
    if match:
        return {"score": float(match.group(1)), "text": match.group(2).strip()}
    return {"score": 0.0, "text": raw_text.strip()}


def _get_filename_from_extras(extras: dict) -> str | None:
    return extras.get("key") or extras.get("filename") or extras.get("name") or None


def _find_video_url(
    references: list, field: str = "video_url"
) -> tuple[str | None, str | None]:
    """Return (video_url, source_name) from the highest-scored ref with extras.<field>."""

    def score_key(ref):
        try:
            return float(ref.get("score") or 0)
        except (TypeError, ValueError):
            return 0.0

    for ref in sorted(references, key=score_key, reverse=True):
        extras = ref.get("extras") or {}
        if not isinstance(extras, dict):
            continue
        url = extras.get(field, "")
        if url and isinstance(url, str) and url.startswith("https://"):
            return url, ref.get("title") or ref.get("source_name") or "Source"
    return None, None


def _store_turn_sources(request, sources: list) -> None:
    """Append sources to __request__.state._wikiteq_sources for get_sources tool.

    Duplicated verbatim in mediawiki_tool.py — see to_source_id() above for why.
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


def _call_rag_service(
    url: str,
    api_key: str,
    timeout: int,
    top_k: int,
    query: str,
    metadata_filters: list[dict] | None = None,
) -> dict:
    payload = {"query": query, "top_k": top_k}
    if metadata_filters:
        payload["metadata_filters"] = metadata_filters
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = url.strip()
    log.info(
        "Calling ROAT: query_length=%d has_filters=%s",
        len(query),
        bool(metadata_filters),
    )
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _format_context_and_sources(
    rag_result: dict, max_document_preview_chars: int = 0
) -> tuple[str, list]:
    references = rag_result.get("references", []) or []
    raw_chunks = rag_result.get("raw") or []

    if not references and not raw_chunks:
        return "", []

    context_parts = []
    sources = []
    _internal_fields = {"key", "format", "version", "checksum"}

    for i in range(max(len(references), len(raw_chunks))):
        ref = references[i] if i < len(references) else {}
        extras = ref.get("extras") or {}
        score = ref.get("score", 0.0)

        text = ref.get("text", "")
        if not text and i < len(raw_chunks):
            parsed = _parse_raw_chunk(raw_chunks[i])
            text = parsed["text"]
            if score == 0.0:
                score = parsed["score"]

        if not text:
            continue

        filename = _get_filename_from_extras(extras)
        source_name = (
            ref.get("title") or ref.get("source_name") or filename or f"Source {i + 1}"
        )

        metadata_fields = {"title": source_name}
        metadata_fields.update(
            {
                k: v
                for k, v in extras.items()
                if k not in _internal_fields
                and k not in ("url", "id")
                and v is not None
            }
        )
        url = ref.get("url") or extras.get("url")
        if url:
            metadata_fields["url"] = url
        frontmatter_body = yaml.safe_dump(
            metadata_fields,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ).strip()
        frontmatter = f"---\n{frontmatter_body}\n---"
        safe_text = _escape_document_tags(text)
        context_parts.append(
            f'<document index="{i + 1}" score="{score:.2f}" format="markdown+frontmatter">\n'
            f"{frontmatter}\n\n{safe_text}\n"
            f"</document>"
        )

        source_obj = {
            "source": {"name": source_name, "id": to_source_id(source_name)},
            "document": [
                (
                    text[:max_document_preview_chars]
                    if max_document_preview_chars > 0
                    else text
                )
            ],
            "metadata": [
                {
                    "source": source_name,
                    "file": filename,
                    "relevance_score": score,
                    "type": extras.get("format", "document"),
                    "storage": extras.get("source"),
                    "key": extras.get("key"),
                    "checksum": extras.get("checksum"),
                    "version": extras.get("version"),
                    "format": extras.get("format"),
                }
            ],
            "distances": [score],
        }
        url = ref.get("url") or extras.get("url")
        if url:
            source_obj["source"]["url"] = url
            source_obj["metadata"][0]["url"] = url

        sources.append(source_obj)

    return "\n\n".join(context_parts), sources


class Tools:
    class Valves(BaseModel):
        rag_service_url: str = Field(
            default="",
            description="Full URL to the ROAT query endpoint, e.g. http://api:8000/query.",
        )
        rag_service_api_key: str = Field(
            default="",
            description="Bearer token for the ROAT API (leave blank if not required).",
        )
        rag_service_timeout: int = Field(
            default=30,
            description="Request timeout in seconds.",
        )
        top_k: int = Field(
            default=20,
            description="Number of top results to retrieve from the knowledge base.",
        )
        max_document_preview_chars: int = Field(
            default=0,
            description="Maximum characters for the document preview in source citations (0 = unlimited).",
        )
        video_metadata_field: str = Field(
            default="video_url",
            description="Metadata field name in retrieved sources that contains the video URL.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def search_knowledge_base(
        self,
        query: str,
        metadata_filters: list[dict] = [],
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
        __request__=None,
    ) -> str:
        """
        Search the organizational knowledge base to answer questions about company
        data, internal processes, documentation, or domain-specific topics that may
        not be in the model's training data.

        ALWAYS call this tool when the user asks about:
        - Internal documents, wikis, or knowledge articles
        - Company processes, policies, or procedures
        - Project-specific data, tickets, or reports
        - Topics specific to this organization that general training data would not cover

        Do NOT call this tool for general knowledge questions (math, programming
        syntax, public facts) that do not require internal documents.

        Args:
            query: A concise, keyword-rich search query for the free-text/semantic part
                   of the question only. Do NOT stuff field-specific values (status,
                   project, assignee, dates, tags, etc.) into this string — put those
                   in metadata_filters instead, even if that means query ends up short
                   or generic. Use specific nouns and avoid filler words.
            metadata_filters: Optional list of metadata filters to narrow the search.
                   Use this proactively, without being asked: results include a
                   "## Metadata" section per source listing the fields available on
                   that source (e.g. project, assignee, tags, status, last_modified).
                   If an earlier search in this conversation returned sources with a
                   metadata field relevant to the user's current question, filter on
                   it directly — do not wait for the user to name the field or ask
                   for "a metadata filter" explicitly, and do not fall back to typing
                   the value into query instead.
                   Example: after a search returns tickets with "## Metadata" showing
                   "status: To Do" / "status: Done", if the user then asks "which are
                   done?" or "what's still in progress?", call this tool again with
                   query="tickets" and
                   metadata_filters=[{"name": "status", "operator": "EQ", "value": "Done"}]
                   — do NOT call it with query="tickets status:Done".
                   Only use field names you have seen in a "## Metadata" section or
                   that the user has stated; never invent a field name you haven't
                   observed. Each filter is a dict with "name", "operator", and
                   "value".

                   Scalar operators — field holds a single value, "value" is a
                   single value: EQ, NE, GT, GTE, LT, LTE, TEXT_MATCH.

                   IN, NIN — "value" is a list, but this still checks the field's
                   own value as a single unit against that list (field IN
                   [A, B, C]), e.g. status IN ["To Do", "Done"] matches a status
                   field equal to either. Do NOT use IN/NIN on a list-valued field
                   (e.g. "labels", "tags" shown as a Python list in "## Metadata")
                   — the field's list gets compared as a whole (e.g. the text
                   "[\"daycare_hardware\"]"), never against one of its individual
                   elements, so IN will silently return nothing and NIN will
                   silently return everything, even when the field clearly
                   contains the value you're checking for.

                   ANY, ALL — use these when the metadata field itself holds a
                   list of values (e.g. "labels": ["daycare_hardware", "urgent"])
                   and you want to check containment: ANY matches if the field
                   contains at least one of the given values, ALL matches only if
                   it contains every given value.

                   Example:
                   [{"name": "project", "operator": "EQ", "value": "MAIT"},
                    {"name": "status", "operator": "IN", "value": ["To Do", "Done"]},
                    {"name": "labels", "operator": "ANY", "value": ["urgent"]}]

        Returns:
            Retrieved document chunks with source metadata, or a message indicating
            nothing relevant was found. Response uses the following format to present
            multiple documents found:

            <document index="1" score="0.63" format="markdown+frontmatter">
            ---
            title: FileTitle.txt
            source: source-data-connector-name
            last_modified: '2026-06-17 13:43:31.051046'
            ---

            Content of the document or document chunk
            </document>

            <document index="2" score="0.55" format="markdown+frontmatter">
            ---
            title: AnotherFileTitle.txt
            source: source-data-connector-name
            last_modified: '2026-06-18 15:23:11.071035'
            ---

            Content of another document or document chunk
            </document>

            ...
        """

        async def emit(description: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {"description": description, "done": done},
                    }
                )

        if not self.valves.rag_service_url:
            await emit(
                "Knowledge base URL is not configured in Tool Valves.", done=True
            )
            return "Error: rag_service_url is not configured in Tool Valves."

        await emit("Searching knowledge base…")

        try:
            log.info(
                "ROAT url=%r top_k=%r timeout=%r has_filters=%s",
                self.valves.rag_service_url,
                self.valves.top_k,
                self.valves.rag_service_timeout,
                bool(metadata_filters),
            )
            rag_result = await asyncio.to_thread(
                _call_rag_service,
                self.valves.rag_service_url,
                self.valves.rag_service_api_key,
                self.valves.rag_service_timeout,
                self.valves.top_k,
                query,
                metadata_filters,
            )
        except requests.HTTPError as e:
            detail = _extract_http_error_detail(e)
            log.error("ROAT request failed: %s", e, exc_info=True)
            await emit("The knowledge base rejected this request.", done=True)
            status_code = (
                e.response.status_code if e.response is not None else "unknown"
            )
            return f"Error: the knowledge base rejected this request ({status_code}). {detail}"
        except Exception as e:
            log.error("ROAT request failed: %s", e, exc_info=True)
            await emit("Failed to reach the knowledge base.", done=True)
            return f"Error: could not reach the knowledge base. {_truncate(str(e))}"

        context, sources = _format_context_and_sources(
            rag_result, self.valves.max_document_preview_chars
        )

        if not context:
            await emit("No relevant information found.", done=True)
            return "No relevant information was found in the knowledge base for this query."

        if __event_emitter__:
            for src in sources:
                await __event_emitter__({"type": "source", "data": src})
        _store_turn_sources(__request__, sources)

        await emit(f"Found {len(sources)} relevant source(s).", done=True)
        log.info(
            "Returning context with %d sources (%d chars)", len(sources), len(context)
        )

        if os.environ.get("FUNCTION_VIDEO_INJECT_ENABLED", "") == "True":
            references = rag_result.get("references", []) or []
            video_url, _ = _find_video_url(
                references, field=self.valves.video_metadata_field
            )
            if video_url:
                log.info("Embedding video marker for %s", video_url[:80])
                return f"<!--VIDEO:{video_url}-->{context}"
        return context
