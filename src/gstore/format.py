"""Small formatting helpers for the UI."""

from __future__ import annotations

_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


def human_bytes(n: int | None) -> str:
    """Human-readable size (base-1024). None renders as an infinity sign."""
    if n is None:
        return "∞"
    value = float(n)
    for unit in _UNITS:
        if value < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"  # unreachable; keeps type checkers happy
