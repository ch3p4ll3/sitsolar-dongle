"""Read-only client for the SiNeng / FuturaPulse WiFi dongle.

The dongle listens on TCP port 7078 and speaks the same "secure Modbus" frame
the official Sit Solar app uses: a 7-byte Modbus header followed by an
AES-128-CBC envelope carrying a nested Modbus RTU frame, authenticated with
HMAC-SHA256.  Both keys are derived from the dongle serial number, so a
session only needs that serial to authenticate.

Typical use::

    from sitsolar_dongle import DongleClient

    with DongleClient("192.168.1.50", "<DONGLE_SERIAL>") as dongle:
        for key, reading in dongle.poll().items():
            if "value" in reading:
                print(key, reading["value"], reading.get("unit", ""))

For asyncio applications such as Home Assistant, use
:class:`AsyncDongleClient` instead -- :class:`DongleClient` blocks the event
loop it is called from::

    from sitsolar_dongle import AsyncDongleClient

    async with AsyncDongleClient("192.168.1.50", "<DONGLE_SERIAL>") as dongle:
        values = await dongle.poll()

Every failure -- refused accreditation, bad HMAC, Modbus exception, dropped
connection or timeout -- is raised as :class:`DongleError`, so a single
``except`` is enough to handle all of them.

Only reads are implemented; nothing in this package ever writes to the
inverter.
"""

from __future__ import annotations

from .async_client import AsyncDongleClient
from .client import DEFAULT_PORT, Device, DongleClient, DongleInfo
from .crc import crc16, crc16_bytes
from .crypto import EnvelopeError, Keys, derive_keys, open_envelope, seal
from .decode import decode
from .errors import DongleError
from .framing import (MAX_REGISTERS, HEADER, DongleNoDataError, Frame,
                      ModbusError, build_read, parse_read_reply, wrap_payload)
from .registers import REGISTERS, Register, blocks

__version__ = "0.3.0"

__all__ = [
    "AsyncDongleClient",
    "DEFAULT_PORT",
    "Device",
    "DongleClient",
    "DongleError",
    "DongleInfo",
    "DongleNoDataError",
    "EnvelopeError",
    "Frame",
    "HEADER",
    "Keys",
    "MAX_REGISTERS",
    "ModbusError",
    "REGISTERS",
    "Register",
    "__version__",
    "blocks",
    "build_read",
    "crc16",
    "crc16_bytes",
    "decode",
    "derive_keys",
    "open_envelope",
    "parse_read_reply",
    "seal",
    "wrap_payload",
]
