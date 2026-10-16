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
"""

from __future__ import annotations

import json
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any, Iterator

from . import framing
from .crypto import Keys, derive_keys, open_envelope, seal
from .decode import decode
from .framing import Frame, ModbusError, build_read, parse_header, \
    parse_read_reply, wrap_payload
from .registers import REGISTERS, blocks

__all__ = ["DongleClient", "DongleInfo", "Device", "DongleError"]

DEFAULT_PORT = 7078

# tcp/BeforeAccredit.json -> functionCode "66" (=0x42), ChildFunctionCode "2",
# nameLen 8, name "general\0".  Function codes are parsed as decimal by the
# app (TcpParamBean.getWriteDataHex uses Integer.parseInt with no radix), so
# the template's "66" is 0x42 on the wire.
_BEFORE_ACCREDIT = bytes.fromhex("42020008") + b"general\x00"

# tcp/Accredit.json -> same, ChildFunctionCode "3", plus an empty info field.
_ACCREDIT = bytes.fromhex("42030008") + b"general\x00" + bytes.fromhex("0000")

# tcp/CallAll.json -> functionCode "66", ChildFunctionCode "7".
_CALL_ALL = bytes.fromhex("4207")

# tcp/Heartbeat.json -> functionCode "67" (=0x43), ChildFunctionCode "1", time.
_HEARTBEAT = bytes.fromhex("4301")


class DongleError(Exception):
    """Protocol-level failure talking to the dongle."""


@dataclass(frozen=True)
class DongleInfo:
    """Reply to ``BeforeAccredit``."""

    version: int
    factory: str
    model: str
    serial: str
    time: int


@dataclass(frozen=True)
class Device:
    """One entry of the ``AffiliatedDevice`` reply."""

    factory: str
    model: str
    serial: str
    protocol_identifier: str
    device_type: str

    @property
    def modbus_id(self) -> int:
        """Modbus slave address of the attached device.

        The dongle itself owns address 0; devices behind it start at 1, which
        matches the protocol document's "default Modbus address: 1".
        """
        return 1


class DongleClient:
    """Read-only session with one dongle."""

    def __init__(self, host: str, serial: str, *, port: int = DEFAULT_PORT,
                 slave: int = 1, timeout: float = 10.0,
                 interframe: float = 0.05) -> None:
        self.host = host
        self.port = port
        self.slave = slave
        self.timeout = timeout
        self.interframe = interframe
        self.keys: Keys = derive_keys(serial)
        self.info: DongleInfo | None = None
        self._sock: socket.socket | None = None
        self._tid = 0
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
    def _next_tid(self) -> int:
        tid = self._tid
        self._tid = (self._tid + 1) & 0xFFFF
        return tid

    def _send(self, payload: bytes, *, protocol: int = 0,
              tid: int | None = None) -> int:
        if self._sock is None:
            raise DongleError("not connected")
        if tid is None:
            tid = self._next_tid()
        # plaintext protocol id is 0x0000; _seal_frame patches byte 2 to 0x10
        wire = self._seal_frame(wrap_payload(payload, tid, protocol))
        self._sock.sendall(wire)
        return tid

    def _seal_frame(self, plain: bytes) -> bytes:
        """Replace the plaintext payload with the sealed envelope.

        ``seal`` works on payloads, but the outer header must survive in
        cleartext, so the frame is rebuilt by hand the way ``utils.a.m58``
        does it: keep the 7-byte header, then append ``IV | CT | HMAC`` and
        patch the protocol id and length field afterwards.
        """
        header, payload = plain[:7], plain[7:]
        envelope = seal(payload, self.keys)
        out = bytearray(header + envelope)
        out[2] = (framing.PROTO_REQUEST >> 8) & 0xFF
        out[4] = (len(envelope) + 1) >> 8
        out[5] = (len(envelope) + 1) & 0xFF
        return bytes(out)

    def _open(self, frame: Frame) -> bytes:
        return open_envelope(frame.envelope, self.keys)

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
        if len(payload) >= 2 and payload[0] == 0x43 and payload[1] == 0x01:
            reply = _HEARTBEAT + struct.pack(">I", int(time.time()) & 0xFFFFFFFF)
            plain = wrap_payload(reply, frame.tid, 0x1001)
            wire = bytearray(self._seal_frame(plain))
            wire[3] = 0x01  # protocol low byte, per WifiTcpService.m50
            if self._sock is not None:
                try:
                    self._sock.sendall(bytes(wire))
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
        reply = self._command(_BEFORE_ACCREDIT)
        if not reply:
            raise DongleError("the dongle ignored BeforeAccredit")
        info = self._parse_dongle_info(reply[0])

        accredit = self._command(_ACCREDIT)
        if not accredit:
            raise DongleError("the dongle ignored Accredit")
        result = accredit[0][2] if len(accredit[0]) > 2 else 1
        if result != 0:
            raise DongleError("Accredit refused with result 0x%02X" % result)
        return info

    @staticmethod
    def _parse_dongle_info(payload: bytes) -> DongleInfo:
        if len(payload) < 8 or payload[0] != 0x42 or payload[1] != 0x02:
            raise DongleError("unexpected BeforeAccredit reply: %s" % payload.hex())
        # layout: 42 02 | version(4) | len(2)+factory | len(2)+model
        #         | len(2)+serial | clock(4)
        pos = 2
        version = struct.unpack(">I", payload[pos:pos + 4])[0]
        pos += 4

        def text() -> str:
            nonlocal pos
            if pos + 2 > len(payload):
                raise DongleError("truncated BeforeAccredit reply: %s"
                                  % payload.hex())
            length = struct.unpack(">H", payload[pos:pos + 2])[0]
            pos += 2
            if pos + length > len(payload):
                raise DongleError("truncated BeforeAccredit reply: %s"
                                  % payload.hex())
            value = payload[pos:pos + length].decode("ascii", "replace").rstrip("\x00")
            pos += length
            return value

        factory, model, serial = text(), text(), text()
        clock = struct.unpack(">I", payload[pos:pos + 4])[0] if len(payload) >= pos + 4 else 0
        return DongleInfo(version=version, factory=factory, model=model,
                          serial=serial, time=clock)

    def call_all(self) -> list[dict[str, Any]]:
        """``CallAll``: the dongle pushes its info and attached device list."""
        out: list[dict[str, Any]] = []
        for payload in self._command(_CALL_ALL):
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
             fc: int = framing.FC_READ_INPUT) -> tuple[int, ...]:
        """Read *count* registers starting at *address*."""
        if not 1 <= count <= framing.MAX_REGISTERS:
            raise ValueError("count must be 1..%d" % framing.MAX_REGISTERS)
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
        raise DongleError("no reply for read at %d (slave %d)" % (address, target))

    def poll(self, *, slave: int | None = None, fc: int = framing.FC_READ_INPUT,
             on_error: str = "skip") -> dict[str, Any]:
        """Read every documented read-only register and decode it.

        Ranges that raise a Modbus exception are skipped (``on_error="skip"``)
        because firmwares differ in which optional blocks they implement --
        for instance a single-phase unit has no S or T phase.
        """
        target = self.slave if slave is None else slave
        words: dict[int, int] = {}
        failures: list[dict[str, Any]] = []

        for start, end in blocks():
            count = end - start + 1
            try:
                values = self.read(start, count, slave=target, fc=fc)
            except ModbusError as exc:
                if on_error == "raise":
                    raise
                failures.append({"range": "%d~%d" % (start, end),
                                 "error": str(exc)})
                continue
            for offset, value in enumerate(values):
                words[start + offset] = value

        result: dict[str, Any] = {}
        for reg in REGISTERS:
            got = tuple(words.get(a) for a in range(reg.address, reg.end + 1))
            if any(word is None for word in got):
                result[reg.key] = {"address": reg.span, "name": reg.name,
                                   "section": reg.section, "error": "unavailable"}
                continue
            result[reg.key] = decode(reg, tuple(got))  # type: ignore[arg-type]

        if failures:
            result["_unreadable_ranges"] = failures
        return result
