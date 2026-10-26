# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-10-26

The register map is updated against *FuturaPulse hybrid inverter Modbus
protocol v0.0.7* (the previous transcription was v0.0.3).

### Added

- **`battery2_parameters` section (`32240`–`32293`, 33 values)** — the second
  battery's status, SOC/SOH, cell extremes, errors and charge limits, a
  verbatim mirror of `battery_parameters` at +200 (protocol change D005). Only
  valid on inverters with two BMS interfaces; on single-battery units the
  range is refused or returns zeros.
- **`meter2_parameters` section (`33101`–`33143`, 17 values)** — the optional
  second meter, a verbatim mirror of `meter_parameters` at +100 (change D009).
- **Meter self-test registers `33048` (`meter_testing_process`, 0~100 %) and
  `33049` (`meter_testing_result`, 14 enum labels including the `0xAA55`
  "testing" sentinel)** in `meter_parameters` (change D007).

The map now holds **235 values** across **42 coalesced read ranges** (was 183
across 34), in fourteen sections.

### Changed

- **`30103` `mppt_input_type`** — v0.0.7 (change D006) reduced the enum from
  five values to two: `0` independent input, `1` same panels parallel. The old
  five-value list is kept in the register's `notes` for reference.
- The `battery_parameters` and `meter_parameters` *sections* were narrowed to
  `32040`–`32239` and `33000`–`33099` to make room for the new `battery2` and
  `meter2` sections. `--section` filters that used the old names still work.

### Fixed

- `32265` `battery2_soh` uses the address the sequence implies; the document
  prints `322065`, an obvious typo.

## [0.2.1] - 2026-10-19

The project is renamed from `sitsolar-dongle-reader` to `sitsolar-dongle`.

**Nothing changes for users.** The PyPI distribution name, the import package
and the console script were already `sitsolar-dongle`, and all three are
untouched — `pip install sitsolar-dongle`, `import sitsolar_dongle` and the
`sitsolar-dongle` command work exactly as before, with no migration and no
change to the API.

### Changed

- **Repository** `ch3p4ll3/sitsolar-dongle-reader` →
  [`ch3p4ll3/sitsolar-dongle`](https://github.com/ch3p4ll3/sitsolar-dongle).
  GitHub redirects the old address, so existing links keep working.
- **Documentation site** →
  [`ch3p4ll3.github.io/sitsolar-dongle`](https://ch3p4ll3.github.io/sitsolar-dongle/).
  Unlike the repository, a GitHub Pages project site is not redirected when its
  repo is renamed, so links to the old address may stop resolving once the site
  is republished under the new name.
- **Attribution** now names `ch3p4ll3` as the author rather than
  "sitsolar-dongle-reader contributors".
- The background reader task is named `sitsolar-dongle` in place of
  `sitsolar-dongle-reader`. Cosmetic: the name appears only in asyncio debug
  output.

### Note for the maintainer

Two settings outside this repository name the repository and must follow the
rename, or the next tagged release will fail to publish:

* **PyPI Trusted Publishing** — the registered publisher must name the
  repository `sitsolar-dongle`. See the header of
  [`publish-pypi.yml`](https://github.com/ch3p4ll3/sitsolar-dongle/blob/main/.github/workflows/publish-pypi.yml).
* **GitHub Pages** — the source repository setting under *Settings → Pages*
  must point at the renamed repository.

## [0.2.0] - 2026-10-19

Adds an asyncio client, and makes a long-lived connection survive a polling
interval of any length. `DongleClient` is unchanged in behaviour, and there
are no breaking changes.

### Added

- **`AsyncDongleClient`** — a non-blocking counterpart to `DongleClient` for
  Home Assistant, FastAPI and other asyncio applications. Same protocol core,
  register map and error types, so returned values and exceptions are
  identical; every method that touches the wire is a coroutine. Nothing in the
  module blocks the event loop.

  ```python
  async with AsyncDongleClient("192.168.1.50", "<DONGLE_SERIAL>") as dongle:
      values = await dongle.poll()
  ```

- **A background reader task** that owns the connection and answers the
  dongle's heartbeat pushes the moment they arrive. Measured against real
  hardware, the dongle closes a connection whose heartbeats go unanswered
  after about five missed pushes — roughly three minutes — while one that
  answers them stays up indefinitely. So a long-lived connection is now safe
  at any polling interval, and reconnecting per poll is unnecessary.

  What was *not* measured is documented alongside what was: see
  [Measured behaviour](https://ch3p4ll3.github.io/sitsolar-dongle/development/#measured-behaviour)
  for the probe output and the open questions.

- **`py.typed`** — the package now declares its inline annotations under
  [PEP 561](https://peps.python.org/pep-0561/), so type checkers use them.

- **A Home Assistant integration example** with reconnect backoff, wired to a
  `DataUpdateCoordinator`. It is extracted from the documentation and executed
  by the test suite, so it cannot silently drift from what is written.

- **A hardware probe**, `tools/hw_probe.py`, behind the heartbeat and
  reconnect-lockout findings. A diagnostic only: it ships in neither the sdist
  nor the test suite, and reads the serial from `--sn-file` so it stays out of
  shell history.

### Changed

- **A failure now costs much less.** Under the previous design every read owned
  the socket, so an interrupted read tore the connection down — and against the
  dongle's ~75 s reconnect lockout, a routine cancellation became a 75 s
  outage. Now:

  - cancelling or timing out a request leaves the connection usable, and a
    reply that arrives late is discarded by transaction id;
  - a dead connection is noticed as it happens rather than at the next poll,
    and a request waiting on it fails immediately instead of burning its full
    timeout;
  - a connection that is alive but stops answering is reclaimed after three
    consecutive request timeouts, since `connected` alone cannot see that
    failure;
  - `close()` is bounded, so an unload handler cannot hang on a dead link.

- **`connected` is trustworthy.** It reports False exactly when the connection
  is finished, which is what a reconnect backoff should key off.

- **Documentation of the reconnect and backoff behaviour** was rewritten around
  what the hardware actually does, with the measured and the inferred kept
  clearly apart.

### Fixed

- **A leaked client held its connection open forever.** The reader task keeps
  its client alive, so dropping the last reference no longer freed the socket
  the way it used to — and since the dongle serves one connection at a time,
  a leaked client locked every other client out until the dongle was restarted.
  The reader now holds a weak reference and hangs up on the next frame, and the
  documentation says to always `await close()`.

### Security

- **The real dongle serial is scrubbed from the documentation and examples.**
  It is a credential: both session keys derive from it.

## [0.1.0] - 2026-10-17

Initial release.

- `DongleClient`, a blocking read-only session over the dongle's TCP protocol
  on port 7078: `BeforeAccredit`/`Accredit` handshake, `CallAll` device
  discovery, and FC 0x04 reads against the inverter's input registers.
- AES-128-CBC + HMAC-SHA256 envelope and key derivation, and the vendor CRC16.
- A documented register map and decoding of raw words into named readings.
- `sitsolar-dongle` command-line interface, with `--json` output, raw reads
  and a follow-forever `--interval` mode.

Nothing in the package ever writes to the inverter.

[0.2.1]: https://github.com/ch3p4ll3/sitsolar-dongle/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ch3p4ll3/sitsolar-dongle/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ch3p4ll3/sitsolar-dongle/releases/tag/v0.1.0
