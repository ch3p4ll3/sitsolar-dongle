"""Exception hierarchy for the dongle protocol.

:mod:`sitsolar_dongle.session` imports from :mod:`~sitsolar_dongle.crypto`
and :mod:`~sitsolar_dongle.framing`, so the base exception has to live
somewhere both can import without creating a cycle -- hence this leaf module
rather than alongside the client.
"""

from __future__ import annotations

__all__ = ["DongleError"]


class DongleError(Exception):
    """Any failure talking to the dongle.

    Every protocol-level exception in this package derives from it, so
    ``except DongleError`` is enough to catch a refused accreditation, a bad
    HMAC, a Modbus exception, a dropped connection or a timeout.
    """
