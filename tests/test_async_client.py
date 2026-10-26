"""Tests for :class:`sitsolar_dongle.async_client.AsyncDongleClient`.

The client is exercised end to end against an in-process fake dongle that
speaks the real protocol: it derives the same keys, parses the encrypted
frames the client sends, and answers ``BeforeAccredit``/``Accredit``/
``CallAll``/FC reads the way the hardware does.  Everything runs through
``asyncio.run`` so no plugin is required.
"""

from __future__ import annotations

import asyncio
import gc
import struct
import time
import weakref

import pytest

from sitsolar_dongle import AsyncDongleClient, DongleError
from sitsolar_dongle.async_client import MAX_CONSECUTIVE_TIMEOUTS
from sitsolar_dongle.crc import crc16_bytes
from sitsolar_dongle.crypto import EnvelopeError, derive_keys, open_envelope
from sitsolar_dongle.framing import HEADER, PROTO_PUSH, parse_header
from sitsolar_dongle.session import (
    CMD_ACCREDIT,
    CMD_BEFORE_ACCREDIT,
    CMD_CALL_ALL,
    CMD_HEARTBEAT,
    seal_frame,
)

SERIAL = "TESTDONGLE0000001"


def first_block_start() -> int:
    """Address of the first range ``poll()`` asks for."""
    from sitsolar_dongle.registers import blocks

    return next(iter(blocks()))[0]


