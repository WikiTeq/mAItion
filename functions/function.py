"""
title: rag-of-all-trades function
author: WikiTeq
date: 2027-01-27
version: 2.3
license: MIT
description: A function that calls the RAG service to retrieve context for chat queries with OWUI sources integration
requirements: requests
"""

import os
import re
import requests
import logging
from typing import List, Optional, Callable, Awaitable
from pydantic import BaseModel

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# Read configuration from environment variables
DEFAULT_ENABLED = os.getenv("ENABLE_CUSTOM_RAG_SERVICE", "true").lower() == "true"
DEFAULT_RAG_URL = os.getenv("CUSTOM_RAG_SERVICE_URL", "")
DEFAULT_API_KEY = os.getenv("CUSTOM_RAG_SERVICE_API_KEY", "")
DEFAULT_TIMEOUT = int(os.getenv("CUSTOM_RAG_SERVICE_TIMEOUT", "30"))


class Filter:
    class Valves(BaseModel):
        """Configuration valves for the filter"""

        pipelines: List[str] = ["*"] # Apply to all pipelines
        priority: int = 0

        # Custom RAG Service Configuration (defaults from environment variables)
        enabled: bool = DEFAULT_ENABLED
        rag_service_url: str = DEFAULT_RAG_URL
        rag_service_api_key: str = DEFAULT_API_KEY
        rag_service_timeout: int = DEFAULT_TIMEOUT
        top_k: int = 5

         # Context injection settings
        inject_context: bool = True
        context_template: str = """Based on the following retrieved context, please answer the user's question.

### Retrieved Context:

{context}

### User Question: {query}

Please provide a comprehensive answer based on the context above. If the context doesn't contain relevant information, say so."""

    def __init__(self):
        self.type = "filter"
        self.name = "Custom RAG Filter"
        self.valves = self.Valves()

    async def on_startup(self):
        log.info(f"Pipeline loaded")
        log.info(f"Enabled: {self.valves.enabled}")
        log.info(f"URL: {self.valves.rag_service_url}")

    async def on_shutdown(self):
        log.info("Pipeline unloaded")

    def call_rag_service(self, query: str) -> dict:
        """
        Call the custom RAG service API

        Args:
            query: The search query string

        Returns:
            dict: The API response with references and raw text
        """
        try:
            log.info(f"Calling RAG service for query: {query[:50]}...")

            payload = {
                "query": query,
                "top_k": self.valves.top_k,
                "metadata_filters": {},
            }

            headers = {"Content-Type": "application/json"}

            # Add API key if configured
            if self.valves.rag_service_api_key:
                headers["Authorization"] = f"Bearer {self.valves.rag_service_api_key}"

            log.info(f"Sending request to: {self.valves.rag_service_url}")

            response = requests.post(
                self.valves.rag_service_url,
                json=payload,
                headers=headers,
                timeout=self.valves.rag_service_timeout,
            )
            response.raise_for_status()

            result = response.json()
            return result

        except Exception as e:
            log.error(f"Error calling RAG service: {e}")
            return {"references": [], "raw": []}

    def parse_raw_chunk(self, raw_text: str) -> dict:
        """
        Parse a raw chunk string to extract score and text.

        Expected format: "Score: 0.6172 | Text: actual content here..."

        Args:
            raw_text: The raw chunk string from the API

        Returns:
            dict: Parsed chunk with 'score' and 'text' keys
        """
        try:
            # Try to parse "Score: X.XX | Text: content" format
            match = re.match(r"Score:\s*([\d.]+)\s*\|\s*Text:\s*(.*)", raw_text, re.DOTALL)
            if match:
                return {
                    "score": float(match.group(1)),
                    "text": match.group(2).strip()
                }
            # If format doesn't match, return the whole text
            return {"score": 0.0, "text": raw_text.strip()}

        except Exception as e:
            log.warning(f"Error parsing raw chunk: {e}")
            return {"score": 0.0, "text": raw_text.strip()}

    def get_filename_from_extras(self, extras: dict) -> str:
        return (
            extras.get("key")
            or extras.get("filename")
            or extras.get("name")
            or None
        )

    def format_context_and_sources(self, rag_result: dict, query: str) -> tuple:
        references = rag_result.get("references", [])
        raw_chunks = rag_result.get("raw", [])

        if not references and not raw_chunks:
            return "", []

        # Build context from raw chunks (contains the actual text)
        context_parts = []
        sources = []

        num_items = max(len(references), len(raw_chunks))

        # Get corresponding reference metadata if available
        for i in range(num_items):
            ref = references[i] if i < len(references) else {}
            extras = ref.get("extras", {})
            score = ref.get("score", 0.0)

            text = ref.get("text", "")
            if not text and i < len(raw_chunks):
                parsed = self.parse_raw_chunk(raw_chunks[i])
                text = parsed["text"]
                if score == 0.0:
                    score = parsed["score"]

            if not text:
                continue

            filename = self.get_filename_from_extras(extras)

            # Extract source information from different possible locations
            source_name = (
                ref.get("title")
                or ref.get("source_name")
                or filename
                or f"Source {i+1}"
            )

            # Build context part with actual text from raw chunks
            context_parts.append(f"[Source: {source_name}]\n{text}\n")

            source_obj = {
                "source": {"name": source_name},
                "document": [text[:1000] if len(text) > 1000 else text],
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
            }

            url = ref.get("url") or extras.get("url")
            if url:
                source_obj["source"]["url"] = url

            sources.append(source_obj)

        context = "\n".join(context_parts)
        if not context:
            return "", []

        # Use template to format final context
        formatted_context = self.valves.context_template.format(
            context=context,
            query=query
        )
        log.info(f"Formatted context with {len(sources)} sources, length: {len(formatted_context)} chars")

        return formatted_context, sources

    async def inlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> dict:
        """
        Inlet filter: Process the request before it goes to the LLM
        This is where we inject RAG context into the messages and emit sources.

        Args:
            body: The request body containing messages
            __user__: The user information (injected by OWUI)
            __event_emitter__: Event emitter function for sending sources to UI (injected by OWUI)
        log.info("Inlet filter triggered")

        Returns:
            dict: Modified request body with RAG context injected
        """

        log.info("Inlet filter triggered")

        # Check if pipeline is enabled
        if not self.valves.enabled:
            log.info("Pipeline is disabled, skipping")
            return body

        # Check if RAG service URL is configured
        if not self.valves.rag_service_url:
            log.warning("RAG service URL not configured, skipping")
            return body

        # Extract the last user message as the query
        messages = body.get("messages", [])
        if not messages:
            return body

        # Find the last user message
        last_user_message = None
        last_user_index = -1

        for i, msg in enumerate(reversed(messages)):
            if msg.get("role") == "user":
                last_user_message = msg
                last_user_index = len(messages) - 1 - i
                break

        if not last_user_message:
            return body

        query = last_user_message.get("content", "")
        if not query.strip():
            return body

        # Call custom RAG service
        try:
            rag_result = self.call_rag_service(query)

            # Format context and get sources
            if self.valves.inject_context:
                context, sources = self.format_context_and_sources(rag_result, query)

                if context:
                    log.info(f"Injecting context into messages")

                    # Inject context as a system message
                    context_msg = {"role": "system", "content": context}

                    # Insert context message before the last user message
                    messages.insert(last_user_index, context_msg)
                    body["messages"] = messages

                    if __event_emitter__:
                        for src in sources:
                            await __event_emitter__({"type": "source", "data": src})

        except Exception as e:
            log.error(f"inlet error: {e}")

        return body

    async def outlet(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __event_emitter__: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> dict:
        """
        Outlet filter: Process the response after the LLM generates it

        Args:
            body: The response body from the LLM
            __user__: The user information
            __event_emitter__: Event emitter function

        Returns:
            dict: Response body (unmodified)
        """
        return body