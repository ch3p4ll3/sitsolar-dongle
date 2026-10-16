"""Turn raw 16-bit register words into human-meaningful values."""

from __future__ import annotations

from typing import Any

from .registers import ASCII, BITFIELD, S16, S32, U16, U32, Register

__all__ = ["decode", "Reading"]


def _signed(value: int, bits: int) -> int:
    """Interpret *value* as a two's-complement integer of *bits* bits."""
    sign = 1 << (bits - 1)
    return value - (1 << bits) if value & sign else value


def _combine(words: tuple[int, ...]) -> int:
    """Join register words big-endian into one integer."""
    out = 0
    for word in words:
        out = (out << 16) | word
    return out


def _ascii(words: tuple[int, ...]) -> str:
    raw = b"".join(word.to_bytes(2, "big") for word in words)
    return raw.decode("ascii", "replace").rstrip("\x00 ").strip()


def decode(reg: Register, words: tuple[int, ...]) -> dict[str, Any]:
    """Decode the *words* belonging to *reg*.

    Returns a dict with ``address``, ``raw``, and -- depending on the data
    type -- ``value`` plus ``unit``, ``label`` or ``active_bits``.
    """
    if len(words) != reg.length:
        raise ValueError("%s needs %d registers, got %d"
                         % (reg.key, reg.length, len(words)))

    out: dict[str, Any] = {
        "address": reg.span,
        "name": reg.name,
        "section": reg.section,
    }

    if reg.dtype == ASCII:
        out["raw"] = " ".join("%04X" % w for w in words)
        out["value"] = _ascii(words)
        return out

    if reg.dtype == U32 or reg.dtype == S32:
        raw = _combine(words)
        out["raw"] = raw
        if reg.dtype == S32:
            raw = _signed(raw, 32)
            out["raw_signed"] = raw
    elif reg.dtype == S16:
        raw = _signed(words[0], 16)
        out["raw"] = words[0]
        out["raw_signed"] = raw
    else:  # U16 / BITFIELD
        raw = words[0]
        out["raw"] = raw

    if reg.scale != 1.0:
        out["value"] = round(raw * reg.scale, 6)
        out["unit"] = reg.unit
    else:
        out["value"] = raw
        if reg.unit:
            out["unit"] = reg.unit

    if reg.enum is not None:
        out["label"] = reg.enum.get(raw, "unknown (%d)" % raw)
    if reg.bits is not None:
        out["active_bits"] = [
            name for bit, name in sorted(reg.bits.items()) if raw & (1 << bit)
        ]

    if reg.notes:
        out["notes"] = reg.notes
    return out
