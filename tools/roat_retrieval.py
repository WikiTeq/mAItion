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
import logging
import os
import re
from collections.abc import Awaitable, Callable

import yaml
import requests
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


def _parse_raw_chunk(raw_text: str) -> dict:
    match = re.match(r"Score:\s*([\d.]+)\s*\|\s*Text:\s*(.*)", raw_text, re.DOTALL)
    if match:
        return {"score": float(match.group(1)), "text": match.group(2).strip()}
    return {"score": 0.0, "text": raw_text.strip()}


def _get_filename_from_extras(extras: dict) -> str | None:
    return extras.get("key") or extras.get("filename") or extras.get("name") or None


def _find_video_url(references: list, field: str = "video_url") -> tuple[str | None, str | None]:
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


def _call_rag_service(url: str, api_key: str, timeout: int, top_k: int, query: str) -> dict:
    payload = {"query": query, "top_k": top_k, "metadata_filters": {}}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = url.strip()
    log.info("Calling ROAT: query_length=%d", len(query))
    response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _format_context_and_sources(rag_result: dict, max_document_preview_chars: int = 0) -> tuple[str, list]:
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
        source_name = ref.get("title") or ref.get("source_name") or filename or f"Source {i + 1}"

        metadata_fields = {"title": source_name}
        metadata_fields.update(
            {
                k: v
                for k, v in extras.items()
                if k not in _internal_fields and k != "url" and v is not None
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
        context_parts.append(
            f'<document index="{i + 1}" score="{score:.2f}" format="markdown+frontmatter">\n'
            f"{frontmatter}\n\n<content>\n{text}\n</content>\n"
            f"</document>"
        )

        source_obj = {
            "source": {"name": source_name},
            "document": [text[:max_document_preview_chars] if max_document_preview_chars > 0 else text],
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
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
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
            query: A concise, keyword-rich search query derived from the user's question.
                   Use specific nouns and avoid filler words.

        Returns:
            Retrieved document chunks with source metadata, or a message indicating
            nothing relevant was found.
        """

        async def emit(description: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": description, "done": done}})

        if not self.valves.rag_service_url:
            await emit("Knowledge base URL is not configured in Tool Valves.", done=True)
            return "Error: rag_service_url is not configured in Tool Valves."

        await emit("Searching knowledge base…")

        try:
            log.info(
                "ROAT url=%r top_k=%r timeout=%r",
                self.valves.rag_service_url,
                self.valves.top_k,
                self.valves.rag_service_timeout,
            )
            rag_result = await asyncio.to_thread(
                _call_rag_service,
                self.valves.rag_service_url,
                self.valves.rag_service_api_key,
                self.valves.rag_service_timeout,
                self.valves.top_k,
                query,
            )
        except Exception as e:
            log.error("ROAT request failed: %s", e, exc_info=True)
            await emit("Failed to reach the knowledge base.", done=True)
            return "Error: could not reach the knowledge base. Check the server logs for details."

        context, sources = _format_context_and_sources(rag_result, self.valves.max_document_preview_chars)

        if not context:
            await emit("No relevant information found.", done=True)
            return "No relevant information was found in the knowledge base for this query."

        if __event_emitter__:
            for src in sources:
                await __event_emitter__({"type": "source", "data": src})

        await emit(f"Found {len(sources)} relevant source(s).", done=True)
        log.info("Returning context with %d sources (%d chars)", len(sources), len(context))

        if os.environ.get("FUNCTION_VIDEO_INJECT_ENABLED", "") == "True":
            references = rag_result.get("references", []) or []
            video_url, _ = _find_video_url(references, field=self.valves.video_metadata_field)
            if video_url:
                log.info("Embedding video marker for %s", video_url[:80])
                return f"<!--VIDEO:{video_url}-->{context}"
        return context
