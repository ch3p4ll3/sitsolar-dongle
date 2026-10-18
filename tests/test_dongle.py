"""Tests for the vendor CRC16 and the AES/HMAC envelope.

Run with ``pytest``.
"""

from __future__ import annotations

import hashlib
import hmac
import sys

from Crypto.Cipher import AES

from sitsolar_dongle import (
    HEADER,
    DongleNoDataError,
    EnvelopeError,
    ModbusError,
    build_read,
    crc16,
    crc16_bytes,
    decode,
    derive_keys,
    open_envelope,
    parse_read_reply,
    seal,
    wrap_payload,
)
from sitsolar_dongle.registers import REGISTERS, U32, blocks

# Keys and vectors taken from a real Si-Dongle-L, serial 300018101261010226.
#
# The serial is deliberately kept rather than scrubbed to a placeholder: the
# value of EXPECTED_AES / EXPECTED_HMAC is that the *device* produced them, so
# a synthetic serial would turn the derivation check into a tautology. Where
# the serial is mere example text it uses <DONGLE_SERIAL> instead.
SERIAL = "300018101261010226"
EXPECTED_AES = bytes.fromhex("d071827be8017016b83df98ee6b34e2f")
EXPECTED_HMAC = bytes.fromhex("0af59496ba8946d1d83d7b23e1ce7548")


def test_crc16_vectors() -> None:
    assert crc16(b"") == 0xFFFF
    assert crc16(b"\x00") == 0xBF40
    assert crc16(b"123456789") == 0x374B
    assert crc16(b"Modbus") == 0x0254
    assert crc16(SERIAL.encode()) == 0x6E41
    assert crc16(SERIAL.encode()[::-1]) == 0x5A73


def test_crc16_bytes_are_big_endian() -> None:
    assert crc16_bytes(SERIAL.encode()) == b"\x6e\x41"
    # CRC covers the inner frame only, appended high byte first.  The body is
    # the real captured FC03 reply (crc.selftest: crc16 = 0x7651); its last
    # two bytes before the CRC are register data, not part of the checksum.
    inner = bytes.fromhex("00030a000100006ad26e1f00017651")
    assert crc16_bytes(inner[:-2]) == inner[-2:]


def test_table_a_row_224() -> None:
    """The row that silently broke the AES key the first time around."""
    from sitsolar_dongle.crc import _TABLE_A, _TABLE_B

    assert len(_TABLE_A) == len(_TABLE_B) == 256
    assert _TABLE_A[224:240] == [
        1, 192, 128, 65, 0, 193, 129, 64,
        0, 193, 129, 64, 1, 192, 128, 65,
    ]


def test_key_derivation() -> None:
    keys = derive_keys(SERIAL)
    assert keys.aes == EXPECTED_AES
    assert keys.hmac == EXPECTED_HMAC
    assert keys.aes == hashlib.md5(
        SERIAL.encode() + bytes.fromhex("6e41")).digest()
    assert keys.hmac == hashlib.md5(
        SERIAL.encode()[::-1] + bytes.fromhex("5a73")).digest()


def test_seal_then_open_roundtrip() -> None:
    keys = derive_keys(SERIAL)
    for payload in (b"", b"A", b"x" * 16, b"y" * 17, b"z" * 1000):
        envelope = seal(payload, keys)
        assert len(envelope) % 16 == 0
        assert open_envelope(envelope, keys) == payload


def test_open_detects_tampering() -> None:
    keys = derive_keys(SERIAL)
    envelope = bytearray(seal(b"hello world", keys))
    envelope[20] ^= 0x01
    try:
        open_envelope(bytes(envelope), keys)
    except EnvelopeError:
        return
    raise AssertionError("tampered envelope was accepted")


def test_open_detects_wrong_serial() -> None:
    envelope = seal(b"hello world", derive_keys(SERIAL))
    try:
        open_envelope(envelope, derive_keys("300018101261010227"))
    except EnvelopeError:
        return
    raise AssertionError("envelope opened with the wrong serial")


def test_envelope_matches_app_layout() -> None:
    """IV(16) | ciphertext | HMAC(32), HMAC over IV|ciphertext."""
    keys = derive_keys(SERIAL)
    payload = bytes.fromhex("4207")
    plain = wrap_payload(payload, tid=2)
    envelope = seal(payload, keys, iv=b"\x11" * 16)

    assert envelope[:16] == b"\x11" * 16
    assert envelope[-32:] == hmac.new(keys.hmac, envelope[:-32],
                                      hashlib.sha256).digest()
    ct = envelope[16:-32]
    assert len(ct) == 16  # one padded block for a 2-byte payload
    padded = AES.new(keys.aes, AES.MODE_CBC, b"\x11" * 16).decrypt(ct)
    assert padded.endswith(b"\x0e" * 14)  # PKCS#7, block - len(payload)
    assert len(plain) == 9  # 7-byte header + 2-byte payload
    assert plain[:7] == bytes.fromhex("00020000000300")


