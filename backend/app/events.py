"""In-process SSE hub.

Lives in its own module so both the ingest endpoints (app.main) and the Plaid
sync (app.plaid_link) can publish refresh events without importing each other.
"""

from __future__ import annotations

import asyncio


class Hub:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: str) -> None:
        for q in list(self._subs):
            q.put_nowait(event)


hub = Hub()
