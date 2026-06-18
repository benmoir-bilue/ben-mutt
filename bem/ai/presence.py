"""Is Ben at the keyboard? Lets Mutt be live and brisk when he's present, and
quiet/slow when he's away (the away-mode behaviour itself lands in a later phase).

macOS only for now: reads HID idle time from ``ioreg``. On any other platform, or
if the reading fails, we assume present — better to stay live than to silently go
quiet on a machine we can't measure.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Optional

# Under this many seconds since the last key/mouse input, treat Ben as present.
AWAY_IDLE_SECS = 90.0


def _parse_idle_seconds(ioreg_output: str) -> Optional[float]:
    """Pull HIDIdleTime (nanoseconds) out of `ioreg -c IOHIDSystem` → seconds."""
    for line in ioreg_output.splitlines():
        if "HIDIdleTime" in line:
            try:
                ns = int(line.rsplit("=", 1)[1].strip())
            except (ValueError, IndexError):
                return None
            return ns / 1_000_000_000
    return None


def idle_seconds() -> Optional[float]:
    """Seconds since the last HID input, or None if it can't be determined."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_idle_seconds(out)


def is_present(idle: Optional[float] = None) -> bool:
    """True when Ben seems to be at the keyboard. Unknown (non-mac / no data)
    defaults to present. Pass `idle` to bypass the system probe (for tests)."""
    if idle is None:
        idle = idle_seconds()
    if idle is None:
        return True
    return idle < AWAY_IDLE_SECS
