import asyncio
import json
import time
from collections import deque

from starlette.responses import JSONResponse


class RequestGuard:
    """Bound body size/read time, concurrent requests, and global request rate."""

    def __init__(self, app, path="/api/compare", body_limit=65536, concurrent=4, per_minute=12):
        self.app = app
        self.path = path
        self.body_limit = body_limit
        self.concurrent = concurrent
        self.per_minute = per_minute
        self.active = 0
        self.starts = deque()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != self.path or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))

        async def reject(status, message):
            await JSONResponse(
                {"error": message}, status_code=status,
                headers={"Cache-Control": "no-store", "Retry-After": "60"} if status == 429 else {},
            )(scope, receive, send)

        if headers.get(b"content-type", b"").split(b";")[0].strip() != b"application/json":
            return await reject(415, "Send application/json.")
        if headers.get(b"sec-fetch-site") == b"cross-site":
            return await reject(403, "Cross-site requests are not accepted.")
        now = time.monotonic()
        while self.starts and now - self.starts[0] >= 60:
            self.starts.popleft()
        if self.active >= self.concurrent or len(self.starts) >= self.per_minute:
            return await reject(429, "Request capacity reached. Try again later.")
        self.active += 1
        self.starts.append(now)
        try:
            body = bytearray()
            async with asyncio.timeout(10):
                while True:
                    part = await receive()
                    if part["type"] == "http.disconnect":
                        return
                    body.extend(part.get("body", b""))
                    if len(body) > self.body_limit:
                        return await reject(413, "Request body is too large.")
                    if not part.get("more_body"):
                        break
            delivered = False

            async def replay():
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, replay, send)
        except TimeoutError:
            await reject(408, "Request body timed out.")
        finally:
            self.active -= 1


class HostedRequestGuard:
    """Keep hosted requests foreground-only and ignore caller configuration overrides."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/responses" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        part = await receive()  # RequestGuard has already assembled and bounded the body.
        try:
            data = json.loads(part["body"])
            if not isinstance(data, dict) or data.get("background"):
                raise ValueError()
            if isinstance(data.get("input"), list) and len(data["input"]) > 160:
                raise ValueError()
            for key in (
                "model", "tools", "instructions", "agent_reference", "reasoning", "tool_choice",
                "temperature", "top_p", "max_output_tokens", "parallel_tool_calls", "include",
            ):
                data.pop(key, None)
        except (ValueError, KeyError):
            return await JSONResponse(
                {"error": {"code": "invalid_request", "message": "Use foreground requests with bounded text input."}},
                status_code=400,
            )(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": json.dumps(data).encode(), "more_body": False}

        await self.app(scope, replay, send)
