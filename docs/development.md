---
title: Development
icon: gear
order: -8
---

# Development

Project layout, module responsibilities, the test suite, and how this
documentation gets built and published.

---

## Repository layout

```text
dongle-reader/
├── retype.yml                 Retype configuration (this site)
├── pyproject.toml             Package metadata, deps, console script
├── uv.lock                    Locked dependencies
├── docs/                      Documentation source (Markdown)
├── src/sitsolar_dongle/
│   ├── __init__.py            Public API surface
│   ├── __main__.py            `python -m sitsolar_dongle`
│   ├── cli.py                 Command-line interface
│   ├── client.py              DongleClient: blocking transport
│   ├── async_client.py        AsyncDongleClient: asyncio transport
│   ├── session.py             ClientCore: logic shared by both clients
│   ├── errors.py              DongleError, the base exception
│   ├── crypto.py              Key derivation + AES/HMAC envelope
│   ├── crc.py                 Vendor CRC16 + selftest
│   ├── framing.py             Header, inner RTU frames, error types
│   ├── decode.py              Raw words -> decoded readings
│   └── registers.py           The register map
└── tests/
    ├── test_dongle.py         CRC vectors, framing, decoding
    └── test_async_client.py   Async client against a scripted dongle
```

---

## Architecture

Requests flow through four layers, each with one job:

| Layer | Module | Responsibility |
|---|---|---|
| Transport | `client.py`, `async_client.py` | Socket/streams, handshake, heartbeat replies, tid matching. The async side does this in a background reader task that owns the stream. |
| Session | `session.py` | `ClientCore`, shared by both transports: key setup, frame sealing, `BeforeAccredit`/`CallAll` parsing, `poll()` assembly. |
| Framing | `framing.py` | Outer header, inner Modbus RTU frame, error taxonomy. |
| Envelope | `crypto.py`, `crc.py` | AES-128-CBC + HMAC-SHA256, key derivation, CRC16. |
| Semantics | `registers.py`, `decode.py` | What each register means and how to turn words into values. |

The two transports implement only the bytes on and off the wire. Anything
they would otherwise duplicate — the handshake parsing, `seal_frame()`, the
refused-range bookkeeping in `poll()` — lives in `ClientCore`, so the blocking
and asyncio clients cannot drift apart.

Two design points are worth calling out:

**Heartbeats are answered by a task that owns the stream.** In the asyncio
client, `_reader_loop` is the only thing that reads. It pops frames off the
wire and, on seeing a push, answers it and continues — so a heartbeat can never
be mistaken for a reply, and every request gets its frame handed to it by
transaction id through a future in `_pending`.

Single ownership is what makes cancellation safe. Cancelling a request only
abandons that request's future; the reader has already consumed whatever was on
the wire, so the stream stays on a frame boundary and the next request reuses
the socket. Under the older design every read owned the socket, so a cancelled
poll tore the connection down — and against the dongle's ~75 s reconnect
lockout that turned a routine cancellation into a 75 s outage.

The reader also watches for a dead peer. It declares the connection finished on
a closed socket, an RST, `keepalive` of silence, or three consecutive request
timeouts, and hands the failure to any request waiting on it rather than letting
it burn its full `timeout`. Silence is judged across *all* traffic, not just
heartbeats, so a live connection never trips it however long the caller stays
quiet, and `keepalive` defaults to three heartbeat periods rather than a hard
number — the 37 s interval was measured on one device and is not a protocol
constant.

Two details are easy to get wrong and worth knowing about:

**The loop is a module-level function, not a method.** A task holds a strong
reference to its coroutine, and a coroutine holds `self` — so a reader method
would keep its client alive forever. That would be a real defect here: a client
dropped without `close()` would hold its connection open, and the dongle serves
one connection at a time, so it would lock every other client out until
restarted. `_reader_loop` is handed `weakref.ref(self)` instead and releases
even that before each read, so an abandoned client is collected and the reader
hangs up on the next frame. There is a test that asserts the client is actually
collectable, and it fails if the reference is made strong again.

