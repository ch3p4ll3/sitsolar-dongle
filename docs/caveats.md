---
title: Caveats & Troubleshooting
icon: alert
order: -7
---

# Caveats & Troubleshooting

The behaviours below are not bugs in this project — they are properties of the
dongle and its firmware. Knowing them up front saves a lot of confusion.

---

## One connection at a time {#one-connection-at-a-time}

The dongle stops accepting new TCP connections for roughly **75 seconds** after
a rapid reconnect.

!!!warning What actually triggers it is not pinned down
Two readings of the same observation are in circulation in this documentation:
that the lockout follows a *rapid reconnect*, and that it follows *any* dropped
connection. Only the first has been observed directly, and neither has been
tested systematically — settling it needs an hour on real hardware, which we
have not had.

The distinction matters. Under the pessimistic reading a single transient
failure pins a Home Assistant integration unavailable for 75 s; under the
optimistic one, a clean close followed by a deliberate reconnect is harmless.
Everything here assumes the pessimistic reading, so anything that fails should
back off rather than retry on the next tick.
!!!

One *drop* trigger is now pinned down, though. Against real hardware a session
that left heartbeats unanswered was closed by the dongle after about five
missed pushes — roughly three minutes — while an identical session that
answered each push *immediately* stayed up for ten minutes.

What was not tested is whether a late answer counts: the blocking client lets
pushes queue and answers them in a burst at the next read, which sits between
"never" and "immediately". So the advice to keep its polling interval under the
~37 s heartbeat period is a sound guess from that mechanism, not a measurement.
`AsyncDongleClient` is unaffected either way — its background reader answers
pushes the moment they arrive.

| Do | Don't |
|---|---|
| Keep each session on a **single socket**. | Open a new connection per sample. |
| Use one long-running `--interval` session. | Start and stop the tool in a tight loop. |
| Space out ad-hoc runs. | Run two clients against the same dongle. |

The practical rule: if a connection is refused shortly after a previous run
ended, wait. This is the single most common source of "it stopped working"
reports.

---

## Firmwares differ

Single-phase units have no S or T phase, and optional blocks (EPS, meter, Italy
self-test) may refuse a read. A refusal comes in **two** flavours:

### Two kinds of refusal {#two-kinds-of-refusal}

| Exception | Raised by | Meaning | Example |
|---|---|---|---|
| `ModbusError` | The **inverter** | A proper Modbus exception, e.g. `0x02` illegal data address. | Addresses past `34100`. |
| `DongleNoDataError` | The **dongle** | A bare `64 07 01` stub with no frame behind it — the dongle has no data for that address. | `33051–33100`, `33950–33999`, `34051–34100`. |

Both are recorded under `_unreadable_ranges` by default. See
[Protocol](protocol/index.md#the-64-07-01-stub) for the wire-level detail.

### Handling them

```bash
# default: record the failure and carry on
sitsolar-dongle <host> --sn ... --errors skip

# or make any refusal fatal
sitsolar-dongle <host> --sn ... --errors raise
```

In library code, `poll(on_error="skip" | "raise")` does the same:

```python
data = dongle.poll(on_error="skip")
for missed in data.get("_unreadable_ranges", []):
    print("skipped", missed["range"], "->", missed["error"])
```

Registers that were never read come back with `"error": "unavailable"` instead
of a `value`, so a partial poll is always visible in the output rather than
silently missing keys.

---

## Common failures

### `HMAC mismatch (wrong serial number?)`

Every frame fails authentication. The serial passed to `--sn` is wrong — check
that it is the number printed **on the dongle**, not the inverter's serial.

Because the MAC is verified before decryption, this fails cleanly rather than
producing garbage.

### `bad PKCS#7 padding (key or IV mismatch)`

The HMAC validated but AES decryption produced garbage. This is the signature
of an **AES key that is wrong while the HMAC key is right** — which is precisely
what a single bad row in the CRC16 lookup table does, since the two keys use the
forward and reversed serial respectively.

The CRC16 module runs `selftest()` on import and refuses to start if the tables
are wrong, so in practice this points at a corrupted or hand-modified table. See
[Crypto](protocol/crypto.md#crc16).

### `connection closed by the dongle`

The socket was dropped mid-session. Often the dongle is busy with another
client, or a previous session was torn down uncleanly. Wait out the ~75 s window
and retry.

### Connection refused / timeout on connect

| Check | |
|---|---|
| Same LAN? | The dongle is not routable and not exposed to the internet. |
| Correct host and `--port 7078`? | 7078 is the only port it serves. |
| Another client connected? | See [connection limits](#one-connection-at-a-time). |
| `--timeout` too aggressive? | The default is 10 s. |

### Values that read exactly `0` where the cloud shows a real number

Almost always the 32-bit word-order trap: the high word sits at the **low**
address, so reading one register out of a pair returns the high word — which is
`0` for a large lifetime counter. Read both registers. See
[Word order](protocol/registers.md#word-order).

### A register reports `unavailable`

The backing words were not returned, so `poll()` could not decode it. The cause
is almost always a firmware difference — check `_unreadable_ranges` in the same
sample for the matching refusal.

---

## Trust and safety

!!!danger Unofficial and unaffiliated
This project is **not** affiliated with, endorsed by, or supported by
SiNeng, FuturaPulse, or Sit Solar. The protocol was reverse-engineered from
the vendor's Android app and its published Modbus protocol document. Use it
at your own risk, on your own equipment.
!!!

!!!warning Keep your serial private
Both session keys are a pure function of the dongle serial. Anyone on your
network who knows it can talk to your dongle. Redact it from logs, captures,
and issue reports — the project's own examples use `<DONGLE_SERIAL>`.
!!!

!!!info Read-only, and staying that way
Only FC `0x04` and `0x03` reads are implemented, and the writable holding
register block is absent from the map entirely. Please keep it that way in
forks and patches.
!!!
---

## Reporting a problem

Reports on other models and firmware versions are welcome — they are how the
register map grows. Please include:

* Dongle model and inverter model / device type.
* DSP1, DSP2, and ARM firmware versions (readable from
  [`device_info`](protocol/registers.md#device_info)).
* The failing range or key, plus `_unreadable_ranges` from the same sample.
* **Your serial number redacted first.**

Open an issue at
[github.com/ch3p4ll3/sitsolar-dongle-reader/issues](https://github.com/ch3p4ll3/sitsolar-dongle-reader/issues).
