#!/usr/bin/env python3
"""Hardware probes for the two questions the docs can only hedge on.

Nothing here is shipped: this is a diagnostic for a machine that can actually
reach a dongle.  It uses the real client against the real device and prints
results meant to be pasted back into an issue or a chat.

Two experiments exist, because two claims in the documentation rest on
observations nobody has tested systematically:

``idle``
    Does the dongle keep a connection open for a client that answers
    heartbeats late, or only for one that answers them at once?

    The probe owns its transport rather than using ``AsyncDongleClient``,
    because that client answers heartbeats from a background reader of its
    own -- using it here would only ever re-demonstrate that the reader
    works.  Three answer policies are swept instead:

    ``--mode passive``
        Never answer.  The dongle is measured to drop this after roughly five
        unanswered pushes.
    ``--mode immediate``
        Answer the instant one arrives.  Measured to survive ten minutes.
    ``--mode burst``
        Queue them and drain every ``--burst`` seconds -- what the blocking
        client does in effect, and the one case no measurement covers.
        ``passive`` and ``immediate`` bracket the answer, so running burst
        between them is what would settle whether a *late* reply counts.

    ``--mode both`` is passive+immediate.

``lockout``
    What triggers the ~75 s window in which the dongle refuses new
    connections?  The docs say "after a rapid reconnect"; the caveats admit
    that "any dropped connection" has not been ruled out.  The two readings
    cost very different amounts in Home Assistant, so it is worth pinning
    down.  Sweeps teardown style against reconnect gap.

Usage:
    # how long can it sit idle, and does answering at once help?
    python tools/hw_probe.py --host 192.168.1.50 --sn-file ./sn idle

    # does a *late* answer count?  drain every 30 s, between the two
    # known outcomes
    python tools/hw_probe.py --host 192.168.1.50 --sn-file ./sn idle \\
        --mode burst --burst 30 --seconds 300

    # what makes it stop accepting connections?
    python tools/hw_probe.py --host 192.168.1.50 --sn-file ./sn lockout

    # one pair at a time, so trials cannot contaminate each other
    python tools/hw_probe.py --host ... --sn-file ./sn lockout \\
        --teardown rst --gaps 20 --no-matrix

The serial is a credential: both session keys are derived from it.  It is
accepted as an argument or via ``--sn-file``, never hard-coded here -- and
``--sn`` lands in your shell history, so prefer the file.
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
from sitsolar_dongle.crypto import derive_keys, open_envelope  # noqa: E402
from sitsolar_dongle.framing import (  # noqa: E402
    Frame,
    PROTO_PUSH,
    build_read,
    parse_header,
    parse_read_reply,
)
from sitsolar_dongle.session import (  # noqa: E402
    CMD_ACCREDIT,
    CMD_BEFORE_ACCREDIT,
    CMD_HEARTBEAT,
    DEFAULT_PORT,
    PROTO_HEARTBEAT_REPLY,
    parse_dongle_info,
    seal_frame,
)

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


def raw_socket(writer) -> socket.socket | None:
    """The transport's socket, for teardown tricks the session cannot do."""
    if writer is None:
        return None
    return writer.get_extra_info("socket")


def pending_frames(reader: asyncio.StreamReader) -> list[tuple[int, int]]:
    """Frames that have arrived but not yet been consumed.

    ``StreamReader`` reads eagerly off the transport, so the bytes pile up in
    its internal buffer rather than in the kernel socket buffer -- peeking at
    the socket with ``MSG_PEEK`` would see nothing.  Reading the buffer
    directly is the only way to watch the backlog grow without consuming it,
    which matters here because consuming it would answer the heartbeats and
    spoil the very measurement being taken.
    """
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


