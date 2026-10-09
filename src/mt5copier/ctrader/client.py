"""A small asyncio client for the cTrader Open API.

Spotware's own Python package (``ctrader-open-api``) pins exact, old versions of Twisted,
protobuf, pyOpenSSL and requests, which would fight this project's own dependencies and the
frozen build. The protocol itself is small, so this speaks it directly: TLS to port 5035, each
message a ``ProtoMessage`` wrapper preceded by its length as four big-endian bytes, whose
``payloadType`` names the real message carried in ``payload``.

A request carries a ``clientMsgId``, and its answer comes back with the same one. Everything
else the server sends -- spot prices, execution events, disconnect notices -- is an *event*,
put on :attr:`CTraderClient.events` for the owner to read.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import struct
import uuid
from typing import Any

from google.protobuf.message import Message

from ..logging_setup import get_logger
from .proto import (
    OpenApiCommonMessages_pb2,
    OpenApiCommonModelMessages_pb2,
    OpenApiMessages_pb2,
    OpenApiModelMessages_pb2,
)

log = get_logger(__name__)

LIVE_HOST = "live.ctraderapi.com"
DEMO_HOST = "demo.ctraderapi.com"
PORT = 5035

HEARTBEAT_S = 10.0
"""The server drops a connection that has been silent for longer than about 30 s."""

REQUEST_TIMEOUT_S = 15.0

_MAX_FRAME = 32 * 1024 * 1024
"""A length prefix above this is a broken stream, not a message."""


def host_for(is_live: bool) -> str:
    """Live accounts and demo accounts are served from different hosts."""
    return LIVE_HOST if is_live else DEMO_HOST


def _payload_classes() -> dict[int, type[Message]]:
    """Every message class with a fixed ``payloadType``, keyed by it."""
    found: dict[int, type[Message]] = {}
    for module in (OpenApiCommonMessages_pb2, OpenApiMessages_pb2):
        for name in dir(module):
            cls = getattr(module, name)
            if not (isinstance(cls, type) and issubclass(cls, Message)):
                continue
            field = getattr(cls, "DESCRIPTOR").fields_by_name.get("payloadType")  # noqa: B009
            if field is not None and field.has_default_value:
                found[int(field.default_value)] = cls
    return found


_CLASSES = _payload_classes()
_HEARTBEAT_TYPE = int(OpenApiCommonModelMessages_pb2.HEARTBEAT_EVENT)
_ERROR_TYPES = {
    int(OpenApiCommonModelMessages_pb2.ERROR_RES),
    int(OpenApiModelMessages_pb2.PROTO_OA_ERROR_RES),
}


DISCONNECT_EVENTS = (
    OpenApiMessages_pb2.ProtoOAAccountsTokenInvalidatedEvent,
    OpenApiMessages_pb2.ProtoOAClientDisconnectEvent,
    OpenApiMessages_pb2.ProtoOAAccountDisconnectEvent,
)
"""Events after which the session is over."""


class OpenApiError(Exception):
    """The server answered a request with an error."""

    def __init__(self, code: str, description: str = "") -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


def payload_type(msg: Message) -> int:
    return int(msg.payloadType)


def encode(msg: Message, client_msg_id: str | None = None) -> bytes:
    """One framed message, ready to write."""
    wrapper = OpenApiCommonMessages_pb2.ProtoMessage(
        payloadType=payload_type(msg), payload=msg.SerializeToString()
    )
    if client_msg_id is not None:
        wrapper.clientMsgId = client_msg_id
    body = bytes(wrapper.SerializeToString())
    return struct.pack(">I", len(body)) + body


def decode(body: bytes) -> tuple[Message | None, str | None]:
    """A frame's body as ``(message, clientMsgId)``. The message is None for a payload type
    this client has no class for -- a newer server message, ignored rather than fatal."""
    wrapper = OpenApiCommonMessages_pb2.ProtoMessage()
    wrapper.ParseFromString(body)
    cls = _CLASSES.get(wrapper.payloadType)
    client_msg_id = wrapper.clientMsgId if wrapper.HasField("clientMsgId") else None
    if cls is None:
        return None, client_msg_id
    msg = cls()
    msg.ParseFromString(wrapper.payload)
    return msg, client_msg_id


class CTraderClient:
    """One connection to an Open API host."""

    def __init__(self, host: str, port: int = PORT, *, tls: bool = True) -> None:
        self.host = host
        self.port = port
        self.tls = tls
        """Off only in tests, against a local fake server."""
        self.events: asyncio.Queue[Message] = asyncio.Queue()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[Message]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._closed = asyncio.Event()
        self.close_reason: str | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._closed.is_set()

    async def connect(self, timeout: float = REQUEST_TIMEOUT_S) -> None:
        context = ssl.create_default_context() if self.tls else None
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=context), timeout
        )
        self._tasks = [
            asyncio.create_task(self._read_loop(), name="ctrader-read"),
            asyncio.create_task(self._heartbeat_loop(), name="ctrader-heartbeat"),
        ]

    async def close(self) -> None:
        self._shut("closed by us")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), 2.0)
        self._writer = None

    async def send(self, msg: Message, client_msg_id: str | None = None) -> None:
        if self._writer is None or self._closed.is_set():
            raise ConnectionError(self.close_reason or "not connected")
        self._writer.write(encode(msg, client_msg_id))
        await self._writer.drain()

    async def request(self, msg: Message, timeout: float = REQUEST_TIMEOUT_S) -> Message:
        """Send ``msg`` and wait for the answer that carries its ``clientMsgId``.

        Raises :class:`OpenApiError` when the answer is an error, and ``ConnectionError`` when
        the connection drops first."""
        client_msg_id = uuid.uuid4().hex
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[client_msg_id] = future
        try:
            await self.send(msg, client_msg_id)
            answer = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(client_msg_id, None)
        if payload_type(answer) in _ERROR_TYPES:
            code = str(getattr(answer, "errorCode", "ERROR"))
            raise OpenApiError(code, str(getattr(answer, "description", "")))
        return answer

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def _shut(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self.close_reason = reason
        self._closed.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError(reason))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                head = await self._reader.readexactly(4)
                (length,) = struct.unpack(">I", head)
                if length > _MAX_FRAME:
                    raise ConnectionError(f"frame of {length} bytes")
                msg, client_msg_id = decode(await self._reader.readexactly(length))
                if msg is None or payload_type(msg) == _HEARTBEAT_TYPE:
                    continue
                future = self._pending.get(client_msg_id) if client_msg_id else None
                if future is not None and not future.done():
                    future.set_result(msg)
                else:
                    self.events.put_nowait(msg)
        except asyncio.IncompleteReadError:
            self._shut("the server closed the connection")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("cTrader read failed: %r", exc)
            self._shut(f"read failed: {exc!r}")

    async def _heartbeat_loop(self) -> None:
        beat = OpenApiCommonMessages_pb2.ProtoHeartbeatEvent()
        try:
            while not self._closed.is_set():
                await asyncio.sleep(HEARTBEAT_S)
                await self.send(beat)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._shut(f"heartbeat failed: {exc!r}")
