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


def save(data):
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, _PATH)
    except OSError:
        pass
