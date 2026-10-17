---
title: Crypto & Framing
icon: key
order: -5
---

# Crypto & Framing

The secure envelope, the key derivation, and the CRC16 that holds it together.
Recovered from `com.sineng.uniplugin_module.utils.a` (`m57` / `m58`) and
`com.sineng.uniplugin_module.service.WifiTcpService`.

---

## Key derivation

Both keys derive from the **dongle serial number** alone — there is no
negotiation, no challenge, and no other secret:

```text
crc        = crc16(S)                       # see CRC16 below
aes_key    = MD5(S || crc.to_bytes(2, "big"))
hmac_key   = MD5(reverse(S) || crc(reverse(S)).to_bytes(2, "big"))
```

In Python, via `sitsolar_dongle.crypto.derive_keys`:

```python
from sitsolar_dongle import derive_keys

keys = derive_keys("300018101261010226")
keys.aes     # 16 bytes
keys.hmac    # 16 bytes
keys.crc     # CRC16 of the serial, for cross-checking
```

!!!danger The serial number is the credential
Because both keys are a pure function of the serial, **anyone on your
network who knows that serial can open an authenticated session with your
dongle**. Keep it private: do not commit it, do not paste it into issues,
and redact it from logs and packet captures before sharing them.

Note the asymmetry: the AES key uses the serial as-is, the HMAC key uses the
**reversed** serial. That is why a bug in one CRC table row can corrupt one
key and not the other — see [CRC16](#crc16).
!!!
---

## Envelope

The payload portion of every frame is:

```text
IV(16) | ciphertext (AES-128-CBC, PKCS#7) | HMAC-SHA256(32)
```

| Part | Size | Notes |
|---|---|---|
| `IV` | 16 | Random per frame (`os.urandom`). |
| ciphertext | multiple of 16 | AES-128-CBC over the PKCS#7-padded payload. |
| `HMAC-SHA256` | 32 | Computed over `IV \|\| ciphertext`. |

### Verification order

`open_envelope()` verifies the HMAC **before** decrypting, exactly as the
vendor's `utils.a.m57` does:

1. Recompute `HMAC-SHA256(IV || ciphertext)` with `hmac_key`.
2. Compare in constant time against the trailing 32 bytes.
3. Only on a match, AES-decrypt and strip PKCS#7 padding.

Any failure raises `EnvelopeError` rather than returning garbage, so a wrong
serial surfaces as the clean message:

```text
EnvelopeError: HMAC mismatch (wrong serial number?)
```

```python
from sitsolar_dongle import seal, open_envelope, derive_keys, EnvelopeError

keys = derive_keys("<DONGLE_SERIAL>")
envelope = seal(b"\x42\x02\x00\x08general\x00", keys)

try:
    payload = open_envelope(envelope, keys)
except EnvelopeError as exc:
    print("authentication failed:", exc)
```

Pass `verify=False` to `open_envelope` to skip the MAC check — useful only when
debugging captured traffic with a known-good key.

### Padding

PKCS#7, block size 16. A padding value outside `1..16`, or bytes that do not
match it, raises `EnvelopeError("bad PKCS#7 padding (key or IV mismatch)")`. That
message is a strong hint that the **AES** key is wrong while the **HMAC** key is
right — see the CRC16 warning below.

---

## CRC16

Used in exactly two places:

* appended to the inner Modbus RTU frame, **big-endian** (high byte first);
* mixed into the AES / HMAC key derivation together with the serial.

```python
from sitsolar_dongle import crc16, crc16_bytes

crc16(b"300018101261010226")        # int
crc16_bytes(b"\x01\x04\x75\x94")    # 2 bytes, big-endian, ready to append
```

!!!danger This is not the standard Modbus CRC16
It is a custom table-driven routine, ported from
`com.sineng.uniplugin_module.utils.i.m69` and its lookup tables
`com.sineng.uniplugin_module.a.a.f1228a` / `f1229b`. Feeding it the standard
Modbus CRC16 tables will produce frames the dongle rejects.
!!!

### Why the tables are delicate

Table A holds only the values `0`, `1`, `64`, `65`, `128`, `129`, `192`, and
`193`, and its four 64-entry quarters are **not** all identical — entries
`224..239` in particular break the pattern of the preceding quarter.

The consequence of getting this wrong is subtle and worth spelling out:

> A single wrong row silently corrupts the **AES** key, while the **HMAC** key —
> derived from the *reversed* serial — keeps working.

That produces the very confusing symptom where **every frame authenticates but
nothing decrypts**: the MAC check passes, then AES yields garbage, and PKCS#7
padding validation fails.

### Guard rails

`sitsolar_dongle.crc.selftest()` verifies the tables against known vectors. The
module **refuses to start** unless it passes, and the test suite
([Development](../development.md#the-test-suite)) pins the tables against
vectors taken from the APK and from a live dongle.

---

## Frame construction

`DongleClient` seals a payload in two steps, because the outer header must
survive in the clear while the protocol id is patched afterwards:

1. `wrap_payload()` builds the plaintext frame: `tid | protocol | length | unit | payload`.
2. The 7-byte header is split off; the remaining payload is sealed.
3. The header is re-prefixed to the envelope, then the **protocol id is written
   into bytes 2-3** (`0x1000` for requests, `0x1001` for heartbeat replies) and
   the length field is recomputed as `len(envelope) + 1`.

The official app reaches the same bytes by leaving the protocol id at `0x0000`
and patching byte 2 to `0x10` only *after* encrypting. Writing both bytes up
front is equivalent on the wire, and it is what lets requests and heartbeat
replies share one function.

```python
# from sitsolar_dongle.session.seal_frame
plain = wrap_payload(payload, tid, protocol)
header, body = plain[:7], plain[7:]
envelope = seal(body, keys)
out = bytearray(header + envelope)
out[2] = (protocol >> 8) & 0xFF
out[3] = protocol & 0xFF
out[4] = (len(envelope) + 1) >> 8
out[5] = (len(envelope) + 1) & 0xFF
```

Heartbeat replies do the same, then set the protocol low byte to `0x01`
(result `0x1001`), per `WifiTcpService.m50`.

---

## Next

- [Protocol overview](index.md) — the full wire format and handshake.
- [Register Map](registers.md) — what the reads return.
