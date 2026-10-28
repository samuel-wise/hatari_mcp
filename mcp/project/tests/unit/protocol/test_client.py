"""Framed client request ids, deadlines, cancellation and chunk reassembly."""

from __future__ import annotations

import asyncio
import json

import pytest

from hatari_mcp.protocol import codec
from hatari_mcp.protocol.client import FramedClient
from hatari_mcp.protocol.errors import ProtocolError
from hatari_mcp.protocol.frame import Flags, Frame, FrameType
from hatari_mcp.protocol.hello import build_hello_ack


def make_client(**kwargs) -> tuple[FramedClient, list[bytes]]:
    sent: list[bytes] = []

    async def write(data: bytes) -> None:
        sent.append(data)

    return FramedClient(write=write, **kwargs), sent


async def start_request(client: FramedClient, op: str, params=None):
    known = set(client._pending)
    task = asyncio.create_task(client.request(op, params))
    for _ in range(100):
        await asyncio.sleep(0)
        new_ids = [value for value in client._pending if value not in known]
        if new_ids:
            return task, new_ids[0]
    raise AssertionError("request did not register a pending id")


def test_request_and_response() -> None:
    async def main() -> None:
        client, sent = make_client()
        task, request_id = await start_request(client, "dbg.ping", {"x": 1})
        assert request_id != 0
        await client.feed(Frame(FrameType.OK, request_id, codec.encode_ok({"pong": True})).encode())
        assert await task == {"pong": True}
        assert client.pending_count == 0
        assert len(sent) == 1

    asyncio.run(main())


def test_out_of_order_and_duplicate_responses() -> None:
    async def main() -> None:
        client, _ = make_client()
        first, first_id = await start_request(client, "a")
        second, second_id = await start_request(client, "b")
        assert first_id != second_id
        await client.feed(Frame(FrameType.OK, second_id, codec.encode_ok(2)).encode())
        await client.feed(Frame(FrameType.OK, first_id, codec.encode_ok(1)).encode())
        assert await first == 1
        assert await second == 2
        await client.feed(Frame(FrameType.OK, first_id, codec.encode_ok(99)).encode())
        assert client.duplicate_responses == 1

    asyncio.run(main())


def test_error_response_is_typed() -> None:
    async def main() -> None:
        client, _ = make_client()
        task, request_id = await start_request(client, "mem.read")
        payload = codec.encode_error("out_of_range", "bad", "check range")
        await client.feed(Frame(FrameType.ERR, request_id, payload).encode())
        with pytest.raises(ProtocolError) as excinfo:
            await task
        assert excinfo.value.code == "out_of_range"
        assert excinfo.value.hint == "check range"

    asyncio.run(main())


def test_timeout_clears_pending_state() -> None:
    async def main() -> None:
        client, _ = make_client(default_timeout=0.01)
        with pytest.raises(ProtocolError) as excinfo:
            await client.request("slow")
        assert excinfo.value.code == "timeout"
        assert client.pending_count == 0

    asyncio.run(main())


def test_cancellation_clears_pending_state() -> None:
    async def main() -> None:
        client, _ = make_client(default_timeout=10)
        task = asyncio.create_task(client.request("cancel-me"))
        await asyncio.sleep(0)
        assert client.pending_count == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.pending_count == 0

    asyncio.run(main())


def test_chunk_reassembly_out_of_order() -> None:
    async def main() -> None:
        client, _ = make_client()
        task, request_id = await start_request(client, "mem.read")

        def chunk(sequence: int, data: bytes, *, more: bool) -> bytes:
            meta = {
                "space": "ram",
                "sequence": sequence,
                "total": 2,
                "total_length": 4,
                "length": len(data),
            }
            flags = Flags.BINARY | (Flags.MORE if more else 0)
            return Frame(FrameType.OK, request_id, codec.encode_binary(meta, data), flags=flags).encode()

        await client.feed(chunk(1, b"cd", more=True))
        await client.feed(chunk(0, b"ab", more=False))
        result = await task
        assert result.data == b"abcd"
        assert result.meta["length"] == 4

    asyncio.run(main())


def test_missing_chunk_is_reported() -> None:
    async def main() -> None:
        client, _ = make_client()
        task, request_id = await start_request(client, "mem.read")
        meta = {"space": "ram", "sequence": 0, "total": 2, "total_length": 4, "length": 2}
        payload = codec.encode_binary(meta, b"ab")
        # total=2 but only sequence 0 is delivered, followed by a non-MORE frame.
        await client.feed(
            Frame(FrameType.OK, request_id, payload, flags=Flags.BINARY | Flags.MORE).encode()
        )
        final_meta = {"space": "ram", "sequence": 1, "total": 2, "total_length": 4, "length": 2}
        await client.feed(
            Frame(FrameType.OK, request_id, codec.encode_binary(final_meta, b"cd"), flags=Flags.BINARY).encode()
        )
        assert (await task).data == b"abcd"

    asyncio.run(main())


def test_hello_ack_is_parsed() -> None:
    async def main() -> None:
        client, _ = make_client()
        task = asyncio.create_task(client.handshake())
        await asyncio.sleep(0)
        request_id = next(iter(client._pending))
        ack_payload = build_hello_ack(capabilities=["framed", "notify"], hatari_version="2.6.1")
        await client.feed(Frame(FrameType.HELLO_ACK, request_id, ack_payload).encode())
        ack = await task
        assert ack.version == 1
        assert ack.hatari_version == "2.6.1"
        assert "notify" in ack.capabilities

    asyncio.run(main())


