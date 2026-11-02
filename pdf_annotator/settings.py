"""Tiny JSON settings store (~/.config/pdf-annotator/settings.json)."""

import json
import os

from gi.repository import GLib

_PATH = os.path.join(GLib.get_user_config_dir(), "pdf-annotator", "settings.json")

DEFAULTS = {
    "tool": "pen",
    "color": [0.07, 0.07, 0.07],
    "width": 1.5,
    "text_size": 14.0,
    "sidebar": False,
}


def _number(lo, hi):
    def check(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            return float(v)
        return None
    return check


def _color(v):
    if isinstance(v, list) and len(v) == 3 and all(_number(0, 1)(c) is not None for c in v):
        return [float(c) for c in v]
    return None


# Each returns the cleaned value, or None if the stored value is unusable.
_VALIDATORS = {
    "tool": lambda v: v if v in ("select", "pen", "text", "eraser") else None,
    "color": _color,
    "width": _number(0.5, 8),
    "text_size": _number(6, 72),
    "sidebar": lambda v: v if isinstance(v, bool) else None,
}


def load():
    """Saved preferences, with defaults for anything missing or invalid (a
    damaged or hand-edited file must never stop the app from starting)."""
    data = dict(DEFAULTS)
    try:
        with open(_PATH) as fh:
            stored = json.load(fh)
    except (OSError, ValueError):
        return data
    if not isinstance(stored, dict):
        return data
    for key, check in _VALIDATORS.items():
        if key in stored:
            value = check(stored[key])
            if value is not None:
                data[key] = value
    return data


_pending = None
_timeout = 0


def save(data):
    """Save soon. Sliders call this on every tick, so writes are batched;
    call flush() before exiting."""
    global _pending, _timeout
    _pending = dict(data)
    if not _timeout:
        _timeout = GLib.timeout_add(500, _flush_soon)


def _flush_soon():
    global _timeout
    _timeout = 0
    flush()
    return False


def flush():
    global _pending, _timeout
    if _timeout:
        GLib.source_remove(_timeout)
        _timeout = 0
    if _pending is not None:
        _write(_pending)
        _pending = None


def _write(data):
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, _PATH)
    except OSError:
        pass
