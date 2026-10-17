"""CRC16 used by the SiNeng dongle protocol.

This is *not* the standard Modbus CRC16.  It is a custom table-driven routine,
port of ``com.sineng.uniplugin_module.utils.i.m69`` and its lookup tables
``com.sineng.uniplugin_module.a.a.f1228a`` / ``f1229b``.

It is used in two places:

* appended to the inner Modbus RTU frame, **big-endian** (high byte first);
* mixed into the AES / HMAC key derivation together with the dongle serial.

CAUTION
-------
Table A holds only 0/1/64/65/128/129/192/193 and the four 64-entry quarters
are *not* all identical: entries 224..239 in particular break the pattern of
the preceding quarter.  A single wrong row silently corrupts the AES key while
the HMAC key (derived from the *reversed* serial) keeps working, which shows
up as the very confusing "every frame authenticates but nothing decrypts".
The module refuses to start unless :func:`selftest` passes.
"""

from __future__ import annotations

__all__ = ["crc16", "crc16_bytes", "selftest"]

# com.sineng.uniplugin_module.a.a.f1228a
_TABLE_A = [
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      1, 192, 128,  65,   0, 193, 129,  64,   0, 193, 129,  64,   1, 192, 128,  65,
      0, 193, 129,  64,   1, 192, 128,  65,   1, 192, 128,  65,   0, 193, 129,  64,
]

_TABLE_B = [
      0, 192, 193,   1, 195,   3,   2, 194, 198,   6,   7, 199,   5, 197, 196,   4,
    204,  12,  13, 205,  15, 207, 206,  14,  10, 202, 203,  11, 201,   9,   8, 200,
    216,  24,  25, 217,  27, 219, 218,  26,  30, 222, 223,  31, 221,  29,  28, 220,
     20, 212, 213,  21, 215,  23,  22, 214, 210,  18,  19, 211,  17, 209, 208,  16,
    240,  48,  49, 241,  51, 243, 242,  50,  54, 246, 247,  55, 245,  53,  52, 244,
     60, 252, 253,  61, 255,  63,  62, 254, 250,  58,  59, 251,  57, 249, 248,  56,
     40, 232, 233,  41, 235,  43,  42, 234, 238,  46,  47, 239,  45, 237, 236,  44,
    228,  36,  37, 229,  39, 231, 230,  38,  34, 226, 227,  35, 225,  33,  32, 224,
    160,  96,  97, 161,  99, 163, 162,  98, 102, 166, 167, 103, 165, 101, 100, 164,
    108, 172, 173, 109, 175, 111, 110, 174, 170, 106, 107, 171, 105, 169, 168, 104,
    120, 184, 185, 121, 187, 123, 122, 186, 190, 126, 127, 191, 125, 189, 188, 124,
    180, 116, 117, 181, 119, 183, 182, 118, 114, 178, 179, 115, 177, 113, 112, 176,
     80, 144, 145,  81, 147,  83,  82, 146, 150,  86,  87, 151,  85, 149, 148,  84,
    156,  92,  93, 157,  95, 159, 158,  94,  90, 154, 155,  91, 153,  89,  88, 152,
    136,  72,  73, 137,  75, 139, 138,  74,  78, 142, 143,  79, 141,  77,  76, 140,
     68, 132, 133,  69, 135,  71,  70, 134, 130,  66,  67, 131,  65, 129, 128,  64,
]


def crc16(data: bytes) -> int:
    """Return the vendor CRC16 of *data* as an integer."""
    hi = 0xFF
    lo = 0xFF
    for byte in data:
        idx = (hi ^ byte) & 0xFF
        new_hi = (lo ^ _TABLE_A[idx]) & 0xFF
        new_lo = _TABLE_B[idx] & 0xFF
        hi, lo = new_hi, new_lo
    return (hi << 8) | lo


def crc16_bytes(data: bytes) -> bytes:
    """Return the CRC16 in its on-wire order (high byte first)."""
    value = crc16(data)
    return bytes([value >> 8, value & 0xFF])


def selftest() -> None:
    """Verify the tables against vectors taken from the APK and a live dongle."""
    if len(_TABLE_A) != 256 or len(_TABLE_B) != 256:
        raise AssertionError("CRC tables must have 256 entries")
    # entries 224..239 look like the preceding quarter but are not
    if _TABLE_A[224:240] != [1, 192, 128, 65, 0, 193, 129, 64,
                            0, 193, 129, 64, 1, 192, 128, 65]:
        raise AssertionError("CRC table A[224:240] has been corrupted")
    vectors = {
        b"": 0xFFFF,
        b"\x00": 0xBF40,
        b"123456789": 0x374B,
        b"Modbus": 0x0254,
        # A real Si-Dongle-L serial.  Kept so this vector stays grounded in a
        # device rather than being self-computed -- see tests/test_dongle.py.
        b"300018101261010226": 0x6E41,
        # inner part of a real FC03 reply captured from a Si-Dongle-L
        bytes.fromhex("00030a000100006ad26e1f0001"): 0x7651,
    }
    for data, want in vectors.items():
        got = crc16(data)
        if got != want:
            raise AssertionError(
                "crc16(%r) = 0x%04X, expected 0x%04X" % (data, got, want)
            )


selftest()
