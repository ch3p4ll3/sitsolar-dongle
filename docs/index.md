---
title: sitsolar-dongle
icon: book
order: 0
description: Read-only Modbus client for the SiNeng / FuturaPulse WiFi dongle used by Sit Solar inverters.
---

# sitsolar-dongle

A read-only Modbus client for the **SiNeng / FuturaPulse** WiFi dongle that
ships with Sit Solar inverters — the "Si-Dongle-L" that listens on TCP port
**7078**.

It speaks the same *secure Modbus* frame as the official Android app, so you can
pull the inverter's telemetry straight off your LAN instead of routing it
through the vendor cloud.

!!!success Read-only by design
Nothing in this package ever writes to the inverter. Only Modbus function
codes **0x04** (read input registers) and **0x03** (read holding registers)
are implemented. The entire writable holding-register block `40001`–`49999`
is deliberately absent from the register map.
!!!

!!!danger Unofficial project
This project is **not** affiliated with, endorsed by, or supported by
SiNeng, FuturaPulse, or Sit Solar. The protocol was reverse-engineered from
the vendor's Android app and its published Modbus protocol document. Use it
at your own risk, on your own equipment.
!!!

||| :icon-plug: One command to telemetry

`sitsolar-dongle 192.168.1.50 --sn <SERIAL>` connects, authenticates, and prints
every decoded value. Add `--json` for machine-readable output.

||| :icon-key: Keys derived from the serial

Both the AES and HMAC keys are derived from the dongle's printed serial number.
There is nothing else to configure.

||| :icon-shield: Authenticated frames

Every frame is AES-128-CBC encrypted and HMAC-SHA256 signed. The MAC is checked
before decryption, so a wrong key fails cleanly instead of producing garbage.
|||

||| :icon-database: 183 documented values

A complete register map transcribed from the FuturaPulse hybrid inverter Modbus
protocol, with enum labels, bit fields, and per-register scale factors.

||| :icon-home: Home-automation friendly

`--interval` polls forever and `--json` emits one object per sample, ready to
feed Home Assistant, MQTT, or InfluxDB.

||| :icon-terminal: Also a library

`from sitsolar_dongle import DongleClient` gives you the same session as a plain
Python API.
|||

---

## Quick start

```bash
git clone https://github.com/ch3p4ll3/sitsolar-dongle.git
cd sitsolar-dongle
uv venv && uv sync

sitsolar-dongle 192.168.1.50 --sn <DONGLE_SERIAL>
```

Head to [Getting Started](getting-started.md) for the full walkthrough,
including how to find your dongle and locate its serial number.

!!!warning Treat the serial number like a password
Both session keys are derived from the dongle serial alone, so **anyone on
your network who knows that serial can talk to the dongle**. Do not commit
it, paste it into issues, or share logs and packet captures that contain it.
!!!
---

## What's in these docs

| Section | What you'll find |
|---|---|
| [Getting Started](getting-started.md) | Requirements, installation, and your first poll. |
| [CLI Reference](cli.md) | Every command-line option, exit code, and output mode. |
| [Python API](library.md) | Using `DongleClient` and the lower-level building blocks. |
| [Protocol](protocol/index.md) | The wire format, handshake, and secure envelope. |
| [Register Map](protocol/registers.md) | All 183 values with types, scales, and known quirks. |
| [Caveats & Troubleshooting](caveats.md) | Firmware differences, connection limits, and common failures. |
| [Development](development.md) | Project layout, architecture, and running the test suite. |

---

## Tested with

| | |
|---|---|
| Dongle | SiNeng / FuturaPulse "Si-Dongle-L" |
| Inverter | Futura Sun 3K-HS (single-phase high-voltage hybrid, device type `33`) |
| Firmware | DSP1 `0000090021`, DSP2 `0000060010`, ARM `001002032` |

Other models and firmware versions may expose different register blocks —
see [Caveats](caveats.md#firmwares-differ). Reports are welcome; please redact
your serial number first.
