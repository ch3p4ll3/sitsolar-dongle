# sitsolar-dongle-reader

Read-only Modbus client for the **SiNeng / FuturaPulse** WiFi dongle that ships
with Sit Solar inverters (the "Si-Dongle-L" on TCP port 7078).

It implements the same *secure Modbus* frame the official Android app speaks,
so you can pull the inverter's telemetry straight off the LAN instead of going
through the vendor cloud.

Nothing in this package ever writes to the inverter — only FC 0x04 / 0x03 reads.

> **Unofficial project.** It is not affiliated with, endorsed by or supported by
> SiNeng, FuturaPulse or Sit Solar. The protocol was worked out from the vendor's
> Android app and its published Modbus protocol document. Use it at your own
> risk, on your own equipment.

## Documentation

Full documentation is published at
**<https://ch3p4ll3.github.io/sitsolar-dongle-reader/>** — it covers the
[install walkthrough](https://ch3p4ll3.github.io/sitsolar-dongle-reader/getting-started/),
the [CLI](https://ch3p4ll3.github.io/sitsolar-dongle-reader/cli/) and
[Python API](https://ch3p4ll3.github.io/sitsolar-dongle-reader/library/)
references, the [wire protocol](https://ch3p4ll3.github.io/sitsolar-dongle-reader/protocol/),
and the [complete register map](https://ch3p4ll3.github.io/sitsolar-dongle-reader/protocol/registers/)
with known hardware quirks.

The site is built from [`docs/`](docs/) with [Retype](https://retype.com) and
deployed to GitHub Pages automatically by GitHub Actions on every push to
`main`. To preview it locally:

```bash
npm install --global retypeapp
retype start
```

## Install

From [PyPI](https://pypi.org/project/sitsolar-dongle/):

```bash
pip install sitsolar-dongle
```

Or from the root of this repository:

```bash
uv venv && uv sync
```

Requires Python 3.10+ and `pycryptodome`.

New versions are published to PyPI automatically whenever a GitHub Release is
published — see [Development](https://ch3p4ll3.github.io/sitsolar-dongle-reader/development/)
for the release process.

## Quick start

```bash
sitsolar-dongle 192.168.1.50 --sn <DONGLE_SERIAL>
```

`--sn` is the **dongle** serial number (the number printed on the dongle
itself, not the inverter's). Both session keys are derived from it:

```
aes_key  = MD5(sn || crc16_be(sn))
hmac_key = MD5(reverse(sn) || crc16_be(reverse(sn)))
```

If the serial is wrong every frame fails authentication, so the tool fails
fast with `HMAC mismatch (wrong serial number?)`.

> **Treat the serial like a password.** Both keys come from it alone, so anyone
> on your network who knows it can talk to the dongle. Don't commit it, paste it
> into issues, or share logs and captures that contain it.

### Useful invocations

```bash
# machine readable
sitsolar-dongle 10.0.0.5 --sn ... --json

# follow forever, one sample every 15 s (handy for Home Assistant / MQTT)
sitsolar-dongle 10.0.0.5 --sn ... --interval 15 --json

# only the AC measurements, with raw register words
sitsolar-dongle 10.0.0.5 --sn ... --section "AC_side" --raw

# a few specific values
sitsolar-dongle 10.0.0.5 --sn ... --key grid_frequency --key battery_soc

# raw reads, ignoring the register map
sitsolar-dongle 10.0.0.5 --sn ... --read 31002:2 --read 30100:1

# show the built-in register map
sitsolar-dongle --list
```

## Library use

```python
from sitsolar_dongle import DongleClient

with DongleClient("192.168.1.50", "<DONGLE_SERIAL>") as dongle:
    print(dongle.info)                       # factory, model, serial, clock

    hz = dongle.read(31002, 2)               # two 16-bit words, big endian
    print(hz)

    for key, reading in dongle.poll().items():
        if "value" in reading:
            print(f"{key:34s} {reading['value']} {reading.get('unit', '')}")
```

`poll()` reads every documented read-only register and returns a dict of
decoded values. Ranges the firmware does not implement are recorded under
`_unreadable_ranges` instead of raising — pass `on_error="raise"` to make them
fatal.

## Protocol

```
+--------+----------+--------+------+---------------------------------+
| tid    | protocol | length | unit | AES-128-CBC envelope            |
| 2 byte | 2 byte   | 2 byte | 1    | IV(16) | CT | HMAC-SHA256(32)   |
+--------+----------+--------+------+---------------------------------+
```

* The 7-byte header (`tid`, `protocol`, `length`, `unit`) is sent in the clear:
  it is neither encrypted nor covered by the HMAC. `length` counts the unit
  byte plus the envelope (`1 + len(envelope)`).
* Outgoing frames use protocol id `0x1000`, except the reply to a heartbeat
  push, which uses `0x1001`. Unsolicited pushes from the dongle arrive as
  `0x0001`.
* The HMAC covers `IV || ciphertext` and is checked before decryption, so a
  wrong key fails cleanly rather than producing garbage.

Inside the encrypted payload sits an ordinary **Modbus RTU** frame, wrapped:

```
request : 64 07 00 02 00 05 | slave fc addr(2) count(2) crc16(2)
response: 64 07 00          | slave fc [bytecount data | exception] crc16(2)
```

The CRC16 is a vendor-specific routine (not the standard Modbus CRC) and is
sent high byte first. The `64 07 00 02 00 05` prefix is a constant — **the
target device is chosen by the slave address byte** inside the inner frame:
`0x00` is the dongle itself, `0x01` is the inverter behind it.

A reply consisting of just `64 07 01` — the three prefix bytes with a
non-zero third byte and no frame behind them at all — means the dongle has no
data for that address. It raises `DongleNoDataError` and is a *dongle*-level
refusal, distinct from a Modbus exception raised by the *inverter*: addresses
past `34100` answer a proper `0x02` illegal data address, while `33051–33100`,
`33950–33999` and `34051–34100` come back as the bare stub.

The handshake, mirroring the app:

1. `BeforeAccredit` (`42 02`) → dongle replies with version, factory, model,
   serial and clock.
2. `Accredit` (`42 03`) → one status byte. Nothing else is served until this
   succeeds.
3. `CallAll` (`42 07`, optional) → dongle pushes its own DeviceInfo plus the
   attached devices as JSON.
4. FC `0x04` reads against slave `0x01`.

Heartbeat pushes (`43 01` + unix time) are answered automatically with the
push's own transaction id, exactly like the app does.

## Register map

Read-only **input registers** `30000`–`35017`, transcribed from
*FuturaPulse hybrid inverter Modbus protocol v0.0.3*:

| Section | Range |
|---|---|
| device_info | 30000–30082 |
| operation_data | 30100–30109 |
| error_info | 30200–30211 |
| temperatures | 30301–30316 |
| ARM_data | 30400–30421 |
| DC_side | 30500–30508 |
| AC_side | 31000–31040 |
| EPS_parameters | 32001–32026 |
| battery_parameters | 32040–32093 |
| meter_parameters | 33001–33043 |
| generation_info | 34001–34043 |
| Italy_self-test | 35001–35017 |

Document register numbers are used **verbatim** as the Modbus PDU address — no
`30000` offset is subtracted. That is confirmed both by the document's own
example frames (`01 04 75 94 …` reads register 30100) and by live traffic.

32-bit values are **big-endian pairs**: the low address holds the *high* word,
so `(0, 50010)` at `31002~31003` is 50.010 Hz. This is worth stating explicitly
because the lifetime counters were first mapped as single 16-bit registers —
which read only the high word, always `0` — and so reported every lifetime
total as zero while the real value sat one register over. As a cross-check the
decoded totals now match the vendor cloud exactly: `all_charge` 600.2,
`all_discharge` 570.1, `all_pv_to_battery_level` 437.6,
`all_battery_to_grid_capacity` 317.7, `totalOngridPower` 446.5,
`totalBuyGridPower` 51.8 kWh.

The writable holding registers (`40001`–`49999`) are deliberately absent: this
project never writes.

## Notes and caveats

* **One connection at a time.** The dongle stops accepting new TCP
  connections for roughly 75 s after a rapid reconnect. Keep each session on a
  single socket and space out runs.
* **Firmwares differ.** Single-phase units have no S/T phase, and optional
  blocks (EPS, meter, Italy self-test) may refuse a read — either with Modbus
  exception `0x02` from the inverter or with the dongle's bare `64 07 01`
  stub. Both are recorded under `_unreadable_ranges` by default.
* **`31016~31017` T-phase power** is documented as `S16` in the table but the
  address spans two registers and the R/S phases are `S32`; it is treated as
  `S32` here.
* Function codes in the app's JSON templates are parsed as *decimal*, so a
  template value of `"66"` is `0x42` on the wire.

### Where the document disagrees with the hardware

Every entry below was checked against a live 3K-HS and against the vendor's own
cloud API, which is what the official app displays. Each one is also recorded in
the `notes` field of the register and printed by `--list`.

| Register | Document says | What it actually does |
|---|---|---|
| `31039~31040` | Inverter output apparent power | Tracks **P** within 1–2 W (419 VA vs 417 W, 425 VA vs 424 W) — it looks like `P / power_factor`. True apparent power is `V·I`, which agrees with `√(P²+Q²)` at ≈ 470 VA, about 50 VA higher. Compute `√(P²+Q²)` for a VA sensor. |
| `31022` | Power factor | Sits at a near-constant `0.999`. It equals `P / (31039~31040)` but contradicts `P/√(P²+Q²) ≈ 0.89`. The vendor cloud publishes the same `0.999`, so this is a setpoint rather than a measurement — see the writable `46056 Power factors adjust`. |
| `33002` | Meter R-phase current, scale `0.1` | `14 → 1.4 A` implies `V·I ≈ 327 VA` while meter P reads 0–1 W and Q reads 25 var. A `0.01` scale (`0.14 A` → 33 VA) fits instead. Voltage and power both agree with the cloud. |
| `33020` | Meter power factor, scale `0.01` | Self-consistent — it is `≈ P/√(P²+Q²)` with P at a fraction of a watt — but in self-consumption mode grid exchange is near zero, so the number carries no information. |
| `30211` | DSP warning 5, "more details in Appendix 11" | **Not a bit field.** Appendix 1 / Chart 11 is a table of decimal warning codes. |

**The warning codes.** `30207`–`30210` are four ordinary bit-field words
(Charts 8–10). Chart 11 addresses their *combined* space as
`201 + 16·word_index + bit_index` — that formula reproduces every row of the
chart (201/204/213 from word 0, 219/225/230 from word 1, 241/247 from word 2),
which is what shows `30211` carries a code rather than a mask. The first
possible code is therefore 201, so the value **200 means no warning** — which
is what a healthy unit reports.

## Tested with

| | |
|---|---|
| Dongle | SiNeng / FuturaPulse "Si-Dongle-L" |
| Inverter | Futura Sun 3K-HS (single-phase high-voltage hybrid, device type 33) |
| Firmware | DSP1 `0000090021`, DSP2 `0000060010`, ARM `001002032` |

Other models and firmware versions may expose different register blocks. Reports
are welcome, please redact your serial number first.

## Tests

```bash
PYTHONPATH=src python tests/test_dongle.py     # no pytest needed
# or
pytest tests/
```

The suite pins the CRC16 lookup tables against vectors taken from the APK and
a live dongle, the tables are **not** symmetric, and a single wrong row can
corrupt only one of the two keys (which one depends on the digits of the
serial). If it hits the AES key, the HMAC key keeps validating, which shows up
as the baffling "every frame authenticates but nothing decrypts" symptom.

## License

GNU General Public License v3.0, see [LICENSE](LICENSE).