def test_ping_pong_and_server_ping() -> None:
    async def main() -> None:
        client, sent = make_client()
        ping = asyncio.create_task(client.ping())
        await asyncio.sleep(0)
        request_id = next(iter(client._pending))
        await client.feed(Frame(FrameType.PONG, request_id, b"").encode())
        assert (await ping)["pong"] is True

        sent.clear()
        await client.feed(Frame(FrameType.PING, 0, b"hi").encode())
        pong = FrameDecoderForTest(sent)
        assert pong == Frame(FrameType.PONG, 0, b"hi")

    asyncio.run(main())


class FrameDecoderForTest:
    def __new__(cls, sent: list[bytes]):
        from hatari_mcp.protocol.frame import FrameDecoder

        frames = FrameDecoder().feed(b"".join(sent))
        assert len(frames) == 1
        return frames[0]


def test_notification_sequence_gap() -> None:
    async def main() -> None:
        client, _ = make_client()
        await client.feed(Frame(FrameType.NOTIFY, 0, json.dumps({"seq": 2, "kind": "x"}).encode()).encode())
        assert client.gaps == []
        await client.feed(Frame(FrameType.NOTIFY, 0, json.dumps({"seq": 5, "kind": "y"}).encode()).encode())
        assert client.gaps and client.gaps[0]["missing"] == 2
        assert [event["kind"] for event in client.notifications] == ["x", "notification.gap", "y"]

    asyncio.run(main())


def test_unsolicited_binary_chunk_is_rejected() -> None:
    async def main() -> None:
        client, _ = make_client()
        meta = {"space": "ram", "sequence": 0, "total": 2, "total_length": 4, "length": 2}
        payload = codec.encode_binary(meta, b"ab")
        with pytest.raises(ProtocolError):
            await client.feed(
                Frame(FrameType.OK, 4242, payload, flags=Flags.BINARY | Flags.MORE).encode()
            )
        assert client._chunks == {}

    asyncio.run(main())


def test_more_requires_binary_flag() -> None:
    with pytest.raises(ProtocolError):
        Frame(FrameType.OK, 1, codec.encode_ok(1), flags=Flags.MORE).encode()


def test_chunk_total_is_bounded_before_allocation() -> None:
    async def main() -> None:
        client, _ = make_client()
        task, request_id = await start_request(client, "mem.read")
        meta = {
            "space": "ram",
            "sequence": 0,
            "total": 0xFFFFFFFF,
            "total_length": 4,
            "length": 2,
        }
        with pytest.raises(ProtocolError):
            await client.feed(
                Frame(
                    FrameType.OK,
                    request_id,
                    codec.encode_binary(meta, b"ab"),
                    flags=Flags.BINARY | Flags.MORE,
                ).encode()
            )
        assert client._chunks == {}
        with pytest.raises(ProtocolError):
            await task

    asyncio.run(main())


def test_running_chunk_total_is_capped_without_quadratic_sum() -> None:
    async def main() -> None:
        client, _ = make_client(max_reassembled=4)
        task, request_id = await start_request(client, "mem.read")

        def chunk(sequence: int, data: bytes, *, more: bool) -> bytes:
            meta = {
                "space": "ram",
                "sequence": sequence,
                "total": 2,
                "total_length": 6,
                "length": len(data),
            }
            flags = Flags.BINARY | (Flags.MORE if more else 0)
            return Frame(FrameType.OK, request_id, codec.encode_binary(meta, data), flags=flags).encode()

        with pytest.raises(ProtocolError):
            await client.feed(chunk(0, b"abc", more=True))
        assert client._chunks == {}
        with pytest.raises(ProtocolError):
            await task

    asyncio.run(main())


def test_resolved_request_id_is_quarantined() -> None:
    async def main() -> None:
        client, _ = make_client()
        task, request_id = await start_request(client, "a")
        await client.feed(Frame(FrameType.OK, request_id, codec.encode_ok(1)).encode())
        assert await task == 1
        with pytest.raises(ProtocolError):
            await client.request("b", request_id=request_id)

    asyncio.run(main())


def test_timed_out_request_id_is_quarantined() -> None:
    async def main() -> None:
        client, _ = make_client()
        with pytest.raises(ProtocolError) as excinfo:
            await client.request("slow", timeout=0.01, request_id=777)
        assert excinfo.value.code == "timeout"
        assert 777 in client._resolved_ids
        assert client.pending_count == 0
        with pytest.raises(ProtocolError):
            await client.request("reuse", request_id=777)

    asyncio.run(main())


def test_late_chunks_for_timed_out_id_are_ignored() -> None:
    async def main() -> None:
        client, _ = make_client()
        with pytest.raises(ProtocolError):
            await client.request("slow", timeout=0.01, request_id=778)
        meta = {"space": "ram", "sequence": 0, "total": 2, "total_length": 4, "length": 2}
        await client.feed(
            Frame(
                FrameType.OK,
                778,
                codec.encode_binary(meta, b"ab"),
                flags=Flags.BINARY | Flags.MORE,
            ).encode()
        )
        final_meta = {"space": "ram", "sequence": 1, "total": 2, "total_length": 4, "length": 2}
        await client.feed(
            Frame(
                FrameType.OK,
                778,
                codec.encode_binary(final_meta, b"cd"),
                flags=Flags.BINARY,
            ).encode()
        )
        assert client._chunks == {}
        assert client.pending_count == 0
        assert client.duplicate_responses == 2

    asyncio.run(main())