def test_build_read_inner_frame() -> None:
    """The inner frame: 6-byte wrapper + slave + FC + addr + count + CRC."""
    payload = build_read(1, 31002, 2, fc=4)
    assert payload[:6] == HEADER
    assert payload[6] == 0x01          # slave = inverter
    assert payload[7] == 0x04          # function code, input registers
    assert payload[8:10] == (31002).to_bytes(2, "big")
    assert payload[10:12] == (2).to_bytes(2, "big")
    assert len(payload) == 14
    assert crc16_bytes(payload[6:-2]) == payload[-2:]

    payload = build_read(0, 1, 17, fc=3)
    assert payload[6] == 0x00          # slave = dongle itself
    assert payload[7] == 0x03


def test_build_read_rejects_bad_counts() -> None:
    for count in (0, 51, -1):
        try:
            build_read(1, 31002, count)
        except ValueError:
            continue
        raise AssertionError("count %d was accepted" % count)


def test_parse_read_reply() -> None:
    """Real FC03 reply: bytecount 0x0a = 10 bytes = 5 RunStatus registers."""
    data = bytes.fromhex("000100006ad26e1f0001")
    body = bytes([0x00, 0x03, 10]) + data
    payload = bytes([0x64, 0x07, 0x00]) + body + crc16_bytes(body)

    reply = parse_read_reply(payload)
    assert reply.slave == 0
    assert reply.fc == 0x03
    assert len(reply.registers) == 5    # 10 data bytes, standard Modbus
    assert reply.registers[0] == 1      # 4G status = 1


def test_parse_read_reply_raises_on_exception() -> None:
    body = bytes([0x01, 0x84, 0x02])    # slave 1, FC04|0x80, illegal address
    payload = bytes([0x64, 0x07, 0x00]) + body + crc16_bytes(body)
    try:
        parse_read_reply(payload)
    except ModbusError as exc:
        if "illegal data address" not in str(exc):
            raise AssertionError("wrong error text: %s" % exc)
        return
    raise AssertionError("exception frame did not raise")


def test_parse_read_reply_rejects_bad_crc() -> None:
    body = bytes([0x01, 0x04, 0x02, 0xC3, 0x54])
    payload = bytes([0x64, 0x07, 0x00]) + body + b"\x00\x00"
    try:
        parse_read_reply(payload)
    except ValueError:
        return
    raise AssertionError("bad inner CRC was accepted")


def test_register_map_is_consistent() -> None:
    keys = [r.key for r in REGISTERS]
    assert len(keys) == len(set(keys)), "duplicate register keys"
    for reg in REGISTERS:
        if reg.dtype in (U32,):
            assert reg.length == 2, reg.key
        if reg.dtype == "S32":
            assert reg.length == 2, reg.key
        assert 0 <= reg.address <= 0xFFFF

    # Widening an entry to U32 must never swallow its neighbour.
    spans = sorted((r.address, r.end, r.key) for r in REGISTERS)
    for (a1, e1, k1), (a2, e2, _) in zip(spans, spans[1:]):
        assert e1 < a2, "%s (%d-%d) overlaps %d" % (k1, a1, e1, a2)


def test_blocks_cover_the_map_and_fit_in_one_frame() -> None:
    wanted = set()
    for reg in REGISTERS:
        wanted.update(range(reg.address, reg.end + 1))

    seen = set()
    for start, end in blocks(50):
        count = end - start + 1
        assert 1 <= count <= 50
        build_read(1, start, count, fc=4)          # raises if out of range
        for addr in range(start, end + 1):
            assert addr not in seen, "address %d read twice" % addr
            seen.add(addr)
    assert seen == wanted


def test_decode_scaling() -> None:
    grid = next(r for r in REGISTERS if r.key == "grid_frequency")
    # 31002~31003 is a big-endian U32: the low address holds the high word.
    # A 50.020 Hz grid reads back as (hi=0, lo=50020).
    out = decode(grid, (0, 50020))
    assert out["value"] == 50.02
    assert out["unit"] == "Hz"
    assert out["address"] == "31002~31003"

    signed = next(r for r in REGISTERS if r.key == "battery_current")
    out = decode(signed, (0xFFCE,))
    assert out["raw"] == 0xFFCE
    assert out["raw_signed"] == -50
    assert out["value"] == -0.5

    neg32 = next(r for r in REGISTERS if r.key == "inverter_p")
    out = decode(neg32, (0xFFFF, 0xFE18))     # -488 W
    assert out["raw_signed"] == -488
    assert out["value"] == -488