**Three timeouts in a row drop the connection.** `keepalive` only sees total
silence, so a dongle that completes the handshake and then never answers reads
leaves `connected` stuck on True while every poll fails — and anything keying
its reconnect off `connected` would retry that dead socket forever. A single
timeout still leaves the connection alone, because a slow inverter is not a
broken one; only a run of them is.

The blocking client has no such task — it answers a heartbeat only while a read
is in flight. The hardware measurement (below) shows the dongle drops a
connection whose heartbeats go unanswered after about five missed pushes, but it
compared "never answer" against "answer immediately", so what it says about the
blocking client's *late* answers is inference rather than measurement.

**Sealing rebuilds the header by hand.** The outer 7-byte header must survive in
the clear while the protocol id is patched from `0x0000` to `0x1000` *after*
encryption. `seal_frame()` therefore splits the header off, seals the payload,
rejoins, and patches the protocol and length fields — mirroring `utils.a.m58`.
Both clients reach it through `ClientCore._seal()`.

---

## Measured behaviour

Several claims in these docs rest on measurements rather than on reading the
protocol. `tools/hw_probe.py` is what produced them; it is deliberately kept
out of the sdist and out of `testpaths`, so it neither ships nor gets collected.

Run against a single Si-Dongle-L (serial redacted — it is a credential, since
both session keys derive from it) on a quiet LAN, 600 s of idling:

| Mode | Result |
|---|---|
| `passive` — connect, then never read | `backlog 5, answered 0`, then `FAILED: DongleError: connection closed by the dongle` |
| `pumped` — connect, answer pushes from a background reader | `backlog 0, answered 16, poll s 10.51`, then `SURVIVED` |

Heartbeats arrived roughly every 37 s, so 16 answered pushes over 600 s is what
a healthy session should look like. The passive run accumulating exactly five
and then being cut off is where "the dongle drops a client after about five
unanswered pushes" comes from — about 185 s.

### What that does and does not establish

- **Measured:** a client that never answers is dropped; one that answers
  immediately is not. That is the basis for the background reader.
- **Inferred, not measured:** the exact kill time. Five is read from the
  backlog counter flat-lining, not from watching the clock, so treat ~185 s as
  an estimate. The probe logs enough to pin it down — the missing piece is the
  line where the backlog stops growing.
- **Inferred, not measured:** what the blocking client's *late*, bursty
  answers do. The probe compared "never" against "immediately", which brackets
  but does not cover "eventually". A third mode that drains queued pushes on a
  timer would settle it.
