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
│   ├── client.py              DongleClient: session, handshake, reads
│   ├── crypto.py              Key derivation + AES/HMAC envelope
│   ├── crc.py                 Vendor CRC16 + selftest
│   ├── framing.py             Header, inner RTU frames, error types
│   ├── decode.py              Raw words -> decoded readings
│   └── registers.py           The register map
└── tests/
    └── test_dongle.py         CRC vectors, framing, decoding
```

---

## Architecture

Requests flow through four layers, each with one job:

| Layer | Module | Responsibility |
|---|---|---|
| Session | `client.py` | Socket lifecycle, handshake, heartbeat replies, tid matching. |
| Framing | `framing.py` | Outer header, inner Modbus RTU frame, error taxonomy. |
| Envelope | `crypto.py`, `crc.py` | AES-128-CBC + HMAC-SHA256, key derivation, CRC16. |
| Semantics | `registers.py`, `decode.py` | What each register means and how to turn words into values. |

Two design points are worth calling out:

**Heartbeats are answered inside the frame iterator.** `_iter_frames()` pops
frames off the wire and, on seeing a push, answers it and continues — so a
heartbeat can never be mistaken for a reply, and pushes never pile up. Callers
see only frames matching their transaction id.

**Sealing rebuilds the header by hand.** The outer 7-byte header must survive in
the clear while the protocol id is patched from `0x0000` to `0x1000` *after*
encryption. `_seal_frame()` therefore splits the header off, seals the payload,
rejoins, and patches the protocol and length fields — mirroring `utils.a.m58`.

---

## Running the tests

The suite needs no `pytest`:

```bash
PYTHONPATH=src python tests/test_dongle.py
```

Or, if you installed the dev extra:

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
