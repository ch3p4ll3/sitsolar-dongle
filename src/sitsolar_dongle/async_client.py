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
  timeout -- so one ``except`` covers the lot.

A background task reads the connection and answers the dongle's unsolicited
heartbeat pushes (``0x43 0x01``, roughly 37 s apart) the moment they arrive,
which is what the official app does.  That is not cosmetic: measured against
real hardware, the dongle **closes a connection whose heartbeats go
unanswered**, dropping a silent client after about five missed pushes, while
an identical session that answers them stayed up for ten minutes and
survived.  Answering only during reads, as the blocking client does, leaves
the connection viable for roughly three minutes and no longer.

Owning the stream in one place also changes what a failure costs:

* cancelling or timing out a request no longer tears the connection.  The
  reader has already consumed whatever was on the wire, so the request simply
  abandons its future and the next one goes out on the same socket.  Under
  the old design every cancelled poll in Home Assistant dropped the
  connection, which under the dongle's ~75 s reconnect lockout meant a 75 s
  outage for a routine ``asyncio.timeout``.
* a dropped connection is detected as it happens rather than at the next
  poll, and any request already waiting on it fails immediately instead of
  burning its full timeout.
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
                 interframe: float = 0.05, keepalive: float = 90.0) -> None:
        super().__init__(host, serial, port=port, slave=slave,
                         timeout=timeout, interframe=interframe)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[Frame]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._generation = 0
        self._dead_reason: str | None = None
        #: How long the reader may hear nothing at all before it concludes
        #: the peer is gone.  Heartbeats arrive every ~37 s on a live link,
        #: so this is several missed pushes rather than a request timeout --
        #: it exists for half-open connections that never send a FIN, which
        #: ``readexactly`` would otherwise block on forever.
        self.keepalive = keepalive

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
        """Open the connection, start the reader and run the handshake.

        A connection that fails to open, or that opens and then fails the
        handshake, is dropped before this raises.  Otherwise a refused
        ``Accredit`` would leave the socket pinned open -- and with
        ``async with`` the exit hook never runs when the entry hook raises,
        so nothing else would close it.  The dongle then refuses new
        connections for roughly 75 s, which makes a leaked socket expensive.

        The generation is bumped first so a reader belonging to the *old*
        connection cannot fail the new one's requests if it happens to wake
        up during the reconnect.
        """
        await self.close()
        self._generation += 1
        self._dead_reason = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        except asyncio.TimeoutError as exc:
            raise DongleError("timed out connecting to %s:%d"
                              % (self.host, self.port)) from exc
        except OSError as exc:
            raise DongleError("cannot connect to %s:%d: %s"
                              % (self.host, self.port, exc)) from exc
        self._reader, self._writer = reader, writer

        self._reader_task = asyncio.create_task(
            self._reader_loop(self._generation, reader),
            name="sitsolar-dongle-reader")
        try:
            self.info = await self.handshake()
        except BaseException:
            self._drop()
            raise
        return self.info

    async def close(self) -> None:
        """Close the connection, if any, and stop the reader."""
        writer, self._writer = self._writer, None
        task, self._reader_task = self._reader_task, None
        self._reader = None
        self._fail_pending(DongleError("connection closed"))
        if task is not None:
            task.cancel()
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
        if task is not None:
            # the reader is cancelled above, so this never raises; gather
            # rather than await because catching CancelledError directly
            # around an await is the kind of thing that confuses asyncio
            await asyncio.gather(task, return_exceptions=True)

    def _drop(self) -> None:
        """Drop the connection immediately, awaiting nothing.

        Deliberately synchronous so it is safe from a ``except BaseException``
        handler, including while the task is being cancelled:
        ``writer.close()`` initiates the close without yielding, so the
        transport stops even if we are never scheduled again.  The reader
        task is cancelled here for the same reason -- awaiting it is left to
        :meth:`close`.
        """
        task, self._reader_task = self._reader_task, None
        if task is not None and not task.done():
            task.cancel()
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass

    def _fail_pending(self, exc: DongleError) -> None:
        """Hand *exc* to every request still waiting for a reply."""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    def _connection_lost(self, generation: int, reason: str) -> None:
        """The reader concluded the connection is finished."""
        if generation != self._generation:
            # a reader from a connection we have already replaced; letting it
            # tear anything down would fail a live session's requests
            return
        self._dead_reason = reason
        self._fail_pending(DongleError(reason))
        self._drop()

    # -- the reader -----------------------------------------------------
    async def _reader_loop(self, generation: int,
                           reader: asyncio.StreamReader) -> None:
        """Own the stream: answer pushes, deliver replies by transaction id.

        This is the only thing that reads, so no two consumers can interleave
        partial frames.  The stream is passed in rather than looked up, so a
        task that outlives its connection keeps reading the socket it was
        started for instead of its replacement.  It never raises out of the
        task, because an unretrieved task exception would surface as a warning
        long after the cause was already reported; it records the failure and
        stops instead.
        """
        try:
            while True:
                header = await self._read_exactly(reader, 7, "header",
                                                  generation)
                if header is None:
                    return
                tid, protocol, length, unit = parse_header(header)
                if length < 1:
                    self._connection_lost(
                        generation, "frame length %d makes no sense" % length)
                    return
                envelope = await self._read_exactly(
                    reader, length - 1, "body of a %d-byte frame" % length,
                    generation)
                if envelope is None:
                    return
                frame = Frame(tid=tid, protocol=protocol, unit=unit,
                              envelope=envelope)
                if frame.is_push:
                    await self._answer_push(frame)
                    continue
                future = self._pending.pop(tid, None)
                if future is not None and not future.done():
                    future.set_result(frame)
                # a reply with no waiter is a late answer to a request that
                # already timed out.  The frame is consumed either way, so
                # the stream stays on a frame boundary and the next request
                # is unaffected.
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._connection_lost(
                generation, "connection lost: %s" % (exc or type(exc).__name__))

    async def _read_exactly(self, reader: asyncio.StreamReader, size: int,
                            what: str, generation: int) -> bytes | None:
        """Read *size* bytes, or conclude the connection is finished.

        Returns ``None`` once the connection has been dropped, so the caller
        can stop rather than try to parse whatever was returned.  The
        *generation* is the reader's own, not the current one: a reader that
        outlived its connection must not tear down its replacement.
        """
        try:
            return await asyncio.wait_for(reader.readexactly(size),
                                          self.keepalive)
        except asyncio.IncompleteReadError:
            reason = "connection closed by the dongle"
        except asyncio.TimeoutError:
            reason = "no %s from %s for %.0fs" % (what, self.host,
                                                  self.keepalive)
        except (ConnectionError, OSError) as exc:
            reason = "connection lost: %s" % exc
        self._connection_lost(generation, reason)
        return None

    async def _answer_push(self, frame: Frame) -> None:
        """Reply to a heartbeat push exactly like the official app does."""
        try:
            payload = self._open(frame)
        except Exception:
            return
        if len(payload) < 2 or payload[:2] != CMD_HEARTBEAT:
            return
        writer = self._writer
        if writer is None or writer.is_closing():
            return
        try:
            writer.write(self._heartbeat_frame(frame.tid))
            await writer.drain()
        except (OSError, ConnectionError):
            pass

    # -- requests -------------------------------------------------------
    async def _request(self, payload: bytes) -> bytes:
        """Send one command and wait for the frame carrying its reply.

        The waiter is registered *before* the write, so a fast reply cannot
        arrive before there is somewhere to put it.
        """
        writer = self._writer
        if writer is None or writer.is_closing():
            raise DongleError("not connected")
        if self._dead_reason:
            raise DongleError(self._dead_reason)
        tid, wire = self._seal(payload)
        future: asyncio.Future[Frame] = asyncio.get_running_loop(
            ).create_future()
        self._pending[tid] = future
        try:
            writer.write(wire)
            await writer.drain()
            frame = await asyncio.wait_for(future, self.timeout)
            return self._open(frame)
        except asyncio.TimeoutError:
            # the reader is untouched by this: it keeps running and will
            # discard the late reply, so the connection stays usable
            raise DongleError("timed out waiting for a reply to %s after %.1fs"
                              % (payload[:2].hex(), self.timeout)) from None
        finally:
            if self._pending.get(tid) is future:
                del self._pending[tid]

    async def _command(self, payload: bytes) -> bytes:
        """Send a dongle command, serialised against other callers."""
        async with self._lock:
            return await self._request(payload)

    async def handshake(self) -> DongleInfo:
        """BeforeAccredit then Accredit; returns the dongle identity."""
        reply = await self._command(CMD_BEFORE_ACCREDIT)
        info = parse_dongle_info(reply)

        accredit = await self._command(CMD_ACCREDIT)
        result = accredit[2] if len(accredit) > 2 else 1
        if result != 0:
            raise DongleError("Accredit refused with result 0x%02X" % result)
        return info

    async def call_all(self) -> list[dict[str, Any]]:
        """``CallAll``: the dongle pushes its info and attached device list."""
        out: list[dict[str, Any]] = []
        payload = await self._command(CMD_CALL_ALL)
        js = payload.find(b"{")
        if js < 0:
            return out
        try:
            out.append(json.loads(payload[js:].decode("utf-8", "replace")))
        except json.JSONDecodeError:
            pass
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
            raw = await self._request(payload)
        reply = parse_read_reply(raw)
        if reply.slave != target:
            raise DongleError("reply came from slave %d, expected %d"
                              % (reply.slave, target))
        return reply.registers

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
