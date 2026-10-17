---
title: Getting Started
icon: rocket
order: -1
---

# Getting Started

Everything you need to go from a freshly cloned repository to live inverter
telemetry on your LAN.

---

## Requirements

| | |
|---|---|
| Python | 3.10 or newer |
| Dependency | [`pycryptodome`](https://pypi.org/project/pycryptodome/) `>= 3.19` |
| Network | The dongle and your machine on the **same LAN** (it is not exposed to the internet) |
| Optional | [`uv`](https://docs.astral.sh/uv/) for environment management |

The dongle serves plain TCP on port **7078**. It is reached by IP or hostname
on your local network; there is no cloud call anywhere in this project.

---

## Install

+++ uv

From the repository root:

```bash
uv venv && uv sync
```

This creates a virtual environment and installs the package in editable mode
along with the `sitsolar-dongle` console script.


To install the test dependencies as well:

```bash
uv sync --extra dev
```

+++ pip

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

On Windows, activate with `.venv\Scripts\activate` instead.

To install the test dependencies as well:

```bash
pip install -e ".[dev]"
```

+++

Verify the install:

```bash
sitsolar-dongle --version
```

---

## Find your dongle

The dongle obtains an address over DHCP. Locate it on your network with your
router's client list, an IP scan, or an mDNS/hostname lookup. It listens
strictly on TCP port **7078**, which is a reliable way to single it out:

```bash
# example: probe a subnet for anything answering on 7078
nmap -p 7078 192.168.1.0/24
```

---

## Find the dongle serial number

`--sn` takes the serial printed **on the dongle itself** — a label on the
casing — *not* the inverter's serial. This distinction matters: the dongle is
what terminates the TCP session, and both session keys are derived from its
serial.

!!!danger The serial number is a credential
The key derivation is:

```text
aes_key  = MD5(sn || crc16_be(sn))
hmac_key = MD5(reverse(sn) || crc16_be(reverse(sn)))
```

Both keys come from the serial **alone**. Anyone on your network who knows
it can open an authenticated session with your dongle, so treat it exactly
like a password: do not commit it, do not paste it into GitHub issues, and
redact it from logs and packet captures before sharing.

The project's own test suite and examples use the placeholder `<DONGLE_SERIAL>`.
!!!

If the serial is wrong, every frame fails authentication and the tool fails
fast with:

```text
HMAC mismatch (wrong serial number?)
```

---

## Run your first poll

```bash
sitsolar-dongle 192.168.1.50 --sn <DONGLE_SERIAL>
```

A single-shot run prints a section-grouped table of decoded values:

```text
dongle   SiNeng Si-Dongle-L  sn=…  time=…

device_info
  device_type             30000       51  (single phase high voltage)
  rated_power           30081~30082   3000  W
…

AC_side
  grid_frequency        31002~31003   50.010  Hz
  phase_r_voltage            31000      230.1  V
```

Each line shows the register key, its address span, the decoded value, and the
unit. Registers the firmware did not return are listed as `unavailable` rather
than aborting the whole poll — see [Caveats](caveats.md#firmwares-differ).

---

## Common invocations

```bash
# Machine-readable JSON, one object
sitsolar-dongle 192.168.1.50 --sn ... --json

# Follow forever, one sample every 15 s — ideal for Home Assistant / MQTT
sitsolar-dongle 192.168.1.50 --sn ... --interval 15 --json

# Only the AC measurements, with raw register words alongside decoded values
sitsolar-dongle 192.168.1.50 --sn ... --section "AC_side" --raw

# A few specific values by key
sitsolar-dongle 192.168.1.50 --sn ... --key grid_frequency --key battery_soc

# Raw reads, bypassing the register map entirely
sitsolar-dongle 192.168.1.50 --sn ... --read 31002:2 --read 30100:1

# Print the built-in register map and exit
sitsolar-dongle --list
```

See the [CLI Reference](cli.md) for the complete option list.

---

## Continuous sampling

Passing `--interval` turns the tool into a long-running poller. Each cycle
prints one sample, then sleeps. Combine it with `--json` to emit newline-free
JSON objects that a supervisor, MQTT bridge, or `mosquitto_pub` wrapper can
consume:

```bash
sitsolar-dongle 192.168.1.50 --sn ... --interval 15 --json
```

!!!warning One connection at a time
The dongle stops accepting new TCP connections for roughly **75 seconds**
after a rapid reconnect. A long-running `--interval` session is the
friendly pattern; repeatedly starting and stopping the tool is not. See
[Connection limits](caveats.md#one-connection-at-a-time).
!!!

---

## Next steps

- [CLI Reference](cli.md) — every option, exit code, and output mode.
- [Python API](library.md) — embed polling in your own application.
- [Register Map](protocol/registers.md) — what each key means.
- [Caveats & Troubleshooting](caveats.md) — when something does not add up.
