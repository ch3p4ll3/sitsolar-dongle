#!/usr/bin/env python3
"""Hardware probes for the two questions the docs can only hedge on.

Nothing here is shipped: this is a diagnostic for a machine that can actually
reach a dongle.  It uses the real client against the real device and prints
results meant to be pasted back into an issue or a chat.

Two experiments exist, because two claims in the documentation rest on
observations nobody has tested systematically:

``idle``
    Does the dongle keep a connection open for a client that says nothing?
    The client answers heartbeats only while a request is in flight, so
    between polls the pushes accumulate in the TCP buffer.  Whether the
    dongle tolerates that -- for minutes, or at all -- is unmeasured, and it
    is what the "keep intervals at or below 30 s" advice rests on.

    Runs each idle duration twice: once with the connection left alone
    (``--mode passive``, what the library does today) and once with a pump
    answering every heartbeat the moment it arrives (``--mode pumped``, what
    a background reader task would do).  If passive fails and pumped
    survives, the reader task is *needed*.  If both survive, it is a
    nice-to-have.  If both fail, something else is wrong.

``lockout``
    What triggers the ~75 s window in which the dongle refuses new
    connections?  The docs say "after a rapid reconnect"; the caveats admit
    that "any dropped connection" has not been ruled out.  The two readings
    cost very different amounts in Home Assistant, so it is worth pinning
    down.  Sweeps teardown style against reconnect gap.

Usage:
    # how long can it sit idle, and does answering help?
    python tools/hw_probe.py idle --host 192.168.1.50 --sn <DONGLE_SERIAL>

    # what makes it stop accepting connections?
    python tools/hw_probe.py lockout --host 192.168.1.50 --sn <DONGLE_SERIAL>

    # one pair at a time, so trials cannot contaminate each other
    python tools/hw_probe.py lockout --host ... --sn ... \\
        --teardown rst --gaps 20 --no-matrix

The serial is a credential: both session keys are derived from it.  It is
accepted as an argument or via ``--sn-file``, never hard-coded here.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sitsolar_dongle import AsyncDongleClient, DongleError  # noqa: E402
from sitsolar_dongle.framing import Frame, parse_header  # noqa: E402

# A heartbeat arrives roughly every 37 s in the captures this was written
# from.  Anything well beyond that without a byte is worth calling out.
HEARTBEAT_PERIOD = 37.0


def log(message: str) -> None:
    """Timestamped progress line."""
    print("[%7.1f] %s" % (time.monotonic() - START, message), flush=True)


START = time.monotonic()


def describe(exc: BaseException) -> str:
    """One line for an exception, type name included."""
    return "%s: %s" % (type(exc).__name__, exc)


def raw_socket(client: AsyncDongleClient) -> socket.socket | None:
    """The transport's socket, for teardown tricks the client cannot do."""
    writer = client._writer          # noqa: SLF001 - this is a probe script
    if writer is None:
        return None
    return writer.get_extra_info("socket")


def pending_frames(client: AsyncDongleClient) -> list[tuple[int, int]]:
    """Frames the client has buffered but not yet consumed.

    ``StreamReader`` reads eagerly off the transport, so the bytes pile up in
    its internal buffer rather than in the kernel socket buffer -- peeking at
    the socket with ``MSG_PEEK`` would see nothing.  Reading the buffer
    directly is the only way to watch the backlog grow without consuming it,
    which matters here because consuming it would answer the heartbeats and
    spoil the very measurement being taken.
    """
    reader = client._reader          # noqa: SLF001
    if reader is None:
        return []
    buf: bytearray = reader._buffer          # noqa: SLF001
    out: list[tuple[int, int]] = []
    offset = 0
    while len(buf) - offset >= 7:
        tid, protocol, length, _unit = parse_header(bytes(buf[offset:offset + 7]))
        total = 6 + length
        if len(buf) - offset < total:
            break                     # frame still incomplete
        out.append((tid, protocol))
        offset += total
    return out


