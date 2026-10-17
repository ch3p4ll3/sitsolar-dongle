---
title: Python API
icon: code
order: -3
---

# Python API

The CLI is a thin wrapper. Everything it does is available as a library, from a
single import:

```python
from sitsolar_dongle import DongleClient
```

Only reads are implemented; nothing in this package ever writes to the
inverter.

---

## `DongleClient`

A blocking, read-only session with one dongle.

```python
DongleClient(
    host: str,
    serial: str,
    *,
    port: int = 7078,
    slave: int = 1,
    timeout: float = 10.0,
    interframe: float = 0.05,
)
```

| Parameter | Default | Description |
|---|---|---|
| `host` | — | Dongle IP address or hostname. |
| `serial` | — | **Dongle** serial number; the AES and HMAC keys are derived from it in the constructor. |
| `port` | `7078` | TCP port. |
| `slave` | `1` | Default Modbus slave for reads. `0` is the dongle, `1` is the inverter. |
| `timeout` | `10.0` | Socket timeout, also used as the reply deadline. |
| `interframe` | `0.05` | Seconds to sleep between frames of a multi-frame reply. Set `0` to disable. |

Key derivation happens in `__init__` and is cheap; the socket is **not** opened
until `connect()` or the first use as a context manager.

### Context manager

```python
from sitsolar_dongle import DongleClient

with DongleClient("192.168.1.50", "<DONGLE_SERIAL>") as dongle:
    print(dongle.info)
    print(dongle.read(31002, 2))
```

`__enter__` calls `connect()`; `__exit__` calls `close()`.

### Attributes

