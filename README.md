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

## Install

From the root of this repository:

```bash
uv venv && uv sync
```

Requires Python 3.10+ and `pycryptodome`.

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

The writable holding registers (`40001`–`49999`) are deliberately absent: this
project never writes.

## Notes and caveats

* **One connection at a time.** The dongle stops accepting new TCP
  connections for roughly 75 s after a rapid reconnect. Keep each session on a
  single socket and space out runs.
* **Firmwares differ.** Single-phase units have no S/T phase, and optional
  blocks (EPS, meter, Italy self-test) may raise Modbus exception `0x02`. Those
  ranges are skipped by default.
* **`31016~31017` T-phase power** is documented as `S16` in the table but the
  address spans two registers and the R/S phases are `S32`; it is treated as
  `S32` here.
* Function codes in the app's JSON templates are parsed as *decimal*, so a
  template value of `"66"` is `0x42` on the wire.

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