async def pump_heartbeats(client: AsyncDongleClient, stop: asyncio.Event,
                          counter: dict[str, int]) -> None:
    """Answer heartbeats the moment they arrive, as a reader task would.

    Only safe while *no* request is in flight: it consumes whatever it reads.
    A non-push frame during a pure idle window would mean the dongle
    volunteered something unsolicited, which would itself be a finding -- it
    is counted rather than silently swallowed.

    The connection is dropped if the pump ever leaves mid-frame, which is the
    one way this can go wrong.  ``readexactly`` only removes bytes once it
    has a complete read, so cancelling it while waiting for a header is
    harmless -- but cancelling after the header is in and before the body is
    would leave the stream torn, and the poll that follows would then fail
    for reasons that have nothing to do with what is being measured.
    """
    reader = client._reader          # noqa: SLF001
    mid_frame = False
    try:
        while not stop.is_set() and reader is not None:
            try:
                header = await asyncio.wait_for(reader.readexactly(7), 0.5)
            except asyncio.TimeoutError:
                continue                        # no frame has started
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                counter["closed"] += 1
                return
            # a frame has started; from here an abort tears the stream
            mid_frame = True
            try:
                tid, protocol, length, unit = parse_header(header)
                envelope = await reader.readexactly(length - 1)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                counter["closed"] += 1
                return
            mid_frame = False
            frame = Frame(tid=tid, protocol=protocol, unit=unit,
                          envelope=envelope)
            if not frame.is_push:
                counter["unsolicited_non_push"] += 1
                continue
            counter["answered"] += 1
            try:
                await client._answer_push(frame)   # noqa: SLF001
            except (ConnectionError, OSError):
                counter["closed"] += 1
                return
    finally:
        if mid_frame:
            counter["desync"] += 1
            client._drop()                          # noqa: SLF001


async def idle_once(host: str, serial: str, port: int, seconds: float,
                    mode: str, timeout: float, sample: float) -> dict:
    """One idle run: connect, say nothing, then poll."""
    result: dict = {"seconds": seconds, "mode": mode}
    client = AsyncDongleClient(host, serial, port=port, timeout=timeout)
    stop = asyncio.Event()
    counter = {"answered": 0, "closed": 0, "unsolicited_non_push": 0,
               "desync": 0}
    task: asyncio.Task | None = None

    try:
        started = time.monotonic()
        info = await client.connect()
        result["connect_s"] = time.monotonic() - started
        result["serial"] = info.serial
        log("connected, idling %.0f s (%s)" % (seconds, mode))

        if mode == "pumped":
            task = asyncio.create_task(pump_heartbeats(client, stop, counter))

        peak = 0
        first_byte_at: float | None = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(sample, max(0.1, deadline - time.monotonic())))
            backlog = pending_frames(client)
            pushes = sum(1 for _tid, proto in backlog if (proto & 0xFF) == 1)
            peak = max(peak, len(backlog))
            if backlog and first_byte_at is None:
                first_byte_at = time.monotonic()
            log("  idle %5.0f s in: %d frame(s) buffered, %d heartbeat(s)"
                % (seconds - (deadline - time.monotonic()), len(backlog), pushes))

        result["peak_buffered"] = peak
        result["backlog"] = len(pending_frames(client))
        result["first_byte_s"] = (None if first_byte_at is None
                                  else first_byte_at - (deadline - seconds))
        result["connected_after_idle"] = client.connected

        if task is not None:
            stop.set()
            try:
                await asyncio.wait_for(task, 3.0)
            except asyncio.TimeoutError:
                task.cancel()
        result.update(counter)

        if not client.connected:
            result["verdict"] = "CONNECTION DROPPED DURING IDLE"
            return result

        started = time.monotonic()
        values = await client.poll()
        result["poll_s"] = time.monotonic() - started
        result["values"] = sum(1 for v in values.values()
                               if isinstance(v, dict) and "value" in v)
        result["verdict"] = "SURVIVED"
        return result
    except DongleError as exc:
        result["verdict"] = "FAILED: %s" % describe(exc)
        return result
    except BaseException as exc:                      # noqa: BLE001
        result["verdict"] = "UNEXPECTED: %s" % describe(exc)
        return result
    finally:
        if task is not None:
            stop.set()
            task.cancel()
        try:
            await client.close()
        except (DongleError, OSError):
            pass


