"""The Home Assistant example in ``docs/library.md``, run for real.

The example ships a reconnect backoff, which is easy to get subtly wrong.
Mirroring that logic here would let the two drift, so this pulls the code
block straight out of the markdown, compiles it, and drives it against a fake
clock and a stub client.  If the documented example stops reconnecting, this
test fails.

Skipped when ``docs/`` is absent, because the sdist deliberately ships
``tests/`` without the documentation (see ``[tool.hatch.build.targets.sdist]``
in ``pyproject.toml``).  Anyone running the suite from an unpacked tarball --
distro packagers, typically -- therefore gets a skip here rather than four
failures for a file that was never included.  CI and checkouts have the file.
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

if not DOCS.exists():
    pytest.skip(
        f"{DOCS.name} is not shipped in the sdist, so there is no example to "
        "extract -- run this suite from a checkout",
        allow_module_level=True,
    )


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

    ``connected`` flips the way the real client's does.  Three failure modes
    are modelled separately, because the documented example treats them
    differently:

    * ``failing`` -- the dongle is gone.  The reader declares the connection
      lost, ``connected`` goes False and ``DongleError`` is raised, so the
      ``finally`` arms the backoff.
    * ``cancellable`` -- a caller cancels mid-poll.  The reader owns the
      stream, so cancelling only abandons that request and the connection
      survives.  This is the case that proves a cancellation is *not* worth a
      75 s lockout.
    * ``healthy_error`` -- an error on a socket that stayed in sync, such as a
      ``ModbusError`` under ``on_error="raise"``.  ``connected`` stays True,
      so no backoff is armed.
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
        self.close_calls = 0
        self.failing = False
        self.cancellable = False
        self.healthy_error = False
        type(self).instances.append(self)

    async def connect(self) -> None:
        self.connect_calls += 1
        if not self.reachable:
            raise DongleError("connection refused")
        self.connected = True

    async def poll(self) -> dict[str, dict[str, float]]:
        self.poll_calls += 1
        if self.cancellable:
            try:
                await asyncio.Event().wait()      # never set: only cancel ends it
            except BaseException:
                # the reader owns the stream, so the connection survives a
                # cancelled request and the next poll reuses the socket
                raise
        if self.healthy_error:
            raise DongleError("modbus exception code 1")   # socket kept
        if self.failing or not self.reachable:
            self.connected = False          # the connection is finished
            raise DongleError("timed out waiting for a reply")
        return {"grid_frequency": {"value": 50.0}}

    async def close(self) -> None:
        self.close_calls += 1
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
    """Minimal config entry: enough of the unload-hook surface for the docs."""

    data = {"host": "192.0.2.1", "serial": "TESTDONGLE0000001"}
    instances: list["Entry"] = []

    def __init__(self) -> None:
        self.unloaders: list = []
        type(self).instances.append(self)

    def async_on_unload(self, func) -> None:
        self.unloaders.append(func)


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
        Entry.instances = []

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
    on every tick.  The deadline could never expire, so a single transient
    timeout on the first tick after setup left the entities unavailable
    until the integration was reloaded.
    """
    clock = Clock(1000.0)

    async def run():
        update, client = await build(clock)
        assert client.poll_calls == 1          # first refresh succeeded

        client.failing = True        # one transient timeout, on the next tick
        first = await tick(update, clock)
        assert first[0][0] == "failed"
        client.failing = False       # dongle is fine straight after

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


def test_cancelled_poll_does_not_set_a_backoff(build):
    """A cancelled poll leaves the connection usable, so it gets no backoff.

    The client reads through a background reader now, so cancelling ``poll()``
    only abandons that request: the connection survives and the next tick
    retries on the same socket.  Arming the 75 s lockout for a routine
    cancellation would cost far more than the cancellation it was meant to
    protect against.

    The ``finally`` is still load-bearing — it arms the backoff when the
    reader declares the connection dead, which
    ``test_backoff_stops_waiting_and_reconnects`` covers.
    """
    clock = Clock(1000.0)

    async def run():
        update, client = await build(clock)
        client.cancellable = True

        calls_before = client.poll_calls
        task = asyncio.create_task(update())
        for _ in range(100):               # let the update reach poll()
            if client.poll_calls > calls_before:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("async_update never reached poll()")

        task.cancel()                      # what asyncio.timeout would do
        with pytest.raises(asyncio.CancelledError):
            await task
        survived = client.connected

        client.cancellable = False
        next_tick = await tick(update, clock)
        return client, survived, next_tick

    client, survived, next_tick = asyncio.run(run())

    assert survived, "a cancelled request must not cost the connection"
    assert next_tick[0][0] == "ok", "backed off after a mere cancellation"
    assert client.connect_calls == 1, \
        "a cancellation should not have needed a reconnect"


def test_the_documented_example_closes_on_unload(build):
    """The example closes the client, or reloading the entry locks the dongle out.

    The reader task keeps its client alive, so a client left to the garbage
    collector holds its connection open — and the dongle serves one connection
    at a time, so a leaked client locks every other client out until the
    dongle is restarted.  ``async_on_unload`` is what stops that.
    """
    clock = Clock(1000.0)

    async def run():
        update, client = await build(clock)
        assert client.connected
        entry = Entry.instances[-1]
        for closer in entry.unloaders:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        return client

    client = asyncio.run(run())

    assert client.close_calls == 1, "unload never closed the client"
    assert not client.connected, "unload must leave the connection closed"


def test_a_healthy_connection_is_not_backed_off(build):
    """An error on a still-usable socket gets no backoff.

    The docs say an ``EnvelopeError``, or a ``ModbusError`` raised under
    ``on_error="raise"``, leaves the stream in sync and the connection worth
    keeping.  So ``connected`` stays True, the ``finally`` must not arm the
    lockout, and the next tick retries on the same socket rather than
    spending 75 s waiting for nothing.
    """
    clock = Clock(1000.0)

    async def run():
        update, client = await build(clock)
        client.healthy_error = True

        failed = await tick(update, clock)
        still_connected = client.connected
        calls = client.connect_calls

        client.healthy_error = False
        next_tick = await tick(update, clock)
        return client, failed, still_connected, calls, next_tick

    client, failed, still_connected, calls, next_tick = asyncio.run(run())

    assert failed[0][0] == "failed", failed
    assert still_connected, "a healthy socket was dropped"
    assert calls == 1, "reconnected despite a usable connection"
    assert next_tick[0][0] == "ok", "backed off after a non-fatal error"
    assert client.connect_calls == 1, "still no reconnect needed"
