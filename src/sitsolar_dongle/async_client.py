"""Asyncio client for the dongle's TCP protocol on port 7078.

A drop-in async counterpart to
:class:`sitsolar_dongle.client.DongleClient`, sharing the same protocol core
(:mod:`sitsolar_dongle.session`), the same register map and the same error
types.  Written for Home Assistant and other asyncio applications:

* nothing blocks the event loop -- there is no ``socket``, ``time.sleep``
  or synchronous ``recv`` anywhere in here;
* concurrent callers are serialised with an :class:`asyncio.Lock`, because a
  Modbus request/reply pair cannot be interleaved on one connection;
* every read is bounded by ``timeout``;
* **every** failure surfaces as :class:`~sitsolar_dongle.errors.DongleError`
  -- refused accreditation, bad HMAC, Modbus exception, refused connection or
  timeout -- so one ``except`` covers the lot;
* anything that interrupts a read, including the caller cancelling the task,
  drops the connection rather than leaving a half-read frame on the stream.
  Reconnect with :meth:`connect`.

The dongle sends unsolicited heartbeat pushes (``0x43 0x01``, roughly 37 s
apart in the captures this was written from).  They are answered as and when
frames are read, exactly like the blocking client and the official app, so
between polls the replies simply accumulate in the TCP buffer.  That is fine
for polling intervals shorter than the heartbeat period; if you intend to
leave a connection idle for minutes at a time, reconnect per poll instead.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .framing import (
    FC_READ_INPUT,
    DongleNoDataError,
    Frame,
    ModbusError,
    build_read,
    parse_header,
    parse_read_reply,
)
from .registers import blocks
from .session import (
    CMD_ACCREDIT,
    CMD_BEFORE_ACCREDIT,
    CMD_CALL_ALL,
    CMD_HEARTBEAT,
    DEFAULT_PORT,
    ClientCore,
    Device,
    DongleError,
    DongleInfo,
    parse_dongle_info,
)

__all__ = ["AsyncDongleClient"]


class AsyncDongleClient(ClientCore):
    """Non-blocking read-only session with one dongle.

    Each client owns its connection; create one per dongle.  Usage::

        async with AsyncDongleClient("192.168.1.50", SERIAL) as dongle:
            values = await dongle.poll()

    On any transport failure the connection is closed and the next call to
    :meth:`connect` opens a fresh one.  Nothing is shared between instances,
    so several dongles can be polled concurrently.
    """

    def __init__(self, host: str, serial: str, *, port: int = DEFAULT_PORT,
                 slave: int = 1, timeout: float = 10.0,
                 interframe: float = 0.05) -> None:
        super().__init__(host, serial, port=port, slave=slave,
                         timeout=timeout, interframe=interframe)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------
    async def __aenter__(self) -> AsyncDongleClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def connected(self) -> bool:
        """True while there is a live connection to the dongle."""
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> DongleInfo:
        """Open the connection and run the accreditation handshake.

        A connection that fails to open, or that opens and then fails the
        handshake, is dropped before this raises.  Otherwise a refused
        ``Accredit`` would leave the socket pinned open -- and with
        ``async with`` the exit hook never runs when the entry hook raises,
        so nothing else would close it.  The dongle then refuses new
        connections for roughly 75 s, which makes a leaked socket expensive.
        """
        await self.close()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        except asyncio.TimeoutError as exc:
            raise DongleError("timed out connecting to %s:%d"
                              % (self.host, self.port)) from exc
        except OSError as exc:
            raise DongleError("cannot connect to %s:%d: %s"
                              % (self.host, self.port, exc)) from exc

        try:
            self.info = await self.handshake()
        except BaseException:
            self._drop()
            raise
        return self.info

    async def close(self) -> None:
        """Close the connection, if any."""
        writer = self._writer
        self._drop()
        if writer is None:
            return
        try:
            await writer.wait_closed()
        except (OSError, ConnectionError):
            pass

    def _drop(self) -> None:
        """Drop the connection immediately, awaiting nothing.

        Deliberately synchronous so it is safe from a ``except BaseException``
        handler, including while the task is being cancelled:
        ``writer.close()`` initiates the close without yielding, so the
        transport stops even if we are never scheduled again.
        """
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass

    # -- low level ------------------------------------------------------
    async def _send(self, payload: bytes, *, tid: int | None = None) -> int:
        if self._writer is None or self._writer.is_closing():
            raise DongleError("not connected")
        tid, wire = self._seal(payload, tid=tid)
        self._writer.write(wire)
        await self._writer.drain()
        return tid

    async def _take(self, size: int) -> bytes:
        """Read exactly *size* bytes, or drop the connection and fail.

        Anything that interrupts the read -- a timeout, the peer going away,
        or the caller cancelling the task -- can leave a partially consumed
        frame on the stream, so the connection is dropped rather than reused.
        A cancelled read is the easy one to miss: wrapping a call in
        ``asyncio.timeout`` and letting it expire is the normal Home
        Assistant pattern, and without this the next request would sit
        waiting for the rest of the torn frame.
        """
        if self._reader is None:
            raise DongleError("not connected")
        try:
            return await asyncio.wait_for(self._reader.readexactly(size),
                                          self.timeout)
        except asyncio.IncompleteReadError:
            self._drop()
            raise DongleError("connection closed by the dongle") from None
        except asyncio.TimeoutError:
            self._drop()
            raise DongleError("no data from %s for %.1fs"
                              % (self.host, self.timeout)) from None
        except BaseException:
            # cancellation, or anything unforeseen: never trust the stream
            self._drop()
            raise

    async def _next_frame(self) -> Frame:
        """Read the next non-push frame, answering heartbeats as they arrive.

        A plain coroutine rather than an async generator: callers loop over
        this with ``while True`` and ``break``/``return``, and an abandoned
        async generator would only be closed once the event loop got round to
        finalising it.
        """
        while True:
            header = await self._take(7)
            tid, protocol, length, unit = parse_header(header)
            if length < 1:
                raise DongleError("frame length %d makes no sense" % length)
            envelope = await self._take(length - 1)
            frame = Frame(tid=tid, protocol=protocol, unit=unit,
                          envelope=envelope)
            if frame.is_push:
                await self._answer_push(frame)
                continue
            return frame

    async def _answer_push(self, frame: Frame) -> None:
        """Reply to a heartbeat push exactly like the official app does."""
        try:
            payload = self._open(frame)
        except Exception:
            return
        if len(payload) < 2 or payload[:2] != CMD_HEARTBEAT:
            return
        if self._writer is None or self._writer.is_closing():
            return
        try:
            self._writer.write(self._heartbeat_frame(frame.tid))
            await self._writer.drain()
        except (OSError, ConnectionError):
            pass

    # -- commands -------------------------------------------------------
    async def _command(self, payload: bytes,
                       expect_tids: int = 1) -> list[bytes]:
        """Send a dongle command and collect replies until they run dry."""
        async with self._lock:
            tid = await self._send(payload)
            deadline = time.monotonic() + self.timeout
            out: list[bytes] = []
            pending = expect_tids
            while True:
                frame = await self._next_frame()
                if frame.tid != tid:
                    # a late push already handled itself; ignore the rest
                    continue
                out.append(self._open(frame))
                pending -= 1
                if pending <= 0:
                    break
                if time.monotonic() > deadline:
                    break
                if self.interframe:
                    await asyncio.sleep(self.interframe)
            return out

    async def handshake(self) -> DongleInfo:
        """BeforeAccredit then Accredit; returns the dongle identity."""
        reply = await self._command(CMD_BEFORE_ACCREDIT)
        if not reply:
            raise DongleError("the dongle ignored BeforeAccredit")
        info = parse_dongle_info(reply[0])

        accredit = await self._command(CMD_ACCREDIT)
        if not accredit:
            raise DongleError("the dongle ignored Accredit")
        result = accredit[0][2] if len(accredit[0]) > 2 else 1
        if result != 0:
            raise DongleError("Accredit refused with result 0x%02X" % result)
        return info

    async def call_all(self) -> list[dict[str, Any]]:
        """``CallAll``: the dongle pushes its info and attached device list."""
        out: list[dict[str, Any]] = []
        for payload in await self._command(CMD_CALL_ALL):
            js = payload.find(b"{")
            if js < 0:
                continue
            try:
                out.append(json.loads(payload[js:].decode("utf-8", "replace")))
            except json.JSONDecodeError:
                continue
        return out

    async def devices(self) -> list[Device]:
        """Attached devices, from the ``CallAll`` reply."""
        found: list[Device] = []
        for blob in await self.call_all():
            dev = blob.get("devInfo")
            if not isinstance(dev, dict):
                continue
            found.append(Device(
                factory=dev.get("factory", ""),
                model=dev.get("model", ""),
                serial=dev.get("sn", ""),
                protocol_identifier=dev.get("protocolIdentifier", ""),
                device_type=dev.get("devType", ""),
            ))
        return found

    async def read(self, address: int, count: int, *, slave: int | None = None,
                   fc: int = FC_READ_INPUT) -> tuple[int, ...]:
        """Read *count* registers starting at *address*."""
        target = self.slave if slave is None else slave
        payload = build_read(target, address, count, fc=fc)
        async with self._lock:
            tid = await self._send(payload)
            while True:
                frame = await self._next_frame()
                if frame.tid != tid:
                    continue
                reply = parse_read_reply(self._open(frame))
                if reply.slave != target:
                    raise DongleError("reply came from slave %d, expected %d"
                                      % (reply.slave, target))
                return reply.registers
        raise DongleError("no reply for read at %d (slave %d)"
                          % (address, target))

    async def poll(self, *, slave: int | None = None,
                   fc: int = FC_READ_INPUT,
                   on_error: str = "skip") -> dict[str, Any]:
        """Read every documented read-only register and decode it.

        Ranges the device refuses are skipped (``on_error="skip"``) because
        firmwares differ in which optional blocks they implement -- for
        instance a single-phase unit has no S or T phase, and unimplemented
        ranges come back either as a Modbus exception
        (:class:`~sitsolar_dongle.framing.ModbusError`) or as the dongle's
        bare ``64 07 01`` stub
        (:class:`~sitsolar_dongle.framing.DongleNoDataError`).  Both are
        recorded under ``_unreadable_ranges``.
        """
        target = self.slave if slave is None else slave
        words: dict[int, int] = {}
        failures: list[dict[str, Any]] = []

        for start, end in blocks():
            count = end - start + 1
            try:
                values = await self.read(start, count, slave=target, fc=fc)
            except (ModbusError, DongleNoDataError) as exc:
                self._note_failure(failures, start, end, exc, on_error)
                continue
            for offset, value in enumerate(values):
                words[start + offset] = value

        return self._assemble(words, failures)
