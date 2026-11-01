"""Offline transport for tests and demos: scripted responses, every request recorded."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from .adapters import HttpRequest
from .push import HttpResponse


@dataclass
class FakeTransport:
    """Returns (or raises) the scripted items in order, then ``default`` (HTTP 200)."""

    script: Iterable[HttpResponse | BaseException] = ()
    default: HttpResponse = field(default_factory=lambda: HttpResponse(200, {"ok": True}))
    requests: list[HttpRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._queue: deque[HttpResponse | BaseException] = deque(self.script)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        item = self._queue.popleft() if self._queue else self.default
        if isinstance(item, BaseException):
            raise item
        return item
