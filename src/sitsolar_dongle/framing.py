"""Framing: Modbus-style header, secure envelope wrapping, inner RTU frames.

Two layers are in play at once:

* the **outer** TCP framing spoken with the dongle on port 7078::

      tid(2) | protocol(2) | length(2) | unit(1) | secure envelope

* the **inner** Modbus RTU frame carried inside the encrypted payload.  A
  register read is issued as::

      64 07 00 02 00 05 | slave(1) 03 addr(2) count(2) crc16(2)

  and answered as::

      64 07 00 | slave(1) fc(1) bytecount(1) data(N) crc16(2)

  The trailing CRC16 is computed over the inner Modbus RTU frame only, and is
  sent high byte first.  ``0x64 0x07 0x02`` is a fixed constant: the register
  target is selected by the *slave address* byte inside the inner frame
  (``0x00`` = the dongle itself, ``0x01`` = the inverter behind it).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .crc import crc16_bytes

__all__ = [
    "HEADER",
    "MAX_REGISTERS",
    "DongleNoDataError",
    "Frame",
    "ModbusError",
    "ReadReply",
    "build_read",
    "frame_length",
    "parse_header",
    "parse_read_reply",
    "wrap_payload",
]

HEADER = bytes([0x64, 0x07, 0x00, 0x02, 0x00, 0x05])

# The vendor recommends frames of at most 100 bytes == 50 registers.
MAX_REGISTERS = 50

PROTO_REQUEST = 0x1000
PROTO_PUSH = 0x0001

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04


class ModbusError(Exception):
    """The slave answered with a Modbus exception."""

    def __init__(self, code: int, slave: int, fc: int) -> None:
        self.code = code
        self.slave = slave
        self.fc = fc
        super().__init__("slave %d: %s (exception 0x%02X, FC 0x%02X)"
                         % (slave, ERROR_NAMES.get(code, "unknown error"), code, fc))


class DongleNoDataError(Exception):
    """The dongle answered a bare ``64 07 <status>`` stub instead of a frame.

    The success prefix is ``64 07 00`` followed by the Modbus reply; when the
    dongle has nothing to return for a range it sends just those three bytes
    with a non-zero status and no Modbus frame behind it at all. Observed for
    33051-33100, 33950-33999 and 34051-34100.

    This is a *dongle*-level refusal and is distinct from
    :class:`ModbusError`, which is the *inverter* rejecting the request -- e.g.
    34101+ answers a proper Modbus exception 0x02 (illegal data address) rather
    than a stub.
    """

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__("dongle returned no data (stub 6407%02X: address "
                         "outside its table)" % status)


ERROR_NAMES = {
    0x01: "illegal function",
    0x02: "illegal data address",
    0x03: "illegal data value",
    0x04: "slave device failure",
    0x05: "acknowledge",
    0x06: "slave device busy",
    0x0A: "gateway path unavailable",
    0x0B: "gateway target device failed to respond",
}


@dataclass(frozen=True)
class Frame:
    """One outer frame, still encrypted."""

    tid: int
    protocol: int
    unit: int
    envelope: bytes

    @property
    def is_push(self) -> bool:
        """Unsolicited dongle pushes use protocol id 0x0001."""
        return (self.protocol & 0xFF) == (PROTO_PUSH & 0xFF)


@dataclass(frozen=True)
class ReadReply:
    """Decoded inner Modbus reply."""

    slave: int
    fc: int
    registers: tuple[int, ...]


def wrap_payload(payload: bytes, tid: int, protocol: int = 0) -> bytes:
    """Build the plain (pre-encryption) outer frame around *payload*.

    The plaintext protocol id is ``0x0000``; the official app patches header
    byte 2 to ``0x10`` (making ``0x1000``) only *after* encrypting, which is
    what :meth:`DongleClient._seal_frame` does too.
    """
    body = payload
    return struct.pack(">HHHB", tid, protocol, len(body) + 1, 0) + body


def parse_header(header: bytes) -> tuple[int, int, int, int]:
    """Split a 7-byte header into ``tid, protocol, length, unit``."""
    if len(header) < 7:
        raise ValueError("header must be 7 bytes")
    tid, protocol, length, unit = struct.unpack(">HHHB", header[:7])
    return tid, protocol, length, unit


def frame_length(header: bytes) -> int:
    """Total size of the frame whose 7-byte *header* we already have."""
    if len(header) < 6:
        raise ValueError("need at least 6 header bytes")
    length = struct.unpack(">H", header[4:6])[0]
    return 6 + length


def build_read(slave: int, address: int, count: int,
               fc: int = FC_READ_INPUT) -> bytes:
    """Build the inner payload of a register read.

    *fc* ``0x04`` reads input registers (the 3xxxx block, per the FuturaPulse
    protocol document); ``0x03`` reads holding registers (4xxxx).
    """
    if not 1 <= count <= MAX_REGISTERS:
        raise ValueError("count must be 1..%d registers" % MAX_REGISTERS)
    if not 0 <= address <= 0xFFFF:
        raise ValueError("address must fit in 16 bits")

    inner = bytes([slave & 0xFF, fc]) + struct.pack(">HH", address, count)
    return HEADER + inner + crc16_bytes(inner)


def parse_read_reply(payload: bytes) -> ReadReply:
    """Decode the decrypted payload of a read reply.

    Accepts both the success form (``fc bytecount data``) and the exception
    form (``fc|0x80 code``); the latter raises :class:`ModbusError`. The bare
    ``64 07 <status>`` stub raises :class:`DongleNoDataError`.
    """
    if (len(payload) >= 3 and payload[:2] == HEADER[:2]
            and payload[2] != HEADER[2]):
        # `64 07 <status>` with nothing behind it: the dongle refused the range.
        raise DongleNoDataError(payload[2])

    if len(payload) < 6 or payload[:3] != HEADER[:3]:
        raise ValueError("not a read reply: %r" % payload[:6].hex())

    body = payload[3:]
    if len(body) < 5:
        raise ValueError("truncated Modbus reply")

    slave, fc = body[0], body[1]
    expected = crc16_bytes(body[:-2])
    if expected != body[-2:]:
        raise ValueError("inner CRC mismatch (expected %s, got %s)"
                         % (expected.hex(), body[-2:].hex()))

    if fc & 0x80:
        raise ModbusError(body[2], slave, fc & 0x7F)

    bytecount = body[2]
    data = body[3:3 + bytecount]
    if len(data) != bytecount:
        raise ValueError("truncated data: wanted %d bytes, got %d"
                         % (bytecount, len(data)))

    registers = tuple(
        (data[i] << 8) | data[i + 1] for i in range(0, len(data) - 1, 2)
    )
    return ReadReply(slave=slave, fc=fc, registers=registers)
