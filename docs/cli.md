---
title: CLI Reference
icon: terminal
order: -2
---

# CLI Reference

`sitsolar-dongle` reads the read-only registers of a SiNeng / FuturaPulse
inverter through its WiFi dongle on TCP port 7078.

```bash
sitsolar-dongle <host> --sn <SERIAL> [options]
sitsolar-dongle --list
```

A `host` is required for every mode except `--list`, and `--sn` is required for
every mode except `--list`. Both are validated before any socket is opened.

---

## Connection

| Option | Type | Default | Description |
|---|---|---|---|
| `host` | positional | — | Dongle IP address or hostname. |
| `--sn` | string | — | **Dongle** serial number. Both session keys are derived from it. |
| `--port` | int | `7078` | TCP port. |
| `--slave` | int | `1` | Modbus slave address to read. `0` is the dongle itself, `1` is the inverter behind it. |
| `--fc` | `04` / `03` | `04` | Function code for reads. `04` reads input registers (the `3xxxx` block), `03` reads holding registers (`4xxxx`). Also accepts `4` and `3`. |
| `--timeout` | float | `10.0` | Socket timeout in seconds. |

!!!info Slave address selects the target device
The `64 07 00 02 00 05` frame prefix is a constant. The device actually
queried is chosen by the slave address byte inside the inner Modbus RTU
frame: **`0x00` is the dongle**, **`0x01` is the inverter**. See
[Protocol](protocol/index.md#target-selection).
!!!

---

## What to read

| Option | Repeatable | Description |
|---|---|---|
| `--read ADDR:COUNT` | yes | Raw register read. Skips the register map entirely and prints words only. `COUNT` must be `1..50`. |
| `--section NAME` | yes | Restrict the poll to one protocol-document section, e.g. `"AC_side"`. Unknown names abort with the list of known sections. |
| `--key KEY` | yes | Restrict the poll to specific register keys, e.g. `grid_frequency`. Unknown keys abort. |

`--read` takes precedence: when present, `--section` and `--key` are ignored and
the output is a list of `{address, count, hex | error}` entries rather than a
decoded register map.

```bash
sitsolar-dongle 192.168.1.50 --sn ... --read 31002:2 --read 30100:1
```

Filtering by section or key still reads the **full coalesced blocks** that
contain those registers, then discards the unwanted values locally — the dongle
is never asked for a narrower range.

---

## Output

| Option | Description |
|---|---|
| `--json` | Emit a JSON object instead of a human-readable table. |
| `--raw` | Show raw register words alongside decoded values (`raw=…`). |
| `--list` | Print the built-in register map and exit. No host or serial needed. |
| `--errors skip\|raise` | What to do when a range answers with a Modbus exception. Default `skip`. |

`--errors skip` (the default) records refused ranges under
`_unreadable_ranges` and carries on, because firmwares differ in which optional
blocks they implement. `--errors raise` makes any refusal fatal.

`--raw` adds the un-scaled integer, plus a signed interpretation in
parentheses when it differs — for example `raw=65535 (-1)` for a two's
complement `-1`.

### JSON shape

A decoded poll produces an object keyed by register key, plus underscore-prefixed
metadata entries:

```json
{
  "grid_frequency": {
    "address": "31002~31003",
    "name": "Grid frequency",
    "section": "AC_side",
    "raw": 50010,
    "value": 50.01,
    "unit": "Hz"
  },
  "battery_soc": {
    "address": "32064",
    "name": "Battery SOC",
    "section": "battery_parameters",
    "raw": 87,
    "value": 87,
    "unit": "%"
  },
  "_unreadable_ranges": [
    { "range": "32001~32026", "error": "…64 07 01 stub…" }
  ],
  "_dongle": {
    "factory": "SiNeng", "model": "Si-Dongle-L",
    "serial": "…", "version": 0, "time": 1718000000
  }
}
```

| Key | Meaning |
|---|---|
| `address` | Register span, e.g. `31002~31003`. |
| `name` | Human-readable name from the protocol document. |
| `section` | Protocol-document chapter this register came from. |
| `raw` | Un-scaled integer. For `U32`/`S32` this is the joined big-endian pair. |
| `raw_signed` | Present for `S16`/`S32` when the signed value differs from `raw`. |
| `value` | Decoded, scaled value. |
| `unit` | Engineering unit, when the register has one. |
| `label` | Enum label, when the register is enumerated. |
| `active_bits` | List of set bit names, for bit-field registers. |
| `error` | Present instead of `value` when the register was unavailable. |

Underscore-prefixed entries — `_unreadable_ranges`, `_devices`, `_dongle` — are
metadata, not registers.

---

## Sampling

| Option | Default | Description |
|---|---|---|
| `--interval SECONDS` | `0.0` (one shot) | Poll forever, sleeping this many seconds between samples. |
| `--include-device-info` | off | Also send `CallAll` and include the dongle and inverter identity JSON under `_devices`. |

With `--interval` unset or `0`, the tool performs exactly one poll and exits.
Any positive value loops until interrupted.

```bash
sitsolar-dongle 192.168.1.50 --sn ... --interval 15 --json
```

---

## Other

| Option | Description |
|---|---|
| `--version` | Print the version and exit. |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `2` | Connection or protocol failure — `DongleError` or an `OSError` (unreachable host, socket timeout, connection closed). |
| `3` | Modbus-level failure — a slave exception (`ModbusError`) or the dongle's no-data stub (`DongleNoDataError`). |
| `130` | Interrupted by `Ctrl-C`. |

---

## Examples

Poll once, human readable:

```bash
sitsolar-dongle 192.168.1.50 --sn <DONGLE_SERIAL>
```

Emit JSON and include device identity:

```bash
sitsolar-dongle 192.168.1.50 --sn ... --json --include-device-info
```

Follow forever for a Home Assistant or MQTT bridge:

```bash
sitsolar-dongle 192.168.1.50 --sn ... --interval 15 --json
```

Inspect one section with raw words shown:

```bash
sitsolar-dongle 192.168.1.50 --sn ... --section AC_side --raw
```

Watch a couple of specific keys:

```bash
sitsolar-dongle 192.168.1.50 --sn ... --key grid_frequency --key battery_soc
```

Probe undocumented registers, tolerating refusals:

```bash
sitsolar-dongle 192.168.1.50 --sn ... --read 31002:2 --read 34101:4
```

Read holding registers instead of input registers:

```bash
sitsolar-dongle 192.168.1.50 --sn ... --fc 03 --read 46056:1
```

Browse the register map offline:

```bash
sitsolar-dongle --list
```