- **Not measured at all:** what triggers the ~75 s reconnect lockout. See
  [Caveats](caveats.md#one-connection-at-a-time).
- **Unexplained:** `poll s 10.51` in the pumped run, against roughly 1 s in the
  documented happy path. A no-idle baseline would show whether that is this
  dongle's normal or overhead in the pump.

To reproduce, use `--sn-file` rather than `--sn` so the serial stays out of
your shell history:

```bash
python tools/hw_probe.py --host <dongle-ip> --sn-file ./sn idle --seconds 600
```

---

## Running the tests

`test_dongle.py` needs nothing beyond the package itself:

```bash
PYTHONPATH=src python tests/test_dongle.py
```

The async client tests use `pytest.raises`, so run the whole suite with:

```bash
pytest tests/
```

Install the dev dependencies first:

```bash
uv sync --extra dev      # or: pip install -e ".[dev]"
```

### The test suite {#the-test-suite}

The suite pins the **CRC16 lookup tables** against vectors taken from the APK
and a live dongle.

This matters more than it sounds. The tables are **not** symmetric, and a single
wrong row can corrupt only *one* of the two derived keys — which one depends on
the digits of the serial. If it hits the AES key, the HMAC key keeps validating,
which shows up as the baffling "every frame authenticates but nothing decrypts"
symptom.

!!!danger Do not "simplify" the CRC tables
The lookup tables look redundant — table A holds only eight distinct values
and its quarters appear to repeat — but entries `224..239` break the pattern.
Regenerating them from a tidy formula is how the AES key gets silently
corrupted. The tables are pinned by tests for exactly this reason.
!!!

`sitsolar_dongle.crc.selftest()` also runs at import time and refuses to start
the module if the tables fail.

`tests/test_async_client.py` takes a different approach: it runs the asyncio
client against an **in-process scripted dongle** that derives the same keys,
decrypts the real frames the client sends, and answers `BeforeAccredit`,
`Accredit`, `CallAll` and FC reads the way the hardware does. That is what
makes the failure paths testable without hardware — a refused `Accredit`, a
corrupted HMAC, a reply that never arrives, and a read cancelled mid-frame are
all driven by fault-injection flags on the fake.

---

## Adding or correcting a register

1. Edit `src/sitsolar_dongle/registers.py`, following the existing `_r(...)`
   helper style.
2. Set `dtype` (`U16`/`S16`/`U32`/`S32`/`ASCII`/`BITFIELD`), `scale`, `unit`,
   and — importantly — a `notes` string whenever the hardware disagrees with the
   protocol document.
3. `notes` is what `sitsolar-dongle --list` prints, so record the discrepancy
   there rather than only in prose.
4. Regenerate the tables in `docs/protocol/registers.md` (see below) and run the
   tests.

---

## Working on this documentation

The site is built with [Retype](https://retype.com) from the Markdown in
`docs/`, configured by `retype.yml` at the repository root.

!!!info Retype is free for this project
The free tier covers projects up to 100 pages, and this site uses around a
dozen — no license key is required.
!!!

### Preview locally

Install the CLI once:

```bash
npm install --global retypeapp
```

Then, from the repository root:

```bash
retype start      # builds, serves, and watches; opens a browser
retype build      # one-off static build into .retype/
```

The output directory `.retype/` is gitignored. Live reload picks up both content
and `retype.yml` changes.

### Regenerating the register tables

`docs/protocol/registers.md` is assembled from the source of truth rather than
maintained by hand. After changing `registers.py`, regenerate the tables so the
site cannot drift from the implementation:

```bash
python tools/gen_register_tables.py > /tmp/tables.md
```

Then splice the generated section between the `## Full map` heading and the
`## Where the document disagrees with the hardware` heading.

### Style conventions

* Retype components are used throughout: callouts (`!!!`), tabs (`+++`), and
  steps (`>>>`).
* Register addresses, keys, and hex bytes are wrapped in `backticks`.
* Security-relevant statements use `danger` or `warning` callouts.
* Every internal link is relative and points at the `.md` source — Retype
  rewrites them to the generated URLs.

---

## Continuous deployment

Documentation is built and published automatically by GitHub Actions on every
push to `main` that touches `docs/`, `retype.yml`, or the workflow itself.

>>> 1. Build

    `retypeapp/action-build` runs `retype build` and produces the static site.

>>> 2. Package

    `actions/upload-pages-artifact` uploads the output as a Pages artifact.

>>> 3. Deploy

    `actions/deploy-pages` publishes it to GitHub Pages.

>>>

The workflow lives at
[`.github/workflows/docs.yml`](https://github.com/ch3p4ll3/sitsolar-dongle-reader/blob/main/.github/workflows/docs.yml)
and runs on `ubuntu-latest` with:

| Setting | Why |
|---|---|
| `permissions: pages: write`, `id-token: write` | Required by the official Pages deployment action. |
| `concurrency` on `pages` | Prevents two deployments racing. |
| `fetch-depth: 0` | Full Git history, so the "last updated" footer can be rendered from commit metadata. |

!!!success One-time repository setting
After the first successful run, set the Pages source to **GitHub Actions**:
*Settings → Pages → Build and deployment → Source → GitHub Actions*.

The site is then published at
`https://ch3p4ll3.github.io/sitsolar-dongle-reader/` on every subsequent push
to `main`.
!!!

---

## Packaging

The project ships to PyPI as **[`sitsolar-dongle`](https://pypi.org/project/sitsolar-dongle/)**,
built with [hatchling](https://hatch.pypa.io/) from `pyproject.toml`.

| | |
|---|---|
| Distribution name | `sitsolar-dongle` |
| Import package | `sitsolar_dongle` |
| Console script | `sitsolar-dongle` → `sitsolar_dongle.cli:main` |
| Build backend | `hatchling>=1.27` |
| Python | `>=3.10` |

!!!info "The version is single-sourced"
`[project] dynamic = ["version"]` plus `[tool.hatch.version]` tells hatchling to
read the version from `__version__` in
[`src/sitsolar_dongle/__init__.py`](https://github.com/ch3p4ll3/sitsolar-dongle-reader/blob/main/src/sitsolar_dongle/__init__.py).
There is **one** place to bump it, and the CLI's `--version` output stays in
step automatically.
!!!

The metadata uses a **PEP 639** SPDX license expression
(`license = "GPL-3.0-or-later"` with `license-files = ["LICENSE"]`). Because of
that, the `License ::` trove classifiers are deliberately **omitted** — PyPI
rejects a distribution that carries both.

### Build and verify locally

```bash
python -m build          # or: uv build
twine check --strict dist/*
```

Then install the wheel into a scratch environment and smoke-test it:

```bash
python -m venv /tmp/smoke && /tmp/smoke/bin/pip install dist/*.whl
/tmp/smoke/bin/python -c "import sitsolar_dongle; print(sitsolar_dongle.__version__)"
/tmp/smoke/bin/sitsolar-dongle --version
/tmp/smoke/bin/sitsolar-dongle --list
```

---

## Continuous deployment to PyPI

Publishing is handled by
[`.github/workflows/publish-pypi.yml`](https://github.com/ch3p4ll3/sitsolar-dongle-reader/blob/main/.github/workflows/publish-pypi.yml)
and is triggered by a **published GitHub Release**. It runs three jobs:

>>> 1. Test

    The suite runs across Python 3.10–3.14.

>>> 2. Build

    `python -m build` produces the sdist and wheel, `twine check --strict`
    validates the metadata, and a guard fails the job if the release tag does
    not match the built version.

>>> 3. Publish

    `pypa/gh-action-pypi-publish` uploads to PyPI over OIDC.

>>

A separate [CI workflow](https://github.com/ch3p4ll3/sitsolar-dongle-reader/blob/main/.github/workflows/ci.yml)
runs the same tests plus a build check on every push to `main` and every pull
request, so a release is the last gate rather than the first time anything runs.

### Trusted Publishing

The workflow authenticates with **PyPA Trusted Publishing** (OIDC). No API token
is stored in the repository, and there is no long-lived credential to rotate or
leak.

!!!success One-time setup on pypi.org
On the project page: **Manage → Publishing → Add a new publisher**

| Field | Value |
|---|---|
| Owner | `ch3p4ll3` |
| Repository | `sitsolar-dongle-reader` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

The environment name **must** match the `environment: name: pypi` block in the
workflow, or the OIDC token will be rejected.
!!!

### Cutting a release

>>> 1. Bump the version

    Edit `__version__` in `src/sitsolar_dongle/__init__.py` — the single source
    of truth.

>>> 2. Commit

    Push the bump to `main` and let CI go green.

>>> 3. Tag and publish a Release

    Create a GitHub Release whose tag is `vX.Y.Z`, matching the version exactly.
    Publishing the Release triggers the workflow.

>>>

!!!warning "The tag must match the version"
If you tag `v0.2.0` while `__version__` still reads `0.1.0`, the build job fails
with an explicit error rather than publishing a mismatched release. PyPI also
refuses to accept the same version twice, so every release needs a fresh bump.
!!!

### Workflow summary

| Workflow | Trigger | Purpose |
|---|---|---|
| `ci.yml` | Push to `main`, pull requests | Tests across 3.10–3.14, build + metadata validation. |
| `docs.yml` | Push to `main` touching docs | Builds and deploys this site to GitHub Pages. |
| `publish-pypi.yml` | GitHub Release published | Tests, builds, version-gates, and publishes to PyPI. |

---

## License

GNU General Public License v3.0 — see [License](license.md).
