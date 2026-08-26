"""
title: Image Resizer
author: EaV Solution, WikiTeq
version: 0.2
description: Downscales oversized images in chat messages before they reach the model.
"""

import base64
import io
import logging

from PIL import Image
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Reject implausibly large encoded payloads before decoding, so a malicious
# or oversized attachment can't force a large in-memory decode.
MAX_ENCODED_BYTES = 25 * 1024 * 1024

# Reject decoded images with an implausible pixel count, independent of how
# small the encoded payload is (guards against decompression-bomb style
# inputs where a tiny file expands into a huge pixel grid).
MAX_PIXELS = 64_000_000  # e.g. an 8000x8000 image


def resize_images_in_messages(messages, max_dimension=768):
    # No cross-turn cache: OpenWebUI resends a fresh copy of history each
    # request, so every historical image is still base64-decoded and
    # re-opened every turn. Only the encode+save step is skipped once an
    # image is already at/under max_dimension.
    for message in messages:
        for item in message.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image_url":
                continue

            image_url = item.get("image_url")
            if not isinstance(image_url, dict):
                continue
            image_data_url = image_url.get("url", "")
            if not image_data_url.startswith("data:image/"):
                continue

            try:
                _, encoded = image_data_url.split(",", 1)
                if len(encoded) > MAX_ENCODED_BYTES:
                    raise ValueError(f"encoded payload too large ({len(encoded)} bytes)")

                image_data = base64.b64decode(encoded)
                image = Image.open(io.BytesIO(image_data))
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise ValueError(
                        f"decoded image too large ({width}x{height} pixels)"
                    )
                # Trust Pillow's own detection of the decoded bytes over the
                # declared mime type. If Pillow can't identify the format,
                # treat it as corrupted and bail out via the except below.
                if image.format is None:
                    raise ValueError("could not determine image format")
                image_format = image.format

                # Resizing only touches the first frame. Leave animated
                # GIF/APNG/WebP images untouched rather than silently
                # collapsing them to a static image.
                if getattr(image, "is_animated", False):
                    continue

                if max(width, height) > max_dimension:
                    scaling_factor = max_dimension / max(width, height)
                    new_width = max(1, int(width * scaling_factor))
                    new_height = max(1, int(height * scaling_factor))
                    image = image.resize((new_width, new_height), Image.LANCZOS)

                    buffered = io.BytesIO()
                    image.save(buffered, format=image_format)
                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
                    out_mime = Image.MIME.get(image_format, f"image/{image_format.lower()}")
                    new_data_url = f"data:{out_mime};base64,{img_str}"
                    image_url["url"] = new_data_url
            except Exception as e:
                log.warning(
                    "image_resizer: failed to process image (mime=%s): %s",
                    image_data_url[:30],
                    e,
                )
                continue

    return messages


class Filter:
    class Valves(BaseModel):
        max_dimension: int = Field(
            default=768, ge=1, description="Maximum image dimension."
        )

    class UserValves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    def inlet(self, body: dict, __user__=None) -> dict:
        messages = body.get("messages", [])
        messages = resize_images_in_messages(messages, self.valves.max_dimension)
        body["messages"] = messages
        return body