class RawSession:
    """A connection the probe drives itself, frame by frame.

    The library's own client answers heartbeats from a background reader, so
    using it here would make the experiment circular: it could only ever show
    that answering heartbeats works.  Owning the transport is what lets the
    answer policy be the thing under test instead.
    """

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, keys) -> None:
        self.reader = reader
        self.writer = writer
        self.keys = keys
        self._tid = 0

    @classmethod
    async def open(cls, host: str, port: int, serial: str,
                   timeout: float) -> RawSession:
        """Connect and run the same handshake the library does."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
        session = cls(reader, writer, derive_keys(serial))
        try:
            await session._handshake(timeout)
        except (DongleError, OSError, asyncio.TimeoutError):
            writer.close()
            raise
        return session

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) & 0xFFFF
        return self._tid

    async def send(self, payload: bytes) -> int:
        """Seal and write one command; returns the transaction id used."""
        tid = self._next_tid()
        self.writer.write(seal_frame(payload, self.keys, tid=tid))
        await self.writer.drain()
        return tid

    async def read_frame(self, timeout: float) -> Frame:
        """One whole frame, or raise.  ``TimeoutError`` means nothing came."""
        header = await asyncio.wait_for(self.reader.readexactly(7), timeout)
        tid, protocol, length, unit = parse_header(header)
        envelope = await self.reader.readexactly(length - 1)
        return Frame(tid=tid, protocol=protocol, unit=unit, envelope=envelope)

    async def payload_of(self, frame: Frame) -> bytes:
        return open_envelope(frame.envelope, self.keys)

    async def _handshake(self, timeout: float) -> None:
        await self.send(CMD_BEFORE_ACCREDIT)
        parse_dongle_info(await self.payload_of(await self.read_frame(timeout)))
        await self.send(CMD_ACCREDIT)
        payload = await self.payload_of(await self.read_frame(timeout))
        if len(payload) > 2 and payload[2] != 0:
            raise DongleError("Accredit refused with result 0x%02X"
                              % payload[2])

    async def answer(self, frame: Frame) -> None:
        """Reply to a heartbeat exactly as the official app does.

        The push's transaction id is reused, so the dongle can tell an answer
        from a fresh command -- answering with a new tid would be a different
        experiment from the one the app performs.
        """
        stamp = struct.pack(">I", int(time.time()) & 0xFFFFFFFF)
        self.writer.write(seal_frame(
            CMD_HEARTBEAT + stamp, self.keys, tid=frame.tid,
            protocol=PROTO_HEARTBEAT_REPLY))
        await self.writer.drain()

    async def poll(self, timeout: float) -> int:
        """One register block, as proof the link still works.  Returns words."""
        await self.send(build_read(1, 31000, 40))
        reply = parse_read_reply(
            await self.payload_of(await self.read_frame(timeout)))
        return len(reply.registers)

    async def close(self) -> None:
        try:
            self.writer.close()
        except OSError:
            pass


async def answer_pushes(session: RawSession, policy: str, period: float,
                        stop: asyncio.Event, counter: dict[str, int]) -> None:
    """Consume incoming frames and answer heartbeats according to *policy*.

    ``passive``
        Never answer.  This is the failure mode the docs used to describe and
        the one the dongle is measured to punish.
    ``immediate``
        Answer the instant one arrives -- what a background reader does, and
        what the official app does.
    ``burst``
        Queue them and drain every *period* seconds.  This is what the
        blocking client does in effect, and it is the case the hardware
        measurement has never covered: whether a *late* answer counts the
        same as a prompt one.  Run it with ``--burst`` set near the heartbeat
        period to bracket the answer.

    Only safe while *no* request is in flight, because it consumes whatever it
    reads.  A non-push frame during a pure idle window would mean the dongle
    volunteered something unsolicited, which would itself be a finding -- it
    is counted rather than silently swallowed.

    The connection is dropped if this ever leaves mid-frame.  ``readexactly``
    only removes bytes once it has a complete read, so cancelling it while
    waiting for a header is harmless -- but cancelling after the header is in
    and before the body would leave the stream torn, and the poll that follows
    would then fail for reasons that have nothing to do with what is being
    measured.
    """
    reader = session.reader
    queue: list[Frame] = []
    mid_frame = False
    next_drain = time.monotonic() + period
    try:
        while not stop.is_set():
            try:
                header = await asyncio.wait_for(reader.readexactly(7), 0.25)
            except asyncio.TimeoutError:
                header = None
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                counter["closed"] += 1
                return

            if header is not None:
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
                else:
                    counter["queued"] += 1
                    if policy == "passive":
                        pass                       # the point of the mode
                    elif policy == "immediate":
                        counter["answered"] += 1
                        await _try_answer(session, frame, counter)
                    else:                          # burst
                        queue.append(frame)

            if policy == "burst" and time.monotonic() >= next_drain:
                next_drain = time.monotonic() + period
                for queued in queue:
                    counter["answered"] += 1
                    await _try_answer(session, queued, counter)
                queue.clear()
    finally:
        if mid_frame:
            counter["desync"] += 1
            await session.close()
        elif policy == "burst" and queue:
            # a real client answers what it queued at its next poll, so
            # draining here keeps the comparison honest rather than
            # crediting burst mode with answers it never got to send
            for queued in queue:
                counter["answered"] += 1
                await _try_answer(session, queued, counter)


async def _try_answer(session: RawSession, frame: Frame,
                      counter: dict[str, int]) -> None:
    try:
        await session.answer(frame)
    except (ConnectionError, OSError):
        counter["closed"] += 1


async def idle_once(host: str, serial: str, port: int, seconds: float,
                    mode: str, timeout: float, sample: float,
                    burst: float) -> dict:
    """One idle run: connect, say nothing, then poll."""
    result: dict = {"seconds": seconds, "mode": mode}
    stop = asyncio.Event()
    counter = {"answered": 0, "closed": 0, "queued": 0,
               "unsolicited_non_push": 0, "desync": 0}
    session: RawSession | None = None
    task: asyncio.Task | None = None

    try:
        started = time.monotonic()
        session = await RawSession.open(host, port, serial, timeout)
        result["connect_s"] = time.monotonic() - started
        log("connected, idling %.0f s (%s)" % (seconds, mode))

        task = asyncio.create_task(answer_pushes(
            session, mode, burst, stop, counter))

        peak = 0
        first_byte_at: float | None = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(min(sample, max(0.1, deadline - time.monotonic())))
            backlog = pending_frames(session.reader)
            pushes = sum(1 for _tid, proto in backlog
                         if (proto & 0xFF) == (PROTO_PUSH & 0xFF))
            peak = max(peak, len(backlog))
            if backlog and first_byte_at is None:
                first_byte_at = time.monotonic()
            log("  idle %5.0f s in: %d frame(s) buffered, %d heartbeat(s)"
                % (seconds - (deadline - time.monotonic()), len(backlog), pushes))

        result["peak_buffered"] = peak
        result["backlog"] = len(pending_frames(session.reader))
        result["first_byte_s"] = (None if first_byte_at is None
                                  else first_byte_at - (deadline - seconds))

        stop.set()
        try:
            await asyncio.wait_for(task, 3.0)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        result.update(counter)

        if counter["closed"]:
            result["verdict"] = "CONNECTION DROPPED DURING IDLE"
            return result

        started = time.monotonic()
        result["values"] = await session.poll(timeout)
        result["poll_s"] = time.monotonic() - started
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
        if session is not None:
            await session.close()


def report_idle(rows: list[dict]) -> None:
    print()
    print("=" * 78)
    print("IDLE TOLERANCE")
    print("=" * 78)
    print("%8s %-9s %-8s %-8s %-8s %-8s %s"
          % ("idle s", "mode", "queued", "answer", "backlog", "poll s",
             "verdict"))
    print("-" * 78)
    for row in rows:
        print("%8.0f %-9s %-8s %-8s %-8s %-8s %s"
              % (row["seconds"], row["mode"],
                 row.get("queued", "-"),
                 row.get("answered", "-"),
                 row.get("backlog", "-"),
                 "%.2f" % row["poll_s"] if "poll_s" in row else "-",
                 row["verdict"]))
    print()
    print("queued = pushes seen, answered = replies written.  A passive run")
    print("should show a growing queue and zero answers; if the dongle drops")
    print("it at some queue depth, that depth is how many it tolerates.")
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
    sock = raw_socket(client._writer)      # noqa: SLF001 - this is a probe
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
    idle.add_argument("--mode", choices=["passive", "immediate", "burst",
                                         "both"],
                      default="both",
                      help="answer policy: never answer, answer at once, or "
                           "queue and drain every --burst seconds. "
                           "'both' is passive+immediate")
    idle.add_argument("--burst", type=float, default=HEARTBEAT_PERIOD,
                      help="seconds between drains in burst mode; try a "
                           "value near the heartbeat period to bracket "
                           "whether late answers count")
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
                 "port": DEFAULT_PORT, "timeout": 15.0}
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

    if args.command == "idle":
        # --mode belongs to this subcommand alone, so it is read here rather
        # than next to the shared options: lockout does not define it
        modes = (["passive", "immediate"] if args.mode == "both"
                 else [args.mode])
        durations = [float(x) for x in args.seconds.split(",") if x.strip()]
        print("idle probe: %d run(s), total ~%.0f s\n"
              % (len(durations) * len(modes),
                 sum(durations) * len(modes)))
        rows = []
        for seconds in durations:
            for mode in modes:
                rows.append(asyncio.run(idle_once(
                    args.host, serial, args.port, seconds, mode,
                    args.timeout, args.sample, args.burst)))
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
