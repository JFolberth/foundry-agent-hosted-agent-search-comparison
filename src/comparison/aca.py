import json
from typing import Literal

import aiohttp
from pydantic import BaseModel, ConfigDict, Field

from .contracts import MAX_RAW_BYTES, MODEL_TIMEOUT


class ACAResponse(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    id: str = Field(min_length=1)
    object: Literal["response"]
    status: Literal["completed", "failed", "in_progress", "cancelled", "queued", "incomplete"]
    output: list[dict]
    tools: list[dict] = Field(default_factory=list)
    usage: dict | None = None


class ACAClient:
    def __init__(self, session, endpoint):
        self.session = session
        self.endpoint = endpoint.rstrip("/") + "/responses"
        self.responses = self

    async def create(self, *, input, extra_headers=None, **kwargs):
        headers = {
            name: value for name, value in (extra_headers or {}).items()
            if name in ("x-client-comparison-id", "x-client-traceparent")
        }
        async with self.session.post(
            self.endpoint,
            json={"input": input, "store": False, "stream": False},
            headers=headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=MODEL_TIMEOUT + 10),
        ) as response:
            if response.status != 200:
                raise aiohttp.ClientResponseError(
                    response.request_info, response.history, status=response.status,
                    message="ACA runtime request failed",
                )
            payload = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                payload.extend(chunk)
                if len(payload) > MAX_RAW_BYTES:
                    raise ValueError("ACA response exceeds size limit")
            return ACAResponse.model_validate(json.loads(payload))