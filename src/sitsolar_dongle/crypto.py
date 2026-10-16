"""AES-128-CBC + HMAC-SHA256 envelope used by the dongle on TCP port 7078.

Recovered from ``com.sineng.uniplugin_module.utils.a`` (``m57`` / ``m58``) and
``com.sineng.uniplugin_module.service.WifiTcpService``.

Key derivation, from the dongle serial number ``S``::

    crc        = crc16(S)                       # see sitsolar_dongle.crc
    aes_key    = MD5(S || crc.to_bytes(2, "big"))
    hmac_key   = MD5(reverse(S) || crc(reverse(S)).to_bytes(2, "big"))

Wire format of a frame::

    tid(2) | protocol(2) | length(2) | unit(1) | envelope

where ``length`` counts everything after the 6-byte Modbus-style header, and
the envelope is::

    IV(16) | ciphertext (AES-128-CBC, PKCS#7) | HMAC-SHA256(32)

The HMAC covers ``IV || ciphertext``.  Outgoing frames set the protocol id to
``0x1000`` (the app patches header byte 2 to ``0x10`` after encrypting);
unsolicited pushes from the dongle arrive with ``0x0001``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from Crypto.Cipher import AES

from .crc import crc16

__all__ = ["EnvelopeError", "Keys", "derive_keys", "seal", "open_envelope"]

_BLOCK = 16
_MAC = 32


class EnvelopeError(Exception):
    """Raised when a frame fails authentication or cannot be decrypted."""


@dataclass(frozen=True)
class Keys:
    """Key material derived from the dongle serial number."""

    serial: str
    aes: bytes
    hmac: bytes

    @property
    def crc(self) -> int:
        return crc16(self.serial.encode("ascii"))


def derive_keys(serial: str) -> Keys:
    """Derive the AES and HMAC keys from the dongle serial number."""
    sn = serial.encode("ascii")
    crc = crc16(sn)
    aes = hashlib.md5(sn + bytes([crc >> 8, crc & 0xFF])).digest()

    rev = sn[::-1]
    crc_rev = crc16(rev)
    hkey = hashlib.md5(rev + bytes([crc_rev >> 8, crc_rev & 0xFF])).digest()
    return Keys(serial=serial, aes=aes, hmac=hkey)


def seal(plaintext: bytes, keys: Keys, *, iv: bytes | None = None) -> bytes:
    """Encrypt *plaintext* into an ``IV | ciphertext | HMAC`` envelope."""
    if iv is None:
        iv = os.urandom(_BLOCK)
    if len(iv) != _BLOCK:
        raise ValueError("IV must be 16 bytes")

    padded_len = _BLOCK - (len(plaintext) % _BLOCK)
    padded = plaintext + bytes([padded_len]) * padded_len
    ct = AES.new(keys.aes, AES.MODE_CBC, iv).encrypt(padded)

    body = iv + ct
    return body + hmac.new(keys.hmac, body, hashlib.sha256).digest()


def open_envelope(envelope: bytes, keys: Keys, *, verify: bool = True) -> bytes:
    """Decrypt an envelope, checking the HMAC first.

    Mirrors ``utils.a.m57``: the HMAC is verified before decryption and any
    failure raises :class:`EnvelopeError` rather than returning garbage.
    """
    if len(envelope) < _BLOCK + _BLOCK + _MAC:
        raise EnvelopeError("envelope too short (%d bytes)" % len(envelope))

    iv = envelope[:_BLOCK]
    mac = envelope[-_MAC:]
    ct = envelope[_BLOCK:-_MAC]
    if len(ct) % _BLOCK:
        raise EnvelopeError("ciphertext is not a whole number of blocks")

    if verify:
        calc = hmac.new(keys.hmac, envelope[:-_MAC], hashlib.sha256).digest()
        if not hmac.compare_digest(calc, mac):
            raise EnvelopeError("HMAC mismatch (wrong serial number?)")

    try:
        padded = AES.new(keys.aes, AES.MODE_CBC, iv).decrypt(ct)
    except Exception as exc:  # pragma: no cover - pycryptodome internal
        raise EnvelopeError("AES decrypt failed: %s" % exc) from exc

    pad = padded[-1]
    if not 1 <= pad <= _BLOCK or padded[-pad:] != bytes([pad]) * pad:
        raise EnvelopeError("bad PKCS#7 padding (key or IV mismatch)")
    return padded[:-pad]
