"""Blocking client for the dongle's TCP protocol on port 7078.

Sequence, mirroring what the official app does on connect:

1. ``BeforeAccredit`` (``0x42 0x02``) -- the dongle answers with its version,
   factory, model, serial number and clock.
2. ``Accredit`` (``0x42 0x03``) -- the dongle answers with a single result
   byte.  Nothing else is served until this succeeds.
3. ``CallAll`` (``0x42 0x07``) -- optional; the dongle pushes its own device
   info plus the list of attached devices as JSON blobs.
4. FC 0x04 reads against slave ``0x01`` for the inverter's input registers.

The dongle also emits unsolicited heartbeat pushes (``0x43 0x01`` + a 4-byte
unix timestamp, protocol id ``0x0001``).  The app answers each one with a
heartbeat of its own, reusing the push's transaction id, so this client does
the same rather than letting the pushes pile up.

Unlike the asyncio client, there is no background reader here: a heartbeat is
only answered while a read is in flight, so between polls it sits in the TCP
buffer.  Measured against real hardware the dongle **closes a connection whose
heartbeats go unanswered**, dropping a silent client after roughly five missed
pushes -- about three minutes.  Keep polling more often than the ~37 s
heartbeat period, or use
:class:`sitsolar_dongle.async_client.AsyncDongleClient`, whose reader task
answers pushes immediately however long the caller stays quiet.

For asyncio applications (Home Assistant, long-running services) use
:class:`sitsolar_dongle.async_client.AsyncDongleClient` instead -- this class
blocks and must not be called from an event loop.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Iterator

from .framing import (DongleNoDataError, FC_READ_INPUT, Frame, ModbusError,
                      build_read, parse_header, parse_read_reply)
from .registers import blocks
from .session import (CMD_ACCREDIT, CMD_BEFORE_ACCREDIT, CMD_CALL_ALL,
                      CMD_HEARTBEAT, DEFAULT_PORT, ClientCore, Device,
                      DongleError, DongleInfo, parse_dongle_info)

__all__ = ["DongleClient", "DongleInfo", "Device", "DongleError"]


class DongleClient(ClientCore):
    """Read-only session with one dongle."""

    def __init__(self, host: str, serial: str, *, port: int = DEFAULT_PORT,
                 slave: int = 1, timeout: float = 10.0,
                 interframe: float = 0.05) -> None:
        super().__init__(host, serial, port=port, slave=slave,
                         timeout=timeout, interframe=interframe)
        self._sock: socket.socket | None = None
        self._rx = bytearray()

    # -- lifecycle ------------------------------------------------------
    def __enter__(self) -> "DongleClient":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> DongleInfo:
        """Open the socket and run the accreditation handshake."""
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._rx.clear()
        self.info = self.handshake()
        return self.info

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- low level ------------------------------------------------------
    def _send(self, payload: bytes, *, tid: int | None = None) -> int:
        if self._sock is None:
            raise DongleError("not connected")
        tid, wire = self._seal(payload, tid=tid)
        self._sock.sendall(wire)
        return tid

    def _iter_frames(self) -> Iterator[Frame]:
        """Yield frames off the wire, transparently answering heartbeats."""
        if self._sock is None:
            raise DongleError("not connected")
        while True:
            header = self._take(7)
            tid, protocol, length, _unit = parse_header(header)
            if length < 1:
                raise DongleError("frame length %d makes no sense" % length)
            envelope = self._take(length - 1)
            frame = Frame(tid=tid, protocol=protocol, unit=_unit,
                          envelope=envelope)
            if frame.is_push:
                self._answer_push(frame)
                continue
            yield frame

    def _take(self, size: int) -> bytes:
        """Read exactly *size* bytes from the socket."""
        if self._sock is None:
            raise DongleError("not connected")
        while len(self._rx) < size:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise DongleError("connection closed by the dongle")
            self._rx += chunk
        taken = bytes(self._rx[:size])
        del self._rx[:size]
        return taken

    def _answer_push(self, frame: Frame) -> None:
        """Reply to a heartbeat push exactly like the official app does."""
        try:
            payload = self._open(frame)
        except Exception:
            return
        if len(payload) < 2 or payload[:2] != CMD_HEARTBEAT:
            return
        if self._sock is not None:
            try:
                self._sock.sendall(self._heartbeat_frame(frame.tid))
            except OSError:
                pass

    # -- commands -------------------------------------------------------
    def _command(self, payload: bytes, expect_tids: int = 1) -> list[bytes]:
        """Send a dongle command and collect replies until they run dry."""
        tid = self._send(payload)
        deadline = time.monotonic() + self.timeout
        out: list[bytes] = []
        pending = expect_tids
        for frame in self._iter_frames():
            if frame.tid != tid:
                # a late push already handled itself; ignore anything else
                continue
            out.append(self._open(frame))
            pending -= 1
            if pending <= 0:
                break
            if time.monotonic() > deadline:
                break
            self._pause()
        return out

    def _pause(self) -> None:
        if self.interframe:
            time.sleep(self.interframe)

    def handshake(self) -> DongleInfo:
        """BeforeAccredit then Accredit; returns the dongle identity."""
        reply = self._command(CMD_BEFORE_ACCREDIT)
        if not reply:
            raise DongleError("the dongle ignored BeforeAccredit")
        info = parse_dongle_info(reply[0])

        accredit = self._command(CMD_ACCREDIT)
        if not accredit:
            raise DongleError("the dongle ignored Accredit")
        result = accredit[0][2] if len(accredit[0]) > 2 else 1
        if result != 0:
            raise DongleError("Accredit refused with result 0x%02X" % result)
        return info

    def call_all(self) -> list[dict[str, Any]]:
        """``CallAll``: the dongle pushes its info and attached device list."""
        out: list[dict[str, Any]] = []
        for payload in self._command(CMD_CALL_ALL):
            js = payload.find(b"{")
            if js < 0:
                continue
            try:
                out.append(json.loads(payload[js:].decode("utf-8", "replace")))
            except json.JSONDecodeError:
                continue
        return out

    def devices(self) -> list[Device]:
        """Attached devices, from the ``CallAll`` reply."""
        found: list[Device] = []
        for blob in self.call_all():
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

    def read(self, address: int, count: int, *, slave: int | None = None,
             fc: int = FC_READ_INPUT) -> tuple[int, ...]:
        """Read *count* registers starting at *address*."""
        target = self.slave if slave is None else slave
        payload = build_read(target, address, count, fc=fc)
        tid = self._send(payload)
        for frame in self._iter_frames():
            if frame.tid != tid:
                continue
            reply = parse_read_reply(self._open(frame))
            if reply.slave != target:
                raise DongleError("reply came from slave %d, expected %d"
                                  % (reply.slave, target))
            return reply.registers
        raise DongleError("no reply for read at %d (slave %d)"
                          % (address, target))

    def poll(self, *, slave: int | None = None, fc: int = FC_READ_INPUT,
             on_error: str = "skip") -> dict[str, Any]:
        """Read every documented read-only register and decode it.

        Ranges the device refuses are skipped (``on_error="skip"``) because
        firmwares differ in which optional blocks they implement -- for
        instance a single-phase unit has no S or T phase, and unimplemented
        ranges come back either as a Modbus exception
        (:class:`ModbusError`) or as the dongle's bare ``64 07 01`` stub
        (:class:`DongleNoDataError`).  Both are recorded under
        ``_unreadable_ranges``.
        """
        target = self.slave if slave is None else slave
        words: dict[int, int] = {}
        failures: list[dict[str, Any]] = []

        for start, end in blocks():
            count = end - start + 1
            try:
                values = self.read(start, count, slave=target, fc=fc)
            except (ModbusError, DongleNoDataError) as exc:
                self._note_failure(failures, start, end, exc, on_error)
                continue
            for offset, value in enumerate(values):
                words[start + offset] = value

        return self._assemble(words, failures)
