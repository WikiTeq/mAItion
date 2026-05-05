"""
title: Knowledge Base Search
author: WikiTeq
date: 2025-05-01
version: 1.0
license: MIT
description: Searches the RAG-of-All-Trades knowledge base and returns relevant context for the user's query.
requirements: requests
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

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

        metadata_fields = {k: v for k, v in extras.items() if k not in _internal_fields}
        metadata_fields["url"] = ref.get("url") or extras.get("url")
        metadata_md = "\n".join(f"- *{k}*: {v}" for k, v in metadata_fields.items() if v is not None)
        metadata_section = f"## Metadata\n\n{metadata_md}" if metadata_md else ""

        context_parts.append(f"[Source: {source_name}]\n\n{metadata_section}\n\n{text}\n")

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

    return "\n".join(context_parts), sources


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

    def __init__(self):
        self.valves = self.Valves()

    async def search_knowledge_base(
        self,
        query: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Use this tool to answer questions about this system, its data, processes,
        or any domain-specific topics. Prefer retrieved knowledge over general
        training knowledge when the topic may be covered in the knowledge base.

        Args:
            query: A concise search query derived from the user's question

        Returns:
            Retrieved document chunks and their metadata from the knowledge base, or a message indicating nothing was found.
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
        return context