def report_idle(rows: list[dict]) -> None:
    print()
    print("=" * 78)
    print("IDLE TOLERANCE")
    print("=" * 78)
    print("%8s %-8s %-9s %-9s %-8s %s"
          % ("idle s", "mode", "backlog", "answered", "poll s", "verdict"))
    print("-" * 78)
    for row in rows:
        print("%8.0f %-8s %-9s %-9s %-8s %s"
              % (row["seconds"], row["mode"],
                 row.get("backlog", "-"),
                 row.get("answered", "-"),
                 "%.2f" % row["poll_s"] if "poll_s" in row else "-",
                 row["verdict"]))
    print()
    anomalies = [(r, k) for r in rows for k in
                 ("unsolicited_non_push", "desync") if r.get(k)]
    for row, key in anomalies:
        print("!! %s: %.0f s (%s) -- %d"
              % (key, row["seconds"], row["mode"], row[key]))
    if anomalies:
        print()
    print("Reading the table:")
    print("  SURVIVED everywhere  -> the dongle tolerates silence; a reader")
    print("                           task is a nice-to-have, not a fix.")
    print("  passive fails, pumped survives -> the dongle requires heartbeats")
    print("                           to be answered; the reader task is needed")
    print("                           for any interval beyond the heartbeat period.")
    print("  both fail            -> the connection is being lost for another")
    print("                           reason; the '30 s' advice is not the fix.")


async def teardown(client: AsyncDongleClient, style: str,
                   abandoned: list[socket.socket]) -> str:
    """Close the connection in a chosen way; returns what was done."""
    sock = raw_socket(client)
    if style == "rst" and sock is not None:
        # SO_LINGER with a zero timeout makes close() emit RST instead of FIN:
        # the abrupt teardown of a crashed client, or a peer that vanished.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("ii", 1, 0))
        except OSError as exc:
            return "rst unavailable (%s); fell back to clean" % exc
        client._drop()                                  # noqa: SLF001
        try:
            sock.close()
        except OSError:
            pass
        return "RST"
    if style == "halfopen" and sock is not None:
        # Shutdown the read half and leave the socket open: the dongle can
        # still write into it and never sees a FIN, which is what a silently
        # vanished peer looks like.  Owned by the caller past this point.
        try:
            sock.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        client._drop()                                  # noqa: SLF001
        abandoned.append(sock)
        return "SHUT_RD (socket left open)"
    await client.close()
    return "FIN"


async def lockout_once(host: str, serial: str, port: int, style: str,
                       gap: float, timeout: float, cooldown: float) -> dict:
    """One trial: connect, tear down, wait *gap*, try to reconnect."""
    result: dict = {"style": style, "gap": gap}
    abandoned: list[socket.socket] = []
    client = AsyncDongleClient(host, serial, port=port, timeout=timeout)
    try:
        log("cooldown %.0f s so the previous trial cannot contaminate this one"
            % cooldown)
        await asyncio.sleep(cooldown)

        try:
            await client.connect()
        except DongleError as exc:
            result["first_connect"] = "FAILED: %s" % describe(exc)
            log("could not establish a session to begin the trial: %s"
                % result["first_connect"])
            return result
        result["first_connect"] = "ok"
        log("session up; tearing down as %s" % style)

        result["teardown"] = await teardown(client, style, abandoned)
        client = AsyncDongleClient(host, serial, port=port, timeout=timeout)
        await asyncio.sleep(gap)

        started = time.monotonic()
        try:
            await client.connect()
            result["reconnect"] = ("ACCEPTED after %.1f s"
                                   % (time.monotonic() - started))
        except DongleError as exc:
            result["reconnect"] = "REFUSED: %s" % describe(exc)
        return result
    finally:
        try:
            await client.close()
        except (DongleError, OSError):
            pass
        # a halfopen trial deliberately abandons a live socket; close it here
        # rather than leaving it for the process to reap
        for sock in abandoned:
            try:
                sock.close()
            except OSError:
                pass


def report_lockout(rows: list[dict]) -> None:
    print()
    print("=" * 78)
    print("RECONNECT LOCKOUT")
    print("=" * 78)
    print("%-10s %-6s %s" % ("teardown", "gap s", "reconnect"))
    print("-" * 78)
    for row in rows:
        print("%-10s %-6.0f %s" % (row.get("teardown", row["style"]), row["gap"],
                                   row.get("reconnect", "not run")))
    print()
    print("Reading the table:")
    print("  REFUSED at every gap for both teardowns  -> the lockout follows any")
    print("      session, so a clean close costs 75 s too.  Assume the pessimistic")
    print("      reading; back off after every failure.")
    print("  accepted after clean, refused after rst  -> the lockout follows an")
    print("      unclean teardown specifically.  A well-behaved client can close")
    print("      and reconnect freely, which is worth documenting.")
    print("  REFUSED only at short gaps               -> the lockout is about")
    print("      reconnection speed, as the docs currently say.  Anything past")
    print("      the window reconnects fine.")
    print("  accepted everywhere                       -> no lockout observed;")
    print("      the ~75 s figure may be specific to a different firmware.")


