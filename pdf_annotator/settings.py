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


def load():
    data = dict(DEFAULTS)
    try:
        with open(_PATH) as fh:
            stored = json.load(fh)
        for k in DEFAULTS:
            if k in stored:
                data[k] = stored[k]
    except (OSError, ValueError):
        pass
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