| Attribute | Type | Description |
|---|---|---|
| `host` | `str` | Target host. |
| `port` | `int` | Target port. |
| `slave` | `int` | Default slave address for reads. |
| `timeout` | `float` | Socket/reply timeout. |
| `keys` | `Keys` | Derived key material — see [`Keys`](#keys). |
| `info` | `DongleInfo | None` | Populated by `connect()` / `handshake()`. |

### Lifecycle

#### `connect() -> DongleInfo`

Open the socket and run the accreditation handshake. Returns the dongle
identity and stores it on `self.info`.

#### `close() -> None`

Close the socket. Safe to call more than once.

#### `handshake() -> DongleInfo`

`BeforeAccredit` followed by `Accredit`. Returns the dongle identity. Called
automatically by `connect()`; call it directly only if you opened the socket
yourself.

#### `call_all() -> list[dict]`

Send `CallAll` and return the JSON blobs the dongle pushes back — its own
device info plus the list of attached devices.

#### `devices() -> list[Device]`

Parsed view of the `CallAll` reply, as [`Device`](#device) objects.

### Reading

#### `read(address, count, *, slave=None, fc=0x04) -> tuple[int, ...]`

Read `count` registers starting at `address` and return the raw 16-bit words,
big endian. `count` must be `1..50`; `slave` falls back to `self.slave`; `fc`
is `0x04` for input registers or `0x03` for holding registers.

```python
hz_words = dongle.read(31002, 2)          # (0, 50010)
hz = ((hz_words[0] << 16) | hz_words[1]) * 0.001   # 50.010
```

!!!info 32-bit values are big-endian pairs
The **low** address holds the *high* word. `(0, 50010)` at `31002~31003` is
50.010 Hz. Reading only the low word of a lifetime counter yields `0`, which
is exactly the bug that once made every lifetime total read as zero — see
[Register Map](protocol/registers.md#word-order).
!!!

#### `poll(*, slave=None, fc=0x04, on_error="skip") -> dict[str, Any]`

Read **every** documented read-only register and decode it, returning a dict
keyed by register key.

| Parameter | Default | Description |
|---|---|---|
| `slave` | `self.slave` | Slave address to poll. |
| `fc` | `0x04` | Function code for the reads. |
| `on_error` | `"skip"` | `"skip"` records refused ranges under `_unreadable_ranges`; `"raise"` re-raises the first failure. |

Registers whose backing words were not returned get
`{"address", "name", "section", "error": "unavailable"}` instead of a `value`.

```python
for key, reading in dongle.poll().items():
    if "value" in reading:
        print(f"{key:34s} {reading['value']} {reading.get('unit', '')}")
```

Each reading carries `address`, `name`, `section`, `raw`, and — depending on the
data type — `value`, `unit`, `label`, `active_bits`, and `raw_signed`. The
full field list is in the [CLI reference](cli.md#json-shape), since the shape is
identical.

---

## Data types

### `DongleInfo`

Reply to `BeforeAccredit`. Frozen dataclass.

| Field | Type | Description |
|---|---|---|
| `version` | `int` | Firmware version word. |
| `factory` | `str` | Manufacturer, e.g. `SiNeng`. |
| `model` | `str` | Model, e.g. `Si-Dongle-L`. |
| `serial` | `str` | Dongle serial number. |
| `time` | `int` | Dongle clock as a Unix timestamp. |

### `Device`

One entry of the `CallAll` attached-device list. Frozen dataclass with
`factory`, `model`, `serial`, `protocol_identifier`, and `device_type`.

#### `Device.modbus_id -> int`

Modbus slave address of the attached device. The dongle owns address `0`;
devices behind it start at `1`, matching the protocol document's default.

### `Keys`

Frozen dataclass of derived key material, produced by `derive_keys(serial)`.

| Field | Type | Description |
|---|---|---|
| `serial` | `str` | The source serial. |
| `aes` | `bytes` | 16-byte AES-128 key. |
| `hmac` | `bytes` | 16-byte HMAC key (used to derive a SHA-256 HMAC). |

#### `Keys.crc -> int`

The CRC16 of the serial, exposed for cross-checking.

---

## Exceptions

All are exported from the package root.

| Exception | Raised when |
|---|---|
| `DongleError` | Protocol-level failure — not connected, no reply, refused `Accredit`, truncated frame, connection closed. |
| `ModbusError` | The slave answered with a Modbus exception. Carries `code`, `slave`, and `fc`. |
| `DongleNoDataError` | The dongle answered a bare `64 07 <status>` stub instead of a frame. Carries `status`. |
| `EnvelopeError` | A frame failed authentication or could not be decrypted. |
| `ValueError` | Invalid arguments, e.g. `count` outside `1..50`. |

!!!warning `ModbusError` and `DongleNoDataError` are different failures
A `DongleNoDataError` is a **dongle-level** refusal: it has no data for that
address. A `ModbusError` is the **inverter** rejecting the request, e.g. the
proper `0x02` illegal-data-address exception past `34100`. `poll()`
treats both the same under `on_error="skip"`, but they mean different things
when you are probing ranges by hand. See
[Troubleshooting](caveats.md#two-kinds-of-refusal).
!!!

---

## Lower-level building blocks

Useful for experimenting, testing, or building your own framing.

### Crypto — `sitsolar_dongle.crypto`

```python
derive_keys(serial: str) -> Keys
seal(plaintext: bytes, keys: Keys, *, iv: bytes | None = None) -> bytes
open_envelope(envelope: bytes, keys: Keys, *, verify: bool = True) -> bytes
```

`seal()` produces `IV | ciphertext | HMAC-SHA256`. `open_envelope()` verifies
the HMAC **before** decrypting and raises `EnvelopeError` on any mismatch,
mirroring the vendor app's behaviour. See
[Crypto](protocol/crypto.md) for the derivation and envelope layout.

### Framing — `sitsolar_dongle.framing`

```python
build_read(slave: int, address: int, count: int, fc: int = 0x04) -> bytes
parse_read_reply(payload: bytes) -> ReadReply
wrap_payload(payload: bytes, tid: int, protocol: int = 0) -> bytes
parse_header(header: bytes) -> tuple[int, int, int, int]
frame_length(header: bytes) -> int
```

Constants: `HEADER` (`64 07 00 02 00 05`), `MAX_REGISTERS` (`50`),
`FC_READ_INPUT` (`0x04`), `FC_READ_HOLDING` (`0x03`), `PROTO_REQUEST`
(`0x1000`), `PROTO_PUSH` (`0x0001`).

`Frame` exposes `tid`, `protocol`, `unit`, `envelope`, and an `is_push` property.
`ReadReply` exposes `slave`, `fc`, and `registers`.

### CRC16 — `sitsolar_dongle.crc`

```python
crc16(data: bytes) -> int
crc16_bytes(data: bytes) -> bytes   # big-endian, ready to append
selftest() -> bool
```

!!!danger This is not the standard Modbus CRC16
It is a custom table-driven routine recovered from the vendor app, and its
lookup tables are **not** symmetric. A single wrong row silently corrupts
one of the two derived keys — the AES key while the HMAC key keeps
validating — producing the baffling "every frame authenticates but nothing
decrypts" symptom. The module refuses to start unless `selftest()` passes,
and the test suite pins the tables against vectors taken from the APK and a
live dongle.
!!!

### Registers — `sitsolar_dongle.registers`

```python
REGISTERS: tuple[Register, ...]
by_section() -> dict[str, list[Register]]
blocks(max_registers: int = 50) -> list[tuple[int, int]]
SECTION_ORDER: tuple[str, ...]
```

`blocks()` coalesces the register map into at most 50-register read ranges — 34
of them for the full map. Each `Register` exposes `address`, `end`, `span`,
`section`, `key`, `name`, `dtype`, `unit`, `scale`, `length`, `notes`, `enum`,
and `bits`.

### Decode — `sitsolar_dongle.decode`

```python
decode(reg: Register, words: tuple[int, ...]) -> dict[str, Any]
```

Turn raw words into the reading dict described above. Raises `ValueError` if the
word count does not match `reg.length`.

---

## Full example

```python
from sitsolar_dongle import DongleClient, ModbusError, DongleNoDataError

with DongleClient("192.168.1.50", "<DONGLE_SERIAL>", timeout=5.0) as dongle:
    # Identity from the handshake
    info = dongle.info
    print(f"{info.factory} {info.model}  sn={info.serial}")

    # Attached devices
    for dev in dongle.devices():
        print(dev.model, dev.serial, dev.device_type, dev.modbus_id)

    # A raw read of two 16-bit words at 31002 (grid frequency)
    words = dongle.read(31002, 2)
    print(words, ((words[0] << 16) | words[1]) * 0.001, "Hz")

    # Everything the map documents
    try:
        data = dongle.poll(on_error="skip")
    except (ModbusError, DongleNoDataError) as exc:
        print("modbus error:", exc)
        raise

    for key, reading in data.items():
        if "value" in reading:
            print(f"{key:34s} {reading['value']:>10} {reading.get('unit', '')}")

    for missed in data.get("_unreadable_ranges", []):
        print("skipped", missed["range"], missed["error"])
```
