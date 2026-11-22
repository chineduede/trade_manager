"""The fake worker every engine test hangs off ``eng._workers``, said once.

A real worker is a spawned process with a command queue; these stand in for both ends so a
test can watch what the engine tried to send without crossing a process boundary. The
process remembers being joined or terminated, so shutdown tests can assert on the
lifecycle, and the queue can refuse (``accept=False``) to play a worker whose queue is
full.
"""

from __future__ import annotations

import queue
import time


class FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.alive = False

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False


class FakeQueue:
    """Records what the engine tried to send."""

    def __init__(self, accept: bool = True) -> None:
        self.sent: list[object] = []
        self.accept = accept

    def put_nowait(self, item: object) -> None:
        if not self.accept:
            raise queue.Full
        self.sent.append(item)

    def close(self) -> None:
        pass


class FakeWorker:
    def __init__(self, terminal_id: str, accept: bool = True) -> None:
        self.terminal_id = terminal_id
        self.process = FakeProcess()
        self.cmd_q = FakeQueue(accept=accept)
        self.started_at = time.monotonic()