def load_serial(args: argparse.Namespace) -> str:
    """The serial, from ``--sn-file`` if given, else ``--sn``."""
    if args.sn_file:
        return Path(args.sn_file).read_text(encoding="utf-8").strip()
    if args.sn:
        return args.sn
    raise SystemExit("give the serial with --sn or --sn-file")


def build_parser() -> argparse.ArgumentParser:
    """Options shared by every subcommand, accepted on either side of it.

    They are declared with ``SUPPRESS`` so that an option the subparser does
    not see leaves the attribute alone rather than resetting it to the
    default.  Without that, ``hw_probe.py --host X idle`` would parse --host
    on the parent and then have the subparser overwrite it with None, which
    is the argparse subparser trap and the reason the first version of this
    script only accepted ``idle --host X``.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", default=argparse.SUPPRESS,
                        help="dongle address")
    common.add_argument("--sn", default=argparse.SUPPRESS,
                        help="dongle serial (a credential: keep it out of "
                             "shell history and transcripts)")
    common.add_argument("--sn-file", default=argparse.SUPPRESS,
                        help="read the serial from this file instead")
    common.add_argument("--port", type=int, default=argparse.SUPPRESS)
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                        help="per-request timeout; the idle probes need it "
                             "well above the heartbeat period (default 15)")

    parser = argparse.ArgumentParser(
        parents=[common], description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    idle = sub.add_parser("idle", parents=[common],
                          help="how long can the connection sit unused")
    idle.add_argument("--seconds", default="60,300,900",
                      help="comma separated idle durations, seconds")
    idle.add_argument("--mode", choices=["passive", "pumped", "both"],
                      default="both")
    idle.add_argument("--sample", type=float, default=5.0,
                      help="seconds between backlog samples")

    lock = sub.add_parser("lockout", parents=[common],
                          help="what triggers the reconnect lockout")
    lock.add_argument("--teardown", default="clean,rst",
                      help="comma separated: clean, rst, halfopen")
    lock.add_argument("--gaps", default="5,20,80",
                      help="comma separated seconds to wait before reconnecting")
    lock.add_argument("--cooldown", type=float, default=90.0,
                      help="seconds to wait before each trial, so an earlier "
                           "trial's lockout cannot masquerade as this one's")
    lock.add_argument("--no-matrix", action="store_true",
                      help="run only the first teardown/gap pair; run the "
                           "others in separate invocations to keep trials "
                           "fully isolated")
    return parser


def apply_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in what SUPPRESS left unset.

    Done here rather than per-argument in ``main()`` so a new shared option
    cannot be forgotten: every attribute below is guaranteed present once this
    returns, which keeps ``args.port`` from becoming an AttributeError waiting
    to happen.
    """
    fallbacks = {"host": None, "sn": None, "sn_file": None,
                 "port": 7078, "timeout": 15.0}
    for name, value in fallbacks.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    return args


def main() -> int:
    parser = build_parser()
    args = apply_defaults(parser.parse_args())

    if not args.host:
        parser.error("--host is required (before or after the subcommand)")
    serial = load_serial(args)
    modes = ["passive", "pumped"] if args.mode == "both" else [args.mode]

    if args.command == "idle":
        durations = [float(x) for x in args.seconds.split(",") if x.strip()]
        print("idle probe: %d run(s), total ~%.0f s\n"
              % (len(durations) * len(modes),
                 sum(durations) * len(modes)))
        rows = []
        for seconds in durations:
            for mode in modes:
                rows.append(asyncio.run(idle_once(
                    args.host, serial, args.port, seconds, mode,
                    args.timeout, args.sample)))
        report_idle(rows)
        return 0

    teardowns = [t.strip() for t in args.teardown.split(",") if t.strip()]
    gaps = [float(g) for g in args.gaps.split(",") if g.strip()]
    if args.no_matrix:
        pairs = [(teardowns[0], gaps[0])]
    else:
        pairs = [(t, g) for t in teardowns for g in gaps]
    estimate = sum(args.cooldown + g + 20 for _t, g in pairs)
    print("lockout probe: %d trial(s), roughly %.0f min of wall clock\n"
          % (len(pairs), estimate / 60))
    print("Make sure nothing else is talking to the dongle during this run.\n")

    rows = []
    for style, gap in pairs:
        log("trial: teardown=%s gap=%.0f s" % (style, gap))
        rows.append(asyncio.run(lockout_once(
            args.host, serial, args.port, style, gap, args.timeout,
            args.cooldown)))
    report_lockout(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
