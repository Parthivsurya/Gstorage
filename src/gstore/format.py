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


def human_duration(seconds: float | None) -> str:
    """Rough human-readable duration, for a progress ETA.

    Deliberately coarse: an upload estimate that reads "1h 12m" is honest
    about its own precision in a way that "1:12:09" is not, and it stops the
    label flickering every second.
    """
    if seconds is None or seconds != seconds or seconds < 0:  # None/NaN/negative
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{max(seconds, 1)}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if secs < 10 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


def human_rate(bytes_per_second: float | None) -> str:
    """Transfer speed, e.g. "2.4 MB/s"."""
    if not bytes_per_second or bytes_per_second < 0:
        return "—"
    return f"{human_bytes(int(bytes_per_second))}/s"
