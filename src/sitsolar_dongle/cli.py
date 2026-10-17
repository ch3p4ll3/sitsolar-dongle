"""Command line interface.

Examples::

    # one shot, human readable
    sitsolar-dongle 192.168.1.50 --sn <DONGLE_SERIAL>

    # machine readable
    sitsolar-dongle 192.168.1.50 --sn ... --json

    # follow forever, one sample every 15 s
    sitsolar-dongle 192.168.1.50 --sn ... --interval 15

    # raw reads, bypassing the register map
    sitsolar-dongle 192.168.1.50 --sn ... --read 31002:2 --read 30100:1

    # just show the register map
    sitsolar-dongle --list
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Sequence

from . import __version__
from .client import DEFAULT_PORT, DongleClient, DongleError
from .framing import FC_READ_HOLDING, FC_READ_INPUT, DongleNoDataError, \
    ModbusError
from .registers import REGISTERS, SECTION_ORDER, blocks


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sitsolar-dongle",
        description="Read the read-only registers of a SiNeng / FuturaPulse "
                    "inverter through its WiFi dongle (TCP port 7078).",
    )
    p.add_argument("host", nargs="?",
                   help="dongle IP address or hostname")
    p.add_argument("--sn", metavar="SERIAL",
                   help="dongle serial number; the AES and HMAC keys are "
                        "derived from it")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="TCP port (default: %(default)s)")
    p.add_argument("--slave", type=int, default=1,
                   help="Modbus slave address to read: 0 is the dongle "
                        "itself, 1 is the inverter (default: %(default)s)")
    p.add_argument("--fc", choices=("04", "03", "4", "3"), default="04",
                   help="function code for reads: 04 reads input registers "
                        "(the 3xxxx block, per the protocol document), 03 "
                        "reads holding registers (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=10.0,
                   help="socket timeout in seconds (default: %(default)s)")

    g = p.add_argument_group("what to read")
    g.add_argument("--read", action="append", default=[], metavar="ADDR:COUNT",
                   help="raw register read, can be repeated; skips the "
                        "handshake parse and prints words only")
    g.add_argument("--section", action="append", default=[],
                   help="only poll this protocol document section "
                        "(e.g. \"AC_side\"), can be repeated")
    g.add_argument("--key", action="append", default=[],
                   help="only poll these register keys, can be repeated")

    o = p.add_argument_group("output")
    o.add_argument("--json", action="store_true",
                   help="emit a JSON object instead of a table")
    o.add_argument("--raw", action="store_true",
                   help="show raw register words alongside decoded values")
    o.add_argument("--list", action="store_true",
                   help="list the register map and exit")
    o.add_argument("--errors", choices=("skip", "raise"), default="skip",
                   help="what to do when a range answers with a Modbus "
                        "exception (default: %(default)s)")

    s = p.add_argument_group("sampling")
    s.add_argument("--interval", type=float, default=0.0,
                   help="poll forever, sleeping this many seconds between "
                        "samples (default: one shot)")
    s.add_argument("--include-device-info", action="store_true",
                   help="also send CallAll and include the dongle and "
                        "inverter identity JSON in the output")

    p.add_argument("--version", action="version",
                   version="%(prog)s " + __version__)
    return p


def _print_map() -> int:
    current = None
    for reg in REGISTERS:
        if reg.section != current:
            current = reg.section
            print("\n" + current)
            print("-" * len(current))
        extra = ""
        if reg.scale != 1.0:
            extra = " scale=%g" % reg.scale
        if reg.unit:
            extra += " [%s]" % reg.unit
        print("  %-7s %-42s %-7s%s" % (reg.span, reg.name, reg.dtype, extra))
        if reg.notes:
            print("  %-7s ^-- note: %s" % ("", reg.notes))
    print("\n%d values in %d read ranges" % (len(REGISTERS), len(blocks())))
    return 0


def _selected(args: argparse.Namespace):
    """Registers to poll after applying --section / --key filters."""
    regs = list(REGISTERS)
    if args.section:
        wanted = [s for s in SECTION_ORDER if s in args.section]
        unknown = [s for s in args.section if s not in wanted]
        if unknown:
            raise SystemExit("unknown section(s): %s\nknown: %s"
                             % (", ".join(unknown), "; ".join(SECTION_ORDER)))
        regs = [r for r in regs if r.section in wanted]
    if args.key:
        known = {r.key for r in regs}
        unknown = [k for k in args.key if k not in known]
        if unknown:
            raise SystemExit("unknown key(s): %s" % ", ".join(unknown))
        regs = [r for r in regs if r.key in args.key]
    if not regs:
        raise SystemExit("the filters selected no registers")
    return regs


def _raw_reads(dongle: DongleClient, specs: Sequence[str],
               fc: int) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        try:
            addr_s, count_s = spec.split(":", 1)
            addr, count = int(addr_s, 0), int(count_s, 0)
        except ValueError:
            raise SystemExit("--read expects ADDR:COUNT, got %r" % spec)
        if not 1 <= count <= 50:
            raise SystemExit("--read COUNT must be 1..50, got %d" % count)
        try:
            words = dongle.read(addr, count, fc=fc)
        except (ModbusError, DongleNoDataError) as exc:
            # Keep going: a probe over several ranges is useless if one
            # refused range discards every reading made before it.
            out.append({"address": addr, "count": count, "error": str(exc)})
            continue
        out.append({
            "address": addr,
            "count": count,
            "words": list(words),
            "hex": " ".join("%04X" % w for w in words),
        })
    return out


def _pretty(data: dict[str, Any], *, raw: bool) -> None:
    info = data.get("_dongle")
    if info:
        print("dongle   %s %s  sn=%s  time=%s"
              % (info["factory"], info["model"], info["serial"], info["time"]))

    width = max((len(k) for k, v in data.items()
                 if not k.startswith("_") and isinstance(v, dict)), default=10)
    reads = data.get("reads")
    if reads is not None:
        # raw --read mode: a list of {address, count, hex|error}, not a map
        for item in reads:
            outcome = item.get("error") or item.get("hex", "")
            print("  %d:%d  %s" % (item["address"], item["count"], outcome))
        return
    section = None
    for key, value in data.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        if value.get("section") != section:
            section = value.get("section")
            print("\n" + str(section))
        if "error" in value:
            print("  %-*s  %s  (%s)" % (width, key, value["address"],
                                        value["error"]))
            continue
        shown = value["value"]
        if value.get("label"):
            shown = "%s  (%s)" % (shown, value["label"])
        if value.get("active_bits") is not None and not value["active_bits"]:
            shown = "%s  (no bits set)" % shown
        line = "  %-*s  %8s  %-6s %s" % (
            width, key, value["address"], shown, value.get("unit", ""))
        if raw:
            extra = value.get("raw_signed")
            suffix = str(value["raw"])
            if extra is not None and extra != value["raw"]:
                suffix = "%s (%d)" % (suffix, extra)
            line += "  raw=%s" % suffix
        print(line)

    for note in ("_unreadable_ranges", "_devices"):
        if note in data:
            print("\n%s:" % note.lstrip("_"))
            print("  " + json.dumps(data[note], indent=2).replace("\n", "\n  "))


def _sample(dongle: DongleClient, args: argparse.Namespace,
            regs) -> dict[str, Any]:
    """One poll cycle: either raw --read specs or the decoded register map."""
    fc = FC_READ_INPUT if args.fc in ("04", "4") else FC_READ_HOLDING

    if args.read:
        # raw mode: ignore the register map entirely
        return {"reads": _raw_reads(dongle, args.read, fc)}

    data = dongle.poll(slave=args.slave, fc=fc, on_error=args.errors)

    # keep only the requested subset of registers
    wanted = {r.key for r in regs}
    filtered = {k: v for k, v in data.items() if k in wanted}
    if "_unreadable_ranges" in data:
        filtered["_unreadable_ranges"] = data["_unreadable_ranges"]

    if args.include_device_info:
        try:
            filtered["_devices"] = dongle.call_all()
        except (DongleError, OSError) as exc:
            filtered["_devices"] = {"error": str(exc)}
    return filtered


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.list:
        return _print_map()
    if not args.host:
        raise SystemExit("a host is required unless --list is given")
    if not args.sn:
        raise SystemExit("--sn is required: the keys are derived from the "
                         "dongle serial number")

    regs = _selected(args)

    try:
        with DongleClient(args.host, args.sn, port=args.port,
                          timeout=args.timeout) as dongle:
            while True:
                sample = _sample(dongle, args, regs)
                if dongle.info and not args.read:
                    info = dongle.info
                    sample["_dongle"] = {
                        "factory": info.factory,
                        "model": info.model,
                        "serial": info.serial,
                        "version": info.version,
                        "time": info.time,
                    }
                if args.json:
                    print(json.dumps(sample, indent=2, default=str))
                else:
                    _pretty(sample, raw=args.raw)
                if args.interval <= 0:
                    break
                sys.stdout.flush()
                time.sleep(args.interval)
    except (ModbusError, DongleNoDataError) as exc:
        print("modbus error: %s" % exc, file=sys.stderr)
        return 3
    except (DongleError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