def test_decode_ascii() -> None:
    reg = next(r for r in REGISTERS if r.key == "serial_number")
    words = tuple(int.from_bytes(raw, "big")
                  for raw in [SERIAL[i:i + 2].encode() for i in range(0, 20, 2)])
    assert decode(reg, words)["value"] == SERIAL


def test_decode_enum_and_bits() -> None:
    status = next(r for r in REGISTERS if r.key == "operation_status")
    assert decode(status, (2,))["label"] == "normal"
    assert decode(status, (9,))["label"] == "unknown (9)"

    flags = next(r for r in REGISTERS if r.key == "dc_power_flags")
    out = decode(flags, (0x11,))
    assert out["active_bits"] == [
        "master DSP updating slave DSP", "DC power off",
    ]
    assert decode(flags, (0,))["active_bits"] == []


def test_lifetime_totals_span_two_registers() -> None:
    """Regression: 16 lifetime counters were declared as plain U16.

    Each read only its own address -- the high word, which is always 0 -- so
    every lifetime total reported zero while the data sat in the low word one
    register over. Live values below come from 300018101261010226 and match
    the vendor cloud's figures exactly.
    """
    u32_addresses = [
        33040, 33042,                       # meter energy totals
        34001, 34003, 34005, 34007, 34009,  # generation info
        34011, 34013, 34015, 34019,
        34034, 34036, 34038, 34040, 34042,
    ]
    by_addr = {r.address: r for r in REGISTERS}
    for addr in u32_addresses:
        reg = by_addr[addr]
        assert reg.dtype == U32, "%d %s is %s" % (addr, reg.key, reg.dtype)
        assert reg.length == 2, "%d %s" % (addr, reg.key)

    # Big-endian: address+1 holds the low word, so (0, 6002) is 600.2 and a
    # byte-swapped reading would be 393,375,744.
    assert decode(by_addr[34007], (0, 6002))["value"] == 600.2   # all_charge
    assert decode(by_addr[34038], (0, 745))["value"] == 74.5     # pv -> grid
    assert decode(by_addr[33040], (0, 4465))["value"] == 446.5   # totalOngridPower
    assert decode(by_addr[34015], (0, 2292))["value"] == 2292    # generation hours


def test_parse_read_reply_dongle_no_data_stub() -> None:
    """Outside its own table the dongle answers a bare ``64 07 01``.

    No Modbus frame follows it at all, so this must not be confused with a
    truncated reply or with a Modbus exception from the inverter.
    """
    try:
        parse_read_reply(bytes.fromhex("640701"))
    except DongleNoDataError as exc:
        assert exc.status == 1
        assert "no data" in str(exc)
    else:
        raise AssertionError("640701 stub was not recognised")

    # Status 0 followed by a real frame is still the success path.
    body = bytes([0x01, 0x04, 0x02, 0xC3, 0x54])
    ok = bytes([0x64, 0x07, 0x00]) + body + crc16_bytes(body)
    assert parse_read_reply(ok).registers == (0xC354,)

    # Unrelated garbage still raises ValueError, not DongleNoDataError.
    try:
        parse_read_reply(b"\x01\x02\x03\x04\x05\x06")
    except ValueError:
        pass
    else:
        raise AssertionError("garbage was accepted")


def test_warning_registers() -> None:
    """30211 carries a decimal code, not a bit mask -- see Chart 11."""
    code = next(r for r in REGISTERS if r.key == "dsp_warning5")
    assert decode(code, (200,))["label"] == "no warning"
    assert decode(code, (241,))["label"] == "BuckBoost hardware over current"
    assert decode(code, (999,))["label"] == "unknown (999)"

    # 30207~30210 are the ordinary bit fields (Charts 8-10).
    bits = next(r for r in REGISTERS if r.key == "dsp_warning1")
    out = decode(bits, (1 << 3,))                  # BIT03 = LVRT
    assert out["value"] == 8
    assert out["active_bits"] == ["LVRT"]
    assert decode(bits, (0,))["active_bits"] == []


def test_power_registers_do_not_claim_to_agree() -> None:
    """31039~31040 tracks P (within 1-2 W), so it is not the apparent power
    the spec calls it, and 31022 is a near-constant setpoint. Both caveats
    have to stay in the map rather than be silently trusted."""
    s = next(r for r in REGISTERS if r.key == "apparent_power")
    inv_p = next(r for r in REGISTERS if r.key == "inverter_p")
    assert s.dtype == U32 and s.length == 2
    assert s.address != inv_p.address          # they are separate registers
    assert "sqrt" in s.notes                   # the usable definition of S
    pf = next(r for r in REGISTERS if r.key == "power_factor")
    assert "setpoint" in pf.notes              # why 0.999 must not be trusted


if __name__ == "__main__":  # pragma: no cover
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL %s: %s" % (name, exc))
            else:
                print("ok   %s" % name)
    sys.exit(1 if failures else 0)
