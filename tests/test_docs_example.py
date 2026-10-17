"""The Home Assistant example in ``docs/library.md``, run for real.

The example ships a reconnect backoff, which is easy to get subtly wrong.
Mirroring that logic here would let the two drift, so this pulls the code
block straight out of the markdown, compiles it, and drives it against a fake
clock and a stub client.  If the documented example stops reconnecting, this
test fails.
"""

from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path

import pytest

import sitsolar_dongle
from sitsolar_dongle import DongleError

DOCS = Path(__file__).resolve().parents[1] / "docs" / "library.md"
HEADING = "### Using it from Home Assistant"
TICK = 15.0


def example_source() -> str:
    """The first python block under the Home Assistant heading."""
    text = DOCS.read_text(encoding="utf-8")
    start = text.index(HEADING)
    block = re.search(r"```python\n(.*?)```", text[start:], re.DOTALL)
    assert block, f"no python code block under {HEADING!r}"
    return block.group(1)


class Clock:
    """A stand-in for the ``time`` module the example imports."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float = TICK) -> None:
        self.now += seconds


class FakeClient:
    """Stands in for :class:`AsyncDongleClient`.

    ``connected`` flips the way the real client's does: a raised
    ``DongleError`` from ``poll()`` means the stream broke, so the socket is
    dropped rather than left looking healthy.
    """

    default_reachable = True
    instances: list["FakeClient"] = []

    def __init__(self, host: str, serial: str, *, timeout: float) -> None:
        self.host = host
        self.serial = serial
        self.timeout = timeout
        self.reachable = type(self).default_reachable
        self.connected = False
        self.connect_calls = 0
        self.poll_calls = 0
        self.failing = False
        type(self).instances.append(self)

    async def connect(self) -> None:
        self.connect_calls += 1
        if not self.reachable:
            raise DongleError("connection refused")
        self.connected = True

    async def poll(self) -> dict[str, dict[str, float]]:
        self.poll_calls += 1
        if self.failing or not self.reachable:
            self.connected = False          # a timeout drops the connection
            raise DongleError("timed out waiting for a reply")
        return {"grid_frequency": {"value": 50.0}}

    async def close(self) -> None:
        self.connected = False


class UpdateFailed(Exception):
    """Stands in for ``homeassistant.exceptions.UpdateFailed``."""


class Coordinator:
    """Minimal ``DataUpdateCoordinator``.

    ``async_config_entry_first_refresh`` swallows ``UpdateFailed`` because
    that is what Home Assistant does with a first refresh: it retries setup
    rather than crashing the entry, so the integration ends up loaded and
    ticking either way.
    """

    instances: list["Coordinator"] = []

    def __init__(self, hass, logger, *, name, update_method, update_interval):
        self.name = name
        self.update_method = update_method
        self.update_interval = update_interval
        self.data: dict = {}
        type(self).instances.append(self)

    async def async_config_entry_first_refresh(self) -> None:
        try:
            self.data = await self.update_method()
        except UpdateFailed:
            self.data = {}


class Entry:
    data = {"host": "192.0.2.1", "serial": "TESTDONGLE0000001"}


class Sensor:
    def __init__(self, coordinator, key) -> None:
        self.coordinator = coordinator
        self.key = key


@pytest.fixture
def build(monkeypatch):
    """Compile the documented example and hand back its update coroutine."""

    async def _build(clock: Clock, *, reachable: bool = True):
        FakeClient.instances = []
        FakeClient.default_reachable = reachable
        Coordinator.instances = []

        helpers = types.ModuleType("homeassistant.helpers")
        helpers.__path__ = []                # type: ignore[attr-defined]
        root = types.ModuleType("homeassistant")
        root.__path__ = []                   # type: ignore[attr-defined]
        uc = types.ModuleType("homeassistant.helpers.update_coordinator")
        uc.DataUpdateCoordinator = Coordinator
        uc.UpdateFailed = UpdateFailed

        namespace: dict = {}
        with monkeypatch.context() as patch:
            patch.setitem(sys.modules, "homeassistant", root)
            patch.setitem(sys.modules, "homeassistant.helpers", helpers)
            patch.setitem(
                sys.modules, "homeassistant.helpers.update_coordinator", uc)
            patch.setattr(sitsolar_dongle, "AsyncDongleClient", FakeClient)
            source = example_source()
            exec(compile(source, f"{DOCS}:HA example", "exec"), namespace)

        # the example imports these as module globals, so swapping them in
        # afterwards rebinds what the nested async_update sees
        namespace["time"] = clock
        namespace["SolarSensor"] = Sensor

        added: list = []

        # Home Assistant's async_add_entities is a callback, not a coroutine:
        # the example calls it without awaiting
        def async_add_entities(entities) -> None:
            added.extend(entities)

        await namespace["async_setup_entry"](object(), Entry(),
                                             async_add_entities)
        return Coordinator.instances[-1].update_method, FakeClient.instances[-1]

    return _build


async def tick(update, clock: Clock, times: int = 1):
    """Advance the fake clock one interval per tick, classifying outcomes."""
    outcomes = []
    for _ in range(times):
        clock.advance(TICK)
        try:
            outcomes.append(("ok", await update()))
        except UpdateFailed as exc:
            outcomes.append(("waiting" if "lockout" in str(exc) else "failed",
                             str(exc)))
    return outcomes


def test_example_compiles(monkeypatch, build):
    """The documented example is valid, runnable Python."""
    source = example_source()
    compile(source, "example", "exec")
    asyncio.run(build(Clock()))


def test_backoff_stops_waiting_and_reconnects(build):
    """A failure that clears by itself does get retried.

    Regression: the lockout guard used to sit inside the ``try``, so its
    ``UpdateFailed`` ran the ``finally`` and pushed ``retry_after`` forward
    on every tick.  The deadline could never expire and a single transient
    timeout at t=60 s left the entities unavailable until a reload.
    """
    clock = Clock(1000.0)

    async def run():
        update, client = await build(clock)
        assert client.poll_calls == 1          # first refresh succeeded

        client.failing = True                  # one transient timeout at 60 s
        first = await tick(update, clock)
        assert first[0][0] == "failed"
        client.failing = False                 # dongle is fine straight after

        return client, await tick(update, clock, times=8)

    client, outcomes = asyncio.run(run())

    waited = [o for o in outcomes if o[0] == "waiting"]
    assert client.connected, "the client should have reconnected"
    assert client.connect_calls == 2, "never reconnected after the failure"
    assert waited, "should have spent the lockout reporting, not connecting"
    # 8 ticks is 120 s, more than the 75 s backoff, so the last tick must
    # have been an attempt rather than another wait
    assert outcomes[-1][0] == "ok"


def test_backoff_eventually_expires_when_the_dongle_is_down(build):
    """A dongle that is down at startup is retried, not abandoned.

    Regression: with the guard inside the ``try``, the very first failure set
    ``retry_after`` and every later tick extended it, so the example made
    exactly one connect attempt for the whole run and only ever reported the
    lockout.  The entities stayed unavailable until a reload.
    """
    clock = Clock(1000.0)

    async def run():
        update, client = await build(clock, reachable=False)
        # the first refresh failed and setup carried on, the way Home
        # Assistant retries a first refresh
        assert client.connect_calls == 1

        early = await tick(update, clock, times=3)
        calls_during_lockout = client.connect_calls
        client.reachable = True              # the dongle comes back
        later = await tick(update, clock, times=3)
        return client, early, calls_during_lockout, later

    client, early, calls_during_lockout, later = asyncio.run(run())

    assert all(o[0] == "waiting" for o in early), "did not back off"
    assert calls_during_lockout == 1, "retried inside the lockout"
    assert any(o[0] == "ok" for o in later), "backoff never expired"
    assert client.connect_calls == 2, "did not reconnect once it expired"


def test_successful_polls_never_set_a_backoff(build):
    """A healthy session must not drift into a backoff."""
    clock = Clock(1000.0)

    async def run():
        update, client = await build(clock)
        return client, await tick(update, clock, times=20)

    client, outcomes = asyncio.run(run())

    assert client.connect_calls == 1, "reconnected despite everything working"
    assert all(o[0] == "ok" for o in outcomes), outcomes
    assert all(o[1]["grid_frequency"]["value"] == 50.0 for o in outcomes)
