import copy
import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

from .contracts import MAX_RAW_BYTES, MAX_RAW_ITEMS


class HistoryExpired(Exception):
    pass


class HistoryFull(Exception):
    pass


def validate_raw(items: list[dict]):
    if len(items) > MAX_RAW_ITEMS or len(json.dumps(items).encode()) > MAX_RAW_BYTES:
        raise HistoryFull()


@dataclass(frozen=True)
class Snapshot:
    side: str
    items: list[dict]
    turns: int
    expires: float
    response_id: str | None = None
    conversation_id: str | None = None
    provider_token: str | None = None


class HistoryStore:
    """Bounded, single-use capabilities for private provider conversation references."""

    def __init__(self, capacity=128, ttl=1200, clock=time.monotonic):
        self.capacity = capacity
        self.ttl = ttl
        self.clock = clock
        self._entries: OrderedDict[str, Snapshot] = OrderedDict()

    def _expire(self):
        for token in list(self._entries):
            if self._entries[token].expires <= self.clock():
                del self._entries[token]

    def get(self, token: str, side: str) -> Snapshot:
        self._expire()
        item = self._entries.get(token)
        if item is None or item.side != side:
            raise HistoryExpired()
        return copy.deepcopy(item)

    def take(self, token: str, side: str) -> Snapshot:
        item = self.get(token, side)
        del self._entries[token]
        return item

    def put(
        self, side: str, items: list[dict], turns: int,
        response_id: str | None = None, conversation_id: str | None = None,
        provider_token: str | None = None,
    ) -> str:
        validate_raw(items)
        self._expire()
        while len(self._entries) >= self.capacity:
            self._entries.popitem(last=False)
        token = secrets.token_urlsafe(32)
        self._entries[token] = Snapshot(
            side, copy.deepcopy(items), turns, self.clock() + self.ttl, response_id, conversation_id, provider_token,
        )
        return token
