---
title: Protocol
icon: shield
order: -4
---

# Protocol

The dongle on TCP port 7078 speaks a *secure Modbus* dialect: an ordinary
Modbus RTU frame, wrapped in an AES-128-CBC envelope, wrapped again in a
Modbus-style TCP header whose first seven bytes travel in the clear.

Recovered from `com.sineng.uniplugin_module.utils.a` (`m57` / `m58`) and
`com.sineng.uniplugin_module.service.WifiTcpService` in the vendor's Android
app, and cross-checked against live traffic and the vendor's own cloud API.

---

## Wire format

```text
+--------+----------+--------+------+---------------------------------+
| tid    | protocol | length | unit | AES-128-CBC envelope            |
| 2 byte | 2 byte   | 2 byte | 1    | IV(16) | CT | HMAC-SHA256(32)   |
+--------+----------+--------+------+---------------------------------+
```

| Field | Size | Notes |
|---|---|---|
| `tid` | 2 | Transaction id, big endian. Reused to match replies to requests, and echoed by heartbeat replies. |
| `protocol` | 2 | Protocol id — see [below](#protocol-ids). |
| `length` | 2 | Counts the `unit` byte plus the envelope: `1 + len(envelope)`. |
| `unit` | 1 | Unit identifier. |
| envelope | variable | `IV(16) \| ciphertext \| HMAC-SHA256(32)`. See [Crypto](crypto.md). |

!!!warning The header is not authenticated
The 7-byte header (`tid`, `protocol`, `length`, `unit`) is sent in the
clear — it is **neither encrypted nor covered by the HMAC**. Only the
envelope is integrity-protected.
!!!

### Protocol ids

| Id | Direction | Meaning |
|---|---|---|
| `0x1000` | outgoing | Normal request. |
| `0x1001` | outgoing | Reply to a heartbeat push. |
| `0x0001` | incoming | Unsolicited push from the dongle. |

Outgoing frames are built with a plaintext protocol id of `0x0000`; the app
patches header byte 2 to `0x10` **only after** encrypting, which is why the
envelope cannot simply be computed over the finished frame. `DongleClient`
rebuilds the header by hand the same way.

---

## The inner Modbus RTU frame

Inside the encrypted payload sits an ordinary Modbus RTU frame, prefixed with a
constant:

```text
request : 64 07 00 02 00 05 | slave fc addr(2) count(2) crc16(2)
response: 64 07 00          | slave fc [bytecount data | exception] crc16(2)
```

| Part | Notes |
|---|---|
| `64 07 00 02 00 05` | Fixed 6-byte prefix. The `02 00 05` portion is a constant and does **not** encode the target. |
| `slave` | Slave address. **This** selects the target device. |
| `fc` | Function code — `0x04` for input registers, `0x03` for holding registers. |
| `addr`, `count` | Register address and count, big endian. |
| `crc16` | Vendor-specific CRC16, sent **high byte first**. See [Crypto](crypto.md#crc16). |

The CRC16 covers the inner Modbus RTU frame only — not the outer header, and not
the prefix.

!!!danger The CRC16 is not the standard Modbus CRC
It is a custom table-driven routine. Using the standard Modbus CRC16 here
will fail. See [CRC16](crypto.md#crc16).
!!!

### Target selection {#target-selection}

The `64 07 00 02 00 05` prefix is constant, so the device actually queried is
chosen by the **slave address byte** inside the inner frame:

| Slave | Target |
|---|---|
| `0x00` | The dongle itself. |
| `0x01` | The inverter behind it — the protocol document's "default Modbus address". |

This is what `--slave` and `DongleClient(slave=…)` control.

---

## The `64 07 01` stub

A reply consisting of just `64 07 01` — the three prefix bytes with a non-zero
third byte and **no frame behind them at all** — means the dongle has no data
for that address.

```python
raise DongleNoDataError(status)   # status == 0x01
```

This is a *dongle-level* refusal, distinct from a Modbus exception raised by the
*inverter*. Observed behaviour on a live 3K-HS:

| Range | Response |
|---|---|
| Past `34100` | A proper Modbus exception `0x02` (illegal data address) from the inverter. |
| `33051–33100`, `33950–33999`, `34051–34100` | The bare `64 07 01` stub from the dongle. |

See [Troubleshooting](../caveats.md#two-kinds-of-refusal).

---

## Handshake

The connect sequence mirrors the official app:

>>> 1. BeforeAccredit (`42 02`)

    42 02 00 08 | "general\0"

The dongle replies with its version, factory, model, serial number, and clock.

>>> 2. Accredit (`42 03`)

    42 03 00 08 | "general\0" | 00 00

The dongle answers with a single status byte. **Nothing else is served until
this succeeds.** A non-zero result raises `DongleError`.

>>> 3. CallAll (`42 07`) — optional

The dongle pushes its own `DeviceInfo` plus the list of attached devices as JSON
blobs.

>>> 4. FC `0x04` reads against slave `0x01`

Ordinary register reads against the inverter.

>>>

!!!info Function codes are decimal in the app's templates
The vendor app parses function codes from its JSON templates as **decimal**
(`Integer.parseInt` with no radix), so a template value of `"66"` is `0x42`
on the wire, and `"67"` is `0x43`.
!!!

---

## Heartbeats

The dongle emits unsolicited heartbeat pushes: `43 01` plus a 4-byte Unix
timestamp, with protocol id `0x0001`.

The official app answers each push with a heartbeat of its own, **reusing the
push's transaction id**, so pushes do not pile up. Both clients in this library
do the same — you never see a heartbeat, and it never confuses a reply:

- `AsyncDongleClient` has a background reader that owns the connection and
  answers the instant a push arrives, whatever the caller is doing.
- `DongleClient` answers from inside its frame iterator, so a push is only
  handled while a read is in flight.

That difference is not cosmetic. Measured against real hardware, the dongle
**closes a connection whose heartbeats go unanswered** — about five missed
pushes, or roughly three minutes — while a session that answered each push
immediately stayed up for ten minutes. The async client therefore does not care
how long the caller stays quiet.

For the blocking client the same measurement implies an interval shorter than
the heartbeat period, but does not prove it: it answers late rather than never,
and whether the dongle treats those alike has not been tested.

```python
# reply layout, built automatically
43 01 | unix_time(4)      # protocol id 0x1001, tid echoed from the push
```

---

## Error handling summary

| Condition | Exception | Level |
|---|---|---|
| Wrong serial / bad MAC | `EnvelopeError` | Envelope |
| Bad PKCS#7 padding | `EnvelopeError` | Envelope |
| Inner CRC mismatch | `ValueError` | Inner frame |
| Slave exception (`fc \| 0x80`) | `ModbusError` | Inverter |
| Bare `64 07 <status>` stub | `DongleNoDataError` | Dongle |
| No reply, refused handshake, closed socket | `DongleError` | Session |

---

## Next

- [Crypto](crypto.md) — key derivation, the envelope, and the CRC16.
- [Register Map](registers.md) — what the registers mean.