async def wait_until(predicate, timeout: float = 2.0) -> None:
    """Let the fake dongle's task catch up before asserting on it."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the dongle")
        await asyncio.sleep(0.01)


class FakeDongle:
    """A scripted dongle: real crypto, canned replies.

    The fault-injection flags exist so the failure paths can be tested
    without hardware: ``accredit_refused`` and ``corrupt_envelope`` reproduce
    the two ways a handshake can fail after the socket is already open,
    ``reply_delay`` holds a reply long enough to cancel a read mid-frame, and
    ``stall_after_header`` sends a header and then nothing, which is what a
    frame torn halfway really looks like on the wire.  ``heartbeat_interval``
    makes it push heartbeats on a timer whether or not anyone is asking,
    ``die_after`` aborts the socket after a delay, and ``ignore_reads``
    answers nothing but heartbeats -- the three states the reader task has to
    cope with on real hardware.
    """

    def __init__(self, serial: str = SERIAL, *,
                 registers: dict[int, int] | None = None,
                 refuse: set[int] | None = None, push_before_read: bool = False,
                 accredit_refused: bool = False, corrupt_envelope: bool = False,
                 corrupt_reads: bool = False,
                 reply_delay: float = 0.0, stall_after_header: bool = False,
                 heartbeat_interval: float = 0.0, die_after: float = 0.0,
                 ignore_reads: bool = False,
                 host: str = "127.0.0.1") -> None:
        self.serial = serial
        self.keys = derive_keys(serial)
        self.registers = registers or {}
        self.refuse = refuse or set()
        self.push_before_read = push_before_read
        self.accredit_refused = accredit_refused
        self.corrupt_envelope = corrupt_envelope
        self.corrupt_reads = corrupt_reads
        self.reply_delay = reply_delay
        self.stall_after_header = stall_after_header
        self.heartbeat_interval = heartbeat_interval
        self.die_after = die_after
        self.ignore_reads = ignore_reads
        self.host = host
        self.port = 0
        self.heartbeats = 0
        self.pushed = False
        self.stalled = False
        self.corrupted = False
        self.disconnects = 0
        self.connections = 0
        self.requests: list[bytes] = []
        self._beats = 0
        self._writers: set[asyncio.StreamWriter] = set()
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> FakeDongle:
        self._server = await asyncio.start_server(self._handle, self.host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        # Server.wait_closed() waits for the handlers, so a client a test
        # forgot to close would hang the fixture instead of failing it.
        for writer in list(self._writers):
            try:
                writer.close()
            except OSError:
                pass
        await self._server.wait_closed()

    # -- protocol ------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.add(writer)
        helpers = [asyncio.create_task(task(writer)) for task in (
            self._beat if self.heartbeat_interval else None,
            self._die if self.die_after else None,
        ) if task is not None]
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _protocol, length, _unit = parse_header(header)
                payload = open_envelope(await reader.readexactly(length - 1),
                                        self.keys)
                self.requests.append(payload)

                if payload[:2] == CMD_HEARTBEAT:
                    # the dongle never answers its own heartbeat
                    self.heartbeats += 1
                    continue

                if (self.push_before_read and payload[:6] == HEADER
                        and not self.pushed):
                    self.pushed = True
                    push = CMD_HEARTBEAT + struct.pack(">I", 1700000000)
                    writer.write(seal_frame(push, self.keys, tid=0x4242,
                                            protocol=PROTO_PUSH))
                    await writer.drain()

                if self.ignore_reads and payload[:6] == HEADER:
                    continue        # a dongle that has gone quiet mid-session

                reply = self._reply_for(payload)
                if reply is None:
                    continue
                if self.reply_delay and payload[:6] == HEADER:
                    # hold only register reads, so a handshake stays quick
                    await asyncio.sleep(self.reply_delay)
                wire = self._seal_reply(
                    reply, tid,
                    corrupt=bool(self.corrupt_reads and payload[:6] == HEADER
                                 and not self.corrupted))
                if self.corrupt_reads and payload[:6] == HEADER:
                    self.corrupted = True
                if (self.stall_after_header and payload[:6] == HEADER
                        and not self.stalled):
                    # header reaches the client, the body never does
                    self.stalled = True
                    writer.write(wire[:7])
                    await writer.drain()
                    await asyncio.sleep(3600)
                writer.write(wire)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, EnvelopeError):
            pass
        finally:
            for helper in helpers:
                helper.cancel()
            self.disconnects += 1
            self._writers.discard(writer)
            writer.close()

    async def _beat(self, writer: asyncio.StreamWriter) -> None:
        """Push heartbeats on a timer, the way the real dongle does."""
        try:
            while not writer.is_closing():
                await asyncio.sleep(self.heartbeat_interval)
                self._beats += 1
                push = CMD_HEARTBEAT + struct.pack(
                    ">I", int(time.time()) & 0xFFFFFFFF)
                # a distinct tid per push, so a client that answers them all
                # cannot look like it answered one repeatedly
                writer.write(seal_frame(push, self.keys,
                                        tid=0x7000 + self._beats,
                                        protocol=PROTO_PUSH))
                await writer.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass

    async def _die(self, writer: asyncio.StreamWriter) -> None:
        """Vanish without a handshake, the way a powered-off dongle does.

        ``abort()`` sends RST rather than a clean FIN, which is what makes
        the peer's ``readexactly`` fail instead of returning a short read.
        """
        try:
            await asyncio.sleep(self.die_after)
            if not writer.is_closing():
                writer.transport.abort()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass

    def _seal_reply(self, reply: bytes, tid: int, *,
                    corrupt: bool = False) -> bytes:
        wire = seal_frame(reply, self.keys, tid=tid)
        if not (corrupt or self.corrupt_envelope):
            return wire
        broken = bytearray(wire)
        broken[-1] ^= 0xFF          # flip a byte of the HMAC, length intact
        return bytes(broken)

    def _reply_for(self, payload: bytes) -> bytes | None:
        if payload[:2] == CMD_BEFORE_ACCREDIT[:2]:
            return self._info_payload()
        if payload[:2] == CMD_ACCREDIT[:2]:
            # the client reads the result from byte 2
            if self.accredit_refused:
                return bytes.fromhex("420301")     # result 1 = refused
            return bytes.fromhex("420300")         # result 0 = accepted
        if payload[:2] == CMD_CALL_ALL:
            return CMD_CALL_ALL + (b'{"devInfo":{"factory":"SiNeng",'
                                   b'"model":"3K-HS","sn":"%s",'
                                   b'"protocolIdentifier":"",'
                                   b'"devType":"33"}}' % self.serial.encode())
        if payload[:6] == HEADER:
            return self._read_reply(payload)
        return None

    def _info_payload(self) -> bytes:
        out = bytearray(bytes.fromhex("4202") + struct.pack(">I", 1))
        for text in (b"SiNeng", b"Si-Dongle-L", self.serial.encode()):
            out += struct.pack(">H", len(text)) + text
        out += struct.pack(">I", 1700000000)
        return bytes(out)

    def _read_reply(self, payload: bytes) -> bytes:
        slave, fc = payload[6], payload[7]
        address, count = struct.unpack(">HH", payload[8:12])

        if address in self.refuse:
            # the bare 64 07 <status> stub: nothing behind the prefix
            return bytes([0x64, 0x07, 0x01])

        body = bytes([slave, fc, count * 2])
        for offset in range(count):
            body += struct.pack(
                ">H", self.registers.get(address + offset, 0) & 0xFFFF)
        return bytes([0x64, 0x07, 0x00]) + body + crc16_bytes(body)


# -- lifecycle -----------------------------------------------------------

def test_handshake_populates_info() -> None:
    async def main() -> None:
        async with FakeDongle() as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                assert client.connected
                assert client.info is not None
                assert client.info.model == "Si-Dongle-L"
                assert client.info.serial == SERIAL
                assert client.info.factory == "SiNeng"
                assert client.info.time == 1700000000
            assert not client.connected

    asyncio.run(main())


def test_connect_returns_info_and_is_reentrant() -> None:
    async def main() -> None:
        async with FakeDongle() as dongle:
            client = AsyncDongleClient(dongle.host, SERIAL, port=dongle.port)
            first = await client.connect()
            assert first.serial == SERIAL
            # a second connect() must close the first connection first
            second = await client.connect()
            assert second == first
            await client.close()

    asyncio.run(main())


def test_operations_before_connect_fail_cleanly() -> None:
    async def main() -> None:
        client = AsyncDongleClient("127.0.0.1", SERIAL, port=9)
        assert not client.connected
        with pytest.raises(DongleError, match="not connected"):
            await client.read(31002, 2)
        with pytest.raises(DongleError, match="not connected"):
            await client.handshake()

    asyncio.run(main())


# -- reading -------------------------------------------------------------

def test_read_returns_registers() -> None:
    async def main() -> None:
        registers = {31002: 0, 31003: 50020}
        async with FakeDongle(registers=registers) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                assert await client.read(31002, 2) == (0, 50020)
                assert await client.read(31002, 1) == (0,)

    asyncio.run(main())


def test_poll_decodes_the_whole_map() -> None:
    async def main() -> None:
        registers = {31003: 50020}
        async with FakeDongle(registers=registers) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                values = await client.poll()
                assert values["grid_frequency"]["value"] == 50.02
                assert values["grid_frequency"]["unit"] == "Hz"
                assert "_unreadable_ranges" not in values
                assert len(values) == 235

    asyncio.run(main())


def test_poll_records_refused_ranges() -> None:
    async def main() -> None:
        # refuse the first address of the first read block
        first = first_block_start()
        async with FakeDongle(refuse={first}) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                values = await client.poll()
                failures = values["_unreadable_ranges"]
                assert len(failures) == 1
                assert failures[0]["range"].startswith(str(first))
                assert "stub" in failures[0]["error"]

    asyncio.run(main())


def test_poll_on_error_raise_propagates() -> None:
    async def main() -> None:
        from sitsolar_dongle.framing import DongleNoDataError

        async with FakeDongle(refuse={first_block_start()}) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                with pytest.raises(DongleNoDataError):
                    await client.poll(on_error="raise")

    asyncio.run(main())


def test_devices_parses_call_all() -> None:
    async def main() -> None:
        async with FakeDongle() as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                found = await client.devices()
                assert len(found) == 1
                assert found[0].model == "3K-HS"
                assert found[0].device_type == "33"
                assert found[0].modbus_id == 1

    asyncio.run(main())


# -- pushes and failures -------------------------------------------------

def test_heartbeat_push_is_answered() -> None:
    async def main() -> None:
        async with FakeDongle(push_before_read=True) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                # the push rides ahead of the reply; the read still succeeds
                assert await client.read(31002, 2) == (0, 0)
            # the server task needs a beat to read the heartbeat reply
            await wait_until(lambda: dongle.heartbeats == 1)

    asyncio.run(main())


def test_heartbeat_while_idle_is_answered() -> None:
    """A heartbeat arriving with no request in flight gets a prompt reply.

    This is the behaviour measured against real hardware, and the reason the
    reader task exists: the dongle closes a connection whose heartbeats go
    unanswered, dropping a silent client after roughly five missed pushes,
    while an identical session that answers them stayed up for ten minutes.
    Answering only during reads leaves the connection viable for about three
    minutes and no longer.
    """
    async def main() -> None:
        async with FakeDongle(heartbeat_interval=0.1) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                # nothing is requested in this window, so every heartbeat
                # here has to be answered by the reader on its own
                await wait_until(lambda: dongle.heartbeats >= 3, timeout=3.0)
                assert client.connected
                # and the session is still good afterwards
                assert await client.read(31002, 2) == (0, 0)

    asyncio.run(main())


def test_reader_keeps_an_idle_connection_alive() -> None:
    """Several heartbeat periods of silence do not disturb the session.

    The shape of the hardware experiment that motivated this design: leave
    the connection alone well past the heartbeat period, then poll.  Under
    the old scheme those pushes accumulated unanswered until the dongle
    dropped the client.
    """
    async def main() -> None:
        async with FakeDongle(heartbeat_interval=0.1) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                await asyncio.sleep(0.7)               # ~7 heartbeat periods
                assert dongle.heartbeats >= 5, dongle.heartbeats
                assert client.connected
                assert await client.poll()

    asyncio.run(main())


def test_request_timeout_keeps_the_connection() -> None:
    """A dongle that goes quiet mid-session does not cost the connection.

    Under the old design a timeout tore the socket down, so a failed poll
    also triggered the ~75 s reconnect lockout.  The reader consumes the
    wire either way, so a late reply is discarded by transaction id and the
    next request on the same socket is unaffected.
    """
    async def main() -> None:
        async with FakeDongle(ignore_reads=True) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port, timeout=0.3,
                                         keepalive=30.0) as client:
                with pytest.raises(DongleError, match="timed out"):
                    await client.read(31002, 2)

                assert client.connected, \
                    "a slow dongle must not cost the connection"

                # the dongle starts answering again
                dongle.ignore_reads = False
                assert await client.read(31002, 2) == (0, 0)
                assert client.connected

    asyncio.run(main())


def test_late_reply_is_discarded_not_misapplied() -> None:
    """A reply arriving after its request timed out is simply dropped.

    Discarding it is what keeps the stream on a frame boundary, which is
    what makes a request timeout survivable without reconnecting.
    """
    async def main() -> None:
        async with FakeDongle(reply_delay=0.5) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port, timeout=0.15,
                                         keepalive=30.0) as client:
                with pytest.raises(DongleError, match="timed out"):
                    await client.read(31002, 2)

                await asyncio.sleep(0.8)      # let the late reply land
                dongle.reply_delay = 0.0
                assert await client.read(31002, 2) == (0, 0)
                assert client.connected

    asyncio.run(main())


def test_reader_detects_an_abrupt_disconnect() -> None:
    """A dongle that vanishes is noticed without another request.

    ``abort()`` sends RST rather than FIN, so ``readexactly`` fails outright
    -- the same thing a powered-off dongle or a dropped wifi link looks like.
    """
    async def main() -> None:
        async with FakeDongle(die_after=0.3) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                assert client.connected
                await wait_until(lambda: not client.connected, timeout=3.0)

    asyncio.run(main())


def test_connection_loss_fails_a_pending_request_immediately() -> None:
    """A request already waiting does not burn its full timeout.

    The reader hands the failure straight to whoever is waiting, so a dead
    dongle surfaces at once instead of after ``timeout`` seconds.
    """
    async def main() -> None:
        async with FakeDongle(reply_delay=30.0, die_after=0.3) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port,
                                         timeout=10.0) as client:
                started = time.monotonic()
                with pytest.raises(DongleError, match="connection"):
                    await client.read(31002, 2)
                elapsed = time.monotonic() - started
                assert elapsed < 3.0, "waited %.1fs for a dead dongle" % elapsed
                assert not client.connected

    asyncio.run(main())


def test_reconnect_after_the_reader_dies() -> None:
    """A dropped connection can be reopened on the same client object."""
    async def main() -> None:
        async with FakeDongle(die_after=0.2) as dongle:
            client = AsyncDongleClient(dongle.host, SERIAL, port=dongle.port)
            await client.connect()
            await wait_until(lambda: not client.connected, timeout=3.0)

            dongle.die_after = 0.0         # the dongle is stable again
            info = await client.connect()
            assert info.serial == SERIAL
            assert await client.read(31002, 2) == (0, 0)
            await client.close()

    asyncio.run(main())


def test_consecutive_timeouts_drop_the_connection() -> None:
    """A dongle that stops answering is reclaimed, not retried forever.

    The keepalive only sees total *silence*, so a dongle that completes the
    handshake and then ignores reads stays "connected" for as long as the
    socket is. Nothing else would ever reconnect it — which is how an
    integration ends up retrying the same dead socket on every tick.
    """
    async def main() -> None:
        async with FakeDongle(ignore_reads=True) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port, timeout=0.2,
                                         keepalive=30.0) as client:
                for attempt in range(MAX_CONSECUTIVE_TIMEOUTS):
                    with pytest.raises(DongleError, match="timed out"):
                        await client.read(31002, 2)
                    if attempt < MAX_CONSECUTIVE_TIMEOUTS - 1:
                        assert client.connected, \
                            "a lone timeout must not cost the connection"

                assert not client.connected
                # the reason is reachable now, not a bare "not connected"
                with pytest.raises(DongleError, match="stopped answering"):
                    await client.read(31002, 2)

    asyncio.run(main())


def test_a_successful_reply_resets_the_timeout_streak() -> None:
    """Two timeouts and a success is not three timeouts.

    A busy inverter can miss a beat, so only a run of failures in a row says
    the connection is finished.
    """
    async def main() -> None:
        async with FakeDongle(ignore_reads=True) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port, timeout=0.2,
                                         keepalive=30.0) as client:
                for _ in range(MAX_CONSECUTIVE_TIMEOUTS - 1):
                    with pytest.raises(DongleError, match="timed out"):
                        await client.read(31002, 2)

                dongle.ignore_reads = False
                assert await client.read(31002, 2) == (0, 0)

                dongle.ignore_reads = True
                for _ in range(MAX_CONSECUTIVE_TIMEOUTS - 1):
                    with pytest.raises(DongleError, match="timed out"):
                        await client.read(31002, 2)
                assert client.connected, "the streak should have been reset"

    asyncio.run(main())


def test_a_dropped_client_is_collected_and_hangs_up() -> None:
    """A client dropped without close() does not hold its connection forever.

    The reader task used to keep its client alive, so an integration that
    reloaded without closing pinned the socket — and the dongle serves one
    connection at a time, so that locks every other client out until it is
    restarted. The reader keeps a weak reference instead and hangs up on the
    next frame.
    """
    async def main() -> None:
        async with FakeDongle(heartbeat_interval=0.1) as dongle:
            client = AsyncDongleClient(dongle.host, SERIAL, port=dongle.port)
            await client.connect()
            assert dongle.connections == 1

            ref = weakref.ref(client)
            del client
            gc.collect()
            assert ref() is None, "the reader must not keep its client alive"

            # the reader notices on the next heartbeat and closes the socket
            await wait_until(lambda: dongle.disconnects >= 1, timeout=3.0)

    asyncio.run(main())


def test_a_stale_reader_cannot_fail_a_new_connection() -> None:
    """A reader from a replaced connection leaves the replacement alone.

    The generation is what tells them apart: a reader only ever tears down
    the connection it was started for. Without that, a reader that happened
    to wake up during a reconnect would fail the new session's requests and
    drop a perfectly good socket.
    """
    async def main() -> None:
        async with FakeDongle() as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                client._connection_lost(client._generation - 1,
                                        "a stale reader says so")

                assert client.connected, \
                    "an old reader must not drop a live connection"
                assert client._dead_reason is None
                assert await client.read(31002, 2) == (0, 0)

    asyncio.run(main())


def test_wrong_serial_fails_and_closes() -> None:
    async def main() -> None:
        async with FakeDongle() as dongle:
            client = AsyncDongleClient(dongle.host,
                                       "300018101261010227",
                                       port=dongle.port, timeout=1.0)
            with pytest.raises(DongleError, match="closed by the dongle"):
                await client.connect()
            assert not client.connected

    asyncio.run(main())


def test_timeout_closes_the_connection() -> None:
    """A silent server must time out and leave the client disconnected."""

    async def silent(reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(4096):
                pass
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def main() -> None:
        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            client = AsyncDongleClient("127.0.0.1", SERIAL, port=port,
                                       timeout=0.2)
            with pytest.raises(DongleError, match="timed out|no data"):
                await client.connect()
            # the half-open connection is dropped rather than left desynced
            assert not client.connected
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(main())


def test_protocol_errors_share_one_base() -> None:
    """Everything the dongle can do wrong is catchable as DongleError.

    ``EnvelopeError``, ``ModbusError`` and ``DongleNoDataError`` used to
    derive straight from ``Exception``, so a caller catching ``DongleError``
    missed a bad HMAC entirely and the connection leaked.
    """
    from sitsolar_dongle import DongleNoDataError, EnvelopeError, ModbusError

    assert issubclass(EnvelopeError, DongleError)
    assert issubclass(ModbusError, DongleError)
    assert issubclass(DongleNoDataError, DongleError)
    # they stay distinct so finer-grained handlers keep working
    assert EnvelopeError is not DongleError
    assert ModbusError is not DongleError


def test_failed_handshake_closes_the_connection() -> None:
    """A refused Accredit must not leave the socket pinned open.

    ``async with`` never runs ``__aexit__`` when ``__aenter__`` raises, so
    ``connect()`` has to clean up after itself.  The dongle refuses fresh
    connections for roughly 75 s afterwards, which makes a leaked socket
    expensive.
    """
    async def main() -> None:
        async with FakeDongle(accredit_refused=True) as dongle:
            client = AsyncDongleClient(dongle.host, SERIAL,
                                       port=dongle.port, timeout=1.0)
            with pytest.raises(DongleError, match="Accredit refused"):
                await client.connect()
            assert not client.connected
            # the server saw its peer go away rather than sit on the socket
            await wait_until(lambda: dongle.disconnects == 1)

    asyncio.run(main())


def test_bad_hmac_is_a_dongle_error_and_closes() -> None:
    """A corrupted envelope raises EnvelopeError, which is a DongleError."""
    async def main() -> None:
        async with FakeDongle(corrupt_envelope=True) as dongle:
            client = AsyncDongleClient(dongle.host, SERIAL,
                                       port=dongle.port, timeout=1.0)
            with pytest.raises(DongleError, match="HMAC mismatch"):
                await client.connect()
            assert not client.connected
            await wait_until(lambda: dongle.disconnects == 1)

    asyncio.run(main())


def test_bad_envelope_mid_session_keeps_the_connection() -> None:
    """An `EnvelopeError` on a live connection does *not* drop it.

    The frame is read in full before its HMAC is checked, so the stream is
    still on a clean frame boundary and the socket stays usable. Only
    `connect()` tears down, where the handshake has failed outright.
    """
    async def main() -> None:
        async with FakeDongle(corrupt_reads=True) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port,
                                         timeout=1.0) as client:
                with pytest.raises(DongleError, match="HMAC mismatch"):
                    await client.read(31002, 2)

                assert client.connected
                # the very next request on the same socket succeeds
                assert await client.read(31002, 2) == (0, 0)

    asyncio.run(main())


def test_connection_refusal_is_a_dongle_error() -> None:
    """A refused socket surfaces as DongleError, not a bare OSError."""

    async def main() -> None:
        # nothing is listening on port 1
        client = AsyncDongleClient("127.0.0.1", SERIAL, port=1, timeout=1.0)
        with pytest.raises(DongleError, match="cannot connect"):
            await client.connect()
        assert not client.connected

    asyncio.run(main())


def test_cancellation_keeps_the_connection() -> None:
    """Cancelling a read no longer tears the connection down.

    Wrapping a call in ``asyncio.timeout`` is the normal Home Assistant
    pattern.  The reader owns the stream now, so a cancelled request only
    abandons its own future: the reply already on the wire is consumed and
    discarded by transaction id, and the next request reuses the socket.
    Previously this dropped the connection, which under the dongle's ~75 s
    reconnect lockout cost a 75 s outage for a routine cancellation.
    """
    async def main() -> None:
        async with FakeDongle(reply_delay=3.0) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port,
                                         timeout=10.0) as client:
                task = asyncio.create_task(client.read(31002, 2))
                await asyncio.sleep(0.2)          # let it get into the wait
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                assert client.connected, \
                    "a cancelled request must not drop the connection"

                # the reply to the cancelled request is still on its way; it
                # has to be consumed and dropped, not mistaken for an answer
                dongle.reply_delay = 0.0
                assert await client.read(31002, 2) == (0, 0)
                assert client.connected

    asyncio.run(main())


def test_reader_reclaims_a_connection_torn_mid_frame() -> None:
    """A dongle that stalls mid-frame is reclaimed by the reader.

    The header reaches the client and the body never does, so the stream is
    genuinely torn -- but only the reader knows, because it is the reader
    that consumed the header.  A caller cancelling its request cannot repair
    that, so the keepalive is what notices, and it does so without anybody
    having to reconnect by hand.
    """
    async def main() -> None:
        async with FakeDongle(stall_after_header=True) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port,
                                         timeout=10.0, keepalive=0.8) as client:
                task = asyncio.create_task(client.read(31002, 2))
                # wait for the header to be on the wire, then cancel: the
                # reader is left holding a half-consumed frame
                await wait_until(lambda: dongle.stalled)
                await asyncio.sleep(0.2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

                # live until the keepalive expires, then gone
                assert client.connected
                await wait_until(lambda: not client.connected, timeout=3.0)

                started = time.monotonic()
                info = await client.connect()
                assert info.serial == SERIAL
                assert time.monotonic() - started < 2.0

    asyncio.run(main())


def test_concurrent_reads_are_serialised() -> None:
    """Two callers must not interleave request/reply on one connection."""

    async def main() -> None:
        registers = {31002: 1111, 31003: 2222, 31004: 3333, 31005: 4444}
        async with FakeDongle(registers=registers) as dongle:
            async with AsyncDongleClient(dongle.host, SERIAL,
                                         port=dongle.port) as client:
                first, second = await asyncio.gather(
                    client.read(31002, 2),
                    client.read(31004, 2),
                )
                assert first == (1111, 2222)
                assert second == (3333, 4444)

    asyncio.run(main())


def test_seal_frame_sets_both_protocol_bytes() -> None:
    """The shared sealer must reproduce the app's header patching exactly."""
    from sitsolar_dongle.session import PROTO_HEARTBEAT_REPLY

    keys = derive_keys(SERIAL)
    wire = seal_frame(b"\x42\x02", keys, tid=7)
    tid, protocol, length, unit = parse_header(wire[:7])
    assert tid == 7
    assert protocol == 0x1000          # requests
    assert unit == 0
    assert length == (len(wire) - 6)

    beat = seal_frame(CMD_HEARTBEAT, keys, tid=7,
                      protocol=PROTO_HEARTBEAT_REPLY)
    assert parse_header(beat[:7])[1] == 0x1001   # heartbeat replies


def test_seal_frame_header_matches_the_app_patch() -> None:
    """Pin the outer header the vendor app produces by patching after encrypt.

    The app builds the plaintext with protocol ``0x0000``, encrypts, then
    patches byte 2 to ``0x10`` and rewrites the length.  The shared sealer
    writes both protocol bytes up front, so these are the bytes that must
    stay put for the dongle to accept a frame.
    """
    keys = derive_keys(SERIAL)
    wire = seal_frame(CMD_BEFORE_ACCREDIT, keys, tid=0x1234)
    envelope = wire[7:]

    assert wire[:2] == (0x1234).to_bytes(2, "big")       # tid
    assert wire[2:4] == bytes([0x10, 0x00])              # protocol 0x1000
    assert wire[6] == 0x00                               # unit
    # length counts the unit byte plus everything after the header
    assert wire[4:6] == (len(envelope) + 1).to_bytes(2, "big")
    # the length field must agree with the actual frame size on the wire
    assert parse_header(wire[:7])[2] == len(wire) - 6
    # and the payload survives the round trip
    assert open_envelope(envelope, keys) == CMD_BEFORE_ACCREDIT
