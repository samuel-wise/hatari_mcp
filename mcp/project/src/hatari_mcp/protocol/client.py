"""Async HMCP v1 client: request ids, deadlines, chunks and notifications."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import codec
from .errors import BAD_FRAME, PROTOCOL, TIMEOUT, from_wire, make_protocol_error
from .frame import (
    DEFAULT_MAX_PAYLOAD,
    MAX_REQUEST_ID,
    Flags,
    Frame,
    FrameDecoder,
    FrameType,
)
from .hello import HelloAck, build_hello, parse_hello_ack

ReadFn = Callable[[], Awaitable[bytes]]
WriteFn = Callable[[bytes], Awaitable[None]]
CloseFn = Callable[[], Awaitable[None]]
NotificationFn = Callable[[dict[str, Any]], Any]

# Even for a large byte cap, a response may not claim an absurd chunk count:
# the final missing-index scan is bounded independently of the byte limit.
MAX_CHUNK_COUNT = 65536


@dataclass
class _Pending:
    future: asyncio.Future[Any]
    kind: str = "request"


@dataclass
class _Chunks:
    meta: dict[str, Any]
    total: int
    total_length: int | None
    parts: dict[int, bytes] = field(default_factory=dict)
    received: int = 0


class FramedClient:
    """HMCP client over a byte stream.

    The class also accepts injected ``read``/``write`` callables so the codec,
    deadlines and chunk reassembly can be tested without a socket. One reader
    task is started by :meth:`start`; tests may instead call :meth:`feed`.
    """

    def __init__(
        self,
        *,
        read: ReadFn | None = None,
        write: WriteFn | None = None,
        close: CloseFn | None = None,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        max_reassembled: int = 64 * 1024 * 1024,
        notification_handler: NotificationFn | None = None,
        default_timeout: float | None = 5.0,
    ):
        self._read = read
        self._write_fn = write
        self._close_fn = close
        self.decoder = FrameDecoder(max_payload=max_payload)
        self.max_payload = max_payload
        self.max_reassembled = max_reassembled
        self.notification_handler = notification_handler
        self.default_timeout = default_timeout
        self._pending: dict[int, _Pending] = {}
        self._used_ids: set[int] = set()
        self._resolved_ids: set[int] = set()
        self._quarantine: deque[int] = deque()
        self._next_id = 1
        self._chunks: dict[int, _Chunks] = {}
        self._send_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._closed = False
        self._last_notification_seq: int | None = None
        self.notifications: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []
        self.duplicate_responses = 0

    @classmethod
    def from_streams(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        **kwargs: Any,
    ) -> "FramedClient":
        async def read() -> bytes:
            return await reader.read(65536)

        async def write(data: bytes) -> None:
            writer.write(data)
            await writer.drain()

        async def close() -> None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        return cls(read=read, write=write, close=close, **kwargs)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._read is None:
            raise make_protocol_error(PROTOCOL, "No read callback configured.")
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._read is not None
        try:
            while not self._closed:
                data = await self._read()
                if not data:
                    break
                await self.feed(data)
            try:
                self.decoder.eof()
            except Exception as exc:
                self._fail_all(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_all(exc)
        finally:
            self._fail_all(make_protocol_error(PROTOCOL, "Framed connection closed."))

    async def feed(self, data: bytes) -> list[Frame]:
        frames = self.decoder.feed(data)
        for frame in frames:
            await self._handle_frame(frame)
        return frames

    async def _handle_frame(self, frame: Frame) -> None:
        if frame.type is FrameType.HELLO_ACK:
            self._resolve_hello_ack(frame)
        elif frame.type is FrameType.OK:
            await self._handle_ok(frame)
        elif frame.type is FrameType.ERR:
            self._handle_error(frame)
        elif frame.type is FrameType.NOTIFY:
            self._handle_notification(frame)
        elif frame.type is FrameType.PING:
            await self._send_frame(Frame(FrameType.PONG, frame.request_id, frame.payload))
        elif frame.type is FrameType.PONG:
            self._resolve(
                frame.request_id, {"pong": True, "request_id": frame.request_id}, kind="ping"
            )
        else:
            # HELLO/REQUEST from a peer are not expected on the client side.
            raise make_protocol_error(BAD_FRAME, f"Unexpected {frame.type.name} frame from server.")

    def _resolve_hello_ack(self, frame: Frame) -> None:
        try:
            ack = parse_hello_ack(frame.payload)
        except Exception as exc:
            self._reject(frame.request_id, exc)
            return
        self._resolve(frame.request_id, ack, kind="hello")

    async def _handle_ok(self, frame: Frame) -> None:
        if (frame.flags & Flags.MORE) and not (frame.flags & Flags.BINARY):
            raise make_protocol_error(PROTOCOL, "MORE flag is only valid on a BINARY response.")
        pending = frame.request_id in self._pending
        if not pending and (frame.flags & (Flags.BINARY | Flags.MORE)):
            if frame.request_id in self._resolved_ids:
                # A timed-out/cancelled request may still receive late chunks.
                # They are already quarantined and must not tear down the
                # connection or allocate reassembly state.
                self.duplicate_responses += 1
                return
            # Never create reassembly state for an unsolicited id: a hostile
            # peer could otherwise allocate unbounded chunk buffers.
            raise make_protocol_error(
                PROTOCOL, f"Binary/chunk response for non-pending request id {frame.request_id}."
            )
        try:
            if frame.flags & Flags.BINARY:
                result = codec.decode_binary(frame.payload)
                if frame.flags & Flags.MORE:
                    if not pending:
                        raise make_protocol_error(
                            PROTOCOL, "Chunk for a non-pending request id."
                        )
                    self._add_chunk(frame, result)
                    return
                if frame.request_id in self._chunks:
                    self._add_chunk(frame, result)
                    self._finish_chunks(frame.request_id)
                    return
                if not pending:
                    raise make_protocol_error(
                        PROTOCOL, "Binary response for a non-pending request id."
                    )
                self._resolve(frame.request_id, result)
                return
            ok, value = codec.decode_response(frame.payload)
            if not ok:
                self._reject(frame.request_id, from_wire(value))
            else:
                self._resolve(frame.request_id, value)
        except Exception as exc:
            self._reject(frame.request_id, exc)
            raise

    def _handle_error(self, frame: Frame) -> None:
        try:
            ok, value = codec.decode_response(frame.payload)
            if ok:
                self._resolve(frame.request_id, value)
            else:
                self._reject(frame.request_id, from_wire(value))
        except Exception as exc:
            self._reject(frame.request_id, exc)

    def _handle_notification(self, frame: Frame) -> None:
        try:
            text = frame.payload.decode("utf-8")
            event = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            event = {"kind": "protocol.error", "error": str(exc)}
        if not isinstance(event, dict):
            event = {"kind": "protocol.error", "payload": event}
        seq = event.get("seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            if self._last_notification_seq is not None and seq > self._last_notification_seq + 1:
                gap = {
                    "kind": "notification.gap",
                    "expected": self._last_notification_seq + 1,
                    "received": seq,
                    "missing": seq - self._last_notification_seq - 1,
                }
                self.gaps.append(gap)
                self.notifications.append(gap)
                self._emit(gap)
            self._last_notification_seq = seq
        self.notifications.append(event)
        self._emit(event)

    def _emit(self, event: dict[str, Any]) -> None:
        if self.notification_handler is None:
            return
        try:
            result = self.notification_handler(event)
        except Exception:
            return
        if inspect.isawaitable(result):
            asyncio.create_task(result)

    def _add_chunk(self, frame: Frame, result: codec.BinaryResult) -> None:
        if frame.request_id not in self._pending:
            raise make_protocol_error(
                PROTOCOL, "Binary chunk for a request id that is not pending."
            )
        meta = result.meta
        total = meta.get("total")
        sequence = meta.get("sequence")
        if not isinstance(total, int) or not isinstance(sequence, int):
            raise make_protocol_error(PROTOCOL, "MORE binary frame lacks sequence/total metadata.")
        if total <= 0:
            raise make_protocol_error(PROTOCOL, "Chunk total must be positive.")
        # A chunk carries at least one byte, so both the chunk count and the
        # declared total are bounded by the reassembly cap. This also keeps
        # the final missing-index scan linear in a sane number of chunks.
        max_chunks = max(1, min(int(self.max_reassembled), MAX_CHUNK_COUNT))
        if total > max_chunks:
            self._chunks.pop(frame.request_id, None)
            raise make_protocol_error(
                PROTOCOL,
                f"Chunk total {total} exceeds the reassembly cap {self.max_reassembled}.",
            )
        chunk = self._chunks.get(frame.request_id)
        if chunk is None:
            total_length = meta.get("total_length")
            if isinstance(total_length, int) and total_length > self.max_reassembled:
                raise make_protocol_error(
                    PROTOCOL, "Declared total_length exceeds the reassembly limit."
                )
            chunk = _Chunks(
                meta=dict(meta),
                total=total,
                total_length=total_length if isinstance(total_length, int) else None,
            )
            self._chunks[frame.request_id] = chunk
        if chunk.total != total:
            raise make_protocol_error(PROTOCOL, "Chunk total changed mid-response.")
        if sequence in chunk.parts:
            raise make_protocol_error(PROTOCOL, f"Duplicate chunk sequence {sequence}.")
        chunk.parts[sequence] = result.data
        chunk.received += len(result.data)
        if chunk.received > self.max_reassembled:
            self._chunks.pop(frame.request_id, None)
            raise make_protocol_error(PROTOCOL, "Reassembled binary data exceeds the limit.")

    def _finish_chunks(self, request_id: int) -> None:
        chunk = self._chunks.pop(request_id, None)
        if chunk is None:
            raise make_protocol_error(PROTOCOL, "Final chunk without a chunk buffer.")
        missing = [index for index in range(chunk.total) if index not in chunk.parts]
        if missing:
            raise make_protocol_error(
                PROTOCOL,
                "Binary response is missing chunks.",
                data={"missing": missing[:20]},
            )
        data = b"".join(chunk.parts[index] for index in range(chunk.total))
        if chunk.total_length is not None and len(data) != chunk.total_length:
            raise make_protocol_error(
                PROTOCOL,
                f"Reassembled length {len(data)} does not match total_length {chunk.total_length}.",
            )
        if len(data) > self.max_reassembled:
            raise make_protocol_error(PROTOCOL, "Reassembled binary data exceeds the limit.")
        meta = dict(chunk.meta)
        meta.pop("sequence", None)
        meta.pop("total", None)
        meta["length"] = len(data)
        meta["sha256"] = hashlib.sha256(data).hexdigest()
        self._resolve(request_id, codec.BinaryResult(meta, data))

    def _mark_resolved(self, request_id: int) -> None:
        if request_id not in self._resolved_ids:
            self._resolved_ids.add(request_id)
            self._quarantine.append(request_id)
        # Quarantine ids for a bounded window so a late duplicate cannot be
        # mistaken for a brand-new request reusing the same id.
        while len(self._quarantine) > 8192:
            old = self._quarantine.popleft()
            self._resolved_ids.discard(old)

    def _resolve(self, request_id: int, value: Any, *, kind: str | None = None) -> None:
        pending = self._pending.get(request_id)
        if pending is None:
            if request_id in self._resolved_ids or request_id in self._used_ids:
                self.duplicate_responses += 1
            return
        if kind is not None and pending.kind != kind:
            return
        self._pending.pop(request_id, None)
        self._used_ids.discard(request_id)
        self._mark_resolved(request_id)
        if not pending.future.done():
            pending.future.set_result(value)

    def _reject(self, request_id: int, exc: BaseException) -> None:
        existed = request_id in self._pending
        pending = self._pending.pop(request_id, None)
        self._used_ids.discard(request_id)
        self._chunks.pop(request_id, None)
        if existed:
            self._mark_resolved(request_id)
        if pending and not pending.future.done():
            pending.future.set_exception(exc)

    def _fail_all(self, exc: BaseException) -> None:
        self._closed = True
        for request_id in list(self._pending):
            self._reject(request_id, exc)
        self._chunks.clear()

    def _alloc_id(self) -> int:
        for _ in range(MAX_REQUEST_ID):
            request_id = self._next_id
            self._next_id = 1 if request_id >= MAX_REQUEST_ID else request_id + 1
            if request_id not in self._used_ids and request_id not in self._resolved_ids:
                self._used_ids.add(request_id)
                return request_id
        raise make_protocol_error(PROTOCOL, "No free request ids.")

    def _release_pending(
        self, request_id: int, future: asyncio.Future[Any] | None = None
    ) -> None:
        """Abandon an id without a response and quarantine it.

        Timeouts, cancellation and write failures all leave a request id that
        may still produce a late response; quarantining prevents reuse and lets
        late binary chunks be ignored instead of closing the connection.
        """
        pending = self._pending.get(request_id)
        if pending is not None and future is not None and pending.future is not future:
            return
        self._pending.pop(request_id, None)
        self._used_ids.discard(request_id)
        self._chunks.pop(request_id, None)
        self._mark_resolved(request_id)
        if future is not None and not future.done():
            future.cancel()

    async def _send_frame(self, frame: Frame) -> None:
        if self._closed:
            raise make_protocol_error(PROTOCOL, "Framed connection is closed.")
        if self._write_fn is None:
            raise make_protocol_error(PROTOCOL, "No write callback configured.")
        encoded = frame.encode(max_payload=self.max_payload)
        async with self._send_lock:
            await self._write_fn(encoded)

    async def request(
        self,
        op: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        flags: int = 0,
        request_id: int | None = None,
    ) -> Any:
        if flags & Flags.NO_REPLY:
            chosen = request_id if request_id is not None else self._alloc_id()
            try:
                await self._send_frame(
                    Frame(
                        FrameType.REQUEST,
                        chosen,
                        codec.encode_control(op, params),
                        flags=flags,
                    )
                )
            finally:
                self._used_ids.discard(chosen)
                self._mark_resolved(chosen)
            return None

        chosen = request_id if request_id is not None else self._alloc_id()
        if chosen in self._pending:
            raise make_protocol_error(PROTOCOL, f"Request id {chosen} is already in flight.")
        if request_id is not None:
            if chosen in self._resolved_ids:
                raise make_protocol_error(
                    PROTOCOL, f"Request id {chosen} was already used and is quarantined."
                )
            if chosen in self._used_ids:
                raise make_protocol_error(PROTOCOL, f"Request id {chosen} is already in use.")
            self._used_ids.add(chosen)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[chosen] = _Pending(future, "request")
        try:
            await self._send_frame(
                Frame(FrameType.REQUEST, chosen, codec.encode_control(op, params), flags=flags)
            )
            return await self._wait(future, timeout)
        finally:
            pending = self._pending.get(chosen)
            if pending is None or pending.future is future:
                self._release_pending(chosen, future)

    async def _wait(self, future: asyncio.Future[Any], timeout: float | None) -> Any:
        effective = self.default_timeout if timeout is None else timeout
        if effective is None:
            return await asyncio.shield(future)
        try:
            return await asyncio.wait_for(asyncio.shield(future), effective)
        except asyncio.TimeoutError:
            raise make_protocol_error(
                TIMEOUT,
                f"Framed request timed out after {effective:.1f}s.",
                "The emulator may be unresponsive or at a breakpoint.",
            ) from None

    async def handshake(
        self,
        *,
        client_name: str = "hatari-mcp",
        client_version: str = "0.1.0",
        capabilities: Any = None,
        timeout: float | None = None,
    ) -> HelloAck:
        chosen = self._alloc_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[chosen] = _Pending(future, "hello")
        try:
            await self._send_frame(
                Frame(
                    FrameType.HELLO,
                    chosen,
                    build_hello(
                        client_name=client_name,
                        client_version=client_version,
                        capabilities=capabilities,
                    ),
                )
            )
            result = await self._wait(future, timeout)
            if not isinstance(result, HelloAck):
                raise make_protocol_error(PROTOCOL, "HELLO did not return a HELLO_ACK.")
            return result
        finally:
            pending = self._pending.get(chosen)
            if pending is None or pending.future is future:
                self._release_pending(chosen, future)

    async def ping(self, payload: bytes = b"", *, timeout: float = 2.0) -> dict[str, Any]:
        chosen = self._alloc_id()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[chosen] = _Pending(future, "ping")
        try:
            await self._send_frame(Frame(FrameType.PING, chosen, payload))
            result = await self._wait(future, timeout)
            if not isinstance(result, dict):
                raise make_protocol_error(PROTOCOL, "Invalid PONG.")
            return result
        finally:
            pending = self._pending.get(chosen)
            if pending is None or pending.future is future:
                self._release_pending(chosen, future)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_all(make_protocol_error(PROTOCOL, "Client closed."))
        if self._close_fn is not None:
            try:
                await self._close_fn()
            except Exception:
                pass


async def connect_unix(path: str, **kwargs: Any) -> FramedClient:
    reader, writer = await asyncio.open_unix_connection(path)
    client = FramedClient.from_streams(reader, writer, **kwargs)
    await client.start()
    return client
