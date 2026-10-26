"""Transport-independent session logic shared by both clients.

``client`` speaks blocking sockets and ``async_client`` speaks ``asyncio``.
They differ in how bytes reach the wire and nothing else: key derivation,
frame sealing, parsing the accreditation replies and turning raw registers
into decoded values are identical, so they live here and the two clients
cannot drift apart.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Any

from .crypto import Keys, derive_keys, open_envelope, seal
from .decode import decode
from .errors import DongleError
from .framing import PROTO_REQUEST, Frame, wrap_payload
from .registers import REGISTERS

__all__ = [
    "CMD_ACCREDIT",
    "CMD_BEFORE_ACCREDIT",
    "CMD_CALL_ALL",
    "CMD_HEARTBEAT",
    "DEFAULT_PORT",
    "PROTO_HEARTBEAT_REPLY",
    "ClientCore",
    "Device",
    "DongleError",
    "DongleInfo",
    "parse_dongle_info",
    "seal_frame",
]

DEFAULT_PORT = 7078

# tcp/BeforeAccredit.json -> functionCode "66" (=0x42), ChildFunctionCode "2",
# nameLen 8, name "general\0".  Function codes are parsed as decimal by the
# app (TcpParamBean.getWriteDataHex uses Integer.parseInt with no radix), so
# the template's "66" is 0x42 on the wire.
CMD_BEFORE_ACCREDIT = bytes.fromhex("42020008") + b"general\x00"

# tcp/Accredit.json -> same, ChildFunctionCode "3", plus an empty info field.
CMD_ACCREDIT = bytes.fromhex("42030008") + b"general\x00" + bytes.fromhex("0000")

# tcp/CallAll.json -> functionCode "66", ChildFunctionCode "7".
CMD_CALL_ALL = bytes.fromhex("4207")

# tcp/Heartbeat.json -> functionCode "67" (=0x43), ChildFunctionCode "1", time.
CMD_HEARTBEAT = bytes.fromhex("4301")

# The app answers a heartbeat with protocol id 0x1001 (WifiTcpService.m50).
PROTO_HEARTBEAT_REPLY = 0x1001


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


def seal_frame(payload: bytes, keys: Keys, *, tid: int,
               protocol: int = PROTO_REQUEST) -> bytes:
    """Build a complete wire frame: 7-byte clear header + sealed envelope.

    ``seal`` works on payloads, but the outer header has to stay in cleartext,
    so the frame is rebuilt by hand the way ``utils.a.m58`` does it: keep the
    header from :func:`~sitsolar_dongle.framing.wrap_payload`, append
    ``IV | CT | HMAC``, then patch the protocol id and the length field.
    Both protocol bytes are written here -- ``0x1000`` for requests and
    ``0x1001`` for heartbeat replies -- which is what the app achieves by
    patching byte 2 (and byte 3) after encrypting.
    """
    plain = wrap_payload(payload, tid, protocol)
    header, body = plain[:7], plain[7:]
    envelope = seal(body, keys)
    out = bytearray(header + envelope)
    out[2] = (protocol >> 8) & 0xFF
    out[3] = protocol & 0xFF
    out[4] = (len(envelope) + 1) >> 8
    out[5] = (len(envelope) + 1) & 0xFF
    return bytes(out)


def parse_dongle_info(payload: bytes) -> DongleInfo:
    """Parse the decrypted payload of a ``BeforeAccredit`` reply."""
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
        value = payload[pos:pos + length].decode("ascii", "replace")
        pos += length
        return value.rstrip("\x00")

    factory, model, serial = text(), text(), text()
    if len(payload) >= pos + 4:
        clock = struct.unpack(">I", payload[pos:pos + 4])[0]
    else:
        clock = 0
    return DongleInfo(version=version, factory=factory, model=model,
                      serial=serial, time=clock)


class ClientCore:
    """Everything about a session that does not depend on the transport.

    Holds the derived keys, the transaction id counter and the pure
    encoding/decoding steps.  The concrete clients add only the bytes going
    on and off the wire.
    """

    def __init__(self, host: str, serial: str, *, port: int = DEFAULT_PORT,
                 slave: int = 1, timeout: float = 10.0,
                 interframe: float = 0.05) -> None:
        self.host = host
        self.serial = serial
        self.port = port
        self.slave = slave
        self.timeout = timeout
        self.interframe = interframe
        self.keys: Keys = derive_keys(serial)
        self.info: DongleInfo | None = None
        self._tid = 0
        #: Read ranges the *dongle's* forwarding table refused (bare
        #: ``64 07 01`` stub), mapped to the failure record to keep
        #: reporting.  Probed once per connection and then skipped on the
        #: wire -- see :meth:`_note_failure`.
        self._parked: dict[tuple[int, int], dict[str, Any]] = {}

    def __repr__(self) -> str:
        return "%s(host=%r, port=%r)" % (type(self).__name__, self.host,
                                         self.port)

    # Kept as a class attribute so the pre-refactor
    # ``DongleClient._parse_dongle_info`` attribute path still resolves.
    _parse_dongle_info = staticmethod(parse_dongle_info)

    # -- transaction ids ----------------------------------------------
    def _next_tid(self) -> int:
        tid = self._tid
        self._tid = (self._tid + 1) & 0xFFFF
        return tid

    # -- envelope ------------------------------------------------------
    def _seal(self, payload: bytes, *, tid: int | None = None,
              protocol: int = PROTO_REQUEST) -> tuple[int, bytes]:
        """Allocate a transaction id (unless one is given) and seal."""
        if tid is None:
            tid = self._next_tid()
        return tid, seal_frame(payload, self.keys, tid=tid, protocol=protocol)

    def _open(self, frame: Frame) -> bytes:
        return open_envelope(frame.envelope, self.keys)

    def _heartbeat_frame(self, tid: int) -> bytes:
        """Sealed wire frame answering a ``43 01`` push, carrying the time."""
        payload = CMD_HEARTBEAT + struct.pack(
            ">I", int(time.time()) & 0xFFFFFFFF)
        return self._seal(payload, tid=tid, protocol=PROTO_HEARTBEAT_REPLY)[1]

    # -- decode --------------------------------------------------------
    def _note_failure(self, failures: list[dict[str, Any]], start: int,
                      end: int, exc: Exception, on_error: str,
                      *, park: bool = False) -> None:
        """Record a refused range, or re-raise it under ``on_error="raise"``.

        With *park* the refusal is remembered on the client, so later polls
        report the very same record without reading the range again.  Only
        the dongle's bare ``64 07 01`` stub earns that: it means "outside my
        forwarding table", which a reconnect can change but a retry cannot.
        Inverter-side :class:`ModbusError` is never parked -- an exception
        like 0x06 slave-busy is transient, and parking it would drop the
        range for the rest of the session.
        """
        if on_error == "raise":
            raise exc
        record = {"range": "%d~%d" % (start, end), "error": str(exc)}
        if park:
            self._parked[(start, end)] = record
        failures.append(record)

    def _parked_record(self, start: int, end: int) -> dict[str, Any] | None:
        """A copy of the stored refusal for *(start, end)*, if it is parked."""
        record = self._parked.get((start, end))
        return dict(record) if record is not None else None

    def _assemble(self, words: dict[int, int],
                  failures: list[dict[str, Any]]) -> dict[str, Any]:
        """Turn collected words into the ``poll()`` result."""
        result: dict[str, Any] = {}
        for reg in REGISTERS:
            got = tuple(words.get(a) for a in range(reg.address, reg.end + 1))
            if any(word is None for word in got):
                result[reg.key] = {"address": reg.span, "name": reg.name,
                                   "section": reg.section,
                                   "error": "unavailable"}
                continue
            result[reg.key] = decode(reg, tuple(got))  # type: ignore[arg-type]

        if failures:
            result["_unreadable_ranges"] = failures
        return result
