"""
title: Image Resizer
author: EaV Solution
version: 0.2
description: Downscales oversized images in chat messages before they reach the model.
"""

import base64
import io
import logging

from pydantic import BaseModel, Field
from PIL import Image

log = logging.getLogger(__name__)

# JPEG can't encode an alpha channel; flatten onto a white background first.
ALPHA_MODES = {"RGBA", "LA", "PA"}


def resize_images_in_messages(messages, max_dimension=768):
    for message in messages:
        for item in message.get("content", []):
            if not (isinstance(item, dict) and item.get("type") == "image_url"):
                continue
            if item.get("_resized"):
                continue

            image_url = item.get("image_url")
            if not isinstance(image_url, dict):
                continue
            image_data_url = image_url.get("url", "")
            if not image_data_url.startswith("data:image/"):
                continue

            try:
                header, encoded = image_data_url.split(",", 1)
                image_mime_type = header.split(":")[1].split(";")[0]

                image_data = base64.b64decode(encoded)
                image = Image.open(io.BytesIO(image_data))
                width, height = image.size
                # Pillow detects the real format from the decoded bytes,
                # which is more reliable than trusting the mime type string.
                image_format = image.format or image_mime_type.split("/")[1].upper()

                if max(width, height) > max_dimension:
                    scaling_factor = max_dimension / max(width, height)
                    new_width = int(width * scaling_factor)
                    new_height = int(height * scaling_factor)
                    image = image.resize((new_width, new_height), Image.LANCZOS)

                    if image_format == "JPEG" and image.mode in ALPHA_MODES:
                        image = image.convert("RGB")

                    buffered = io.BytesIO()
                    image.save(buffered, format=image_format)
                    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
                    new_data_url = f"data:{image_mime_type};base64,{img_str}"
                    image_url["url"] = new_data_url

                item["_resized"] = True
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
        max_dimension: int = Field(default=768, description="Maximum image dimension.")

    class UserValves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    def inlet(self, body: dict, __user__=None) -> dict:
        messages = body["messages"]
        messages = resize_images_in_messages(messages, self.valves.max_dimension)
        body["messages"] = messages
        return body
