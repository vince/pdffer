"""Plain-language error messages.

Errors shown to people say what happened and what to do, using the file
names they know. The technical cause is kept separately for the
"Technical Details" section of the error dialog and for the log.
"""

import errno
import logging
import os
import re

from gi.repository import Gio, GLib

log = logging.getLogger("pdf_annotator")


class UserError(Exception):
    """An error whose message is meant for people.

    `details` holds the technical cause, if any.
    """

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details


def name(path):
    return f"“{os.path.basename(path) or path}”"


def folder(path):
    d = os.path.dirname(os.path.abspath(path)) or "/"
    home = os.path.expanduser("~")
    if d == home or d.startswith(home + os.sep):
        d = "~" + d[len(home):]
    return f"“{d}”"


def technical(exc):
    """A one-line technical description of an exception."""
    if isinstance(exc, GLib.Error):
        return f"{exc.domain}: {exc.message} (code {exc.code})"
    # pikepdf prefixes messages with the (meaningless) in-memory stream name
    text = re.sub(r"^stream <[^>]*>:\s*", "", str(exc).strip())
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def read_error(exc, path):
    """UserError for a failure to read `path` (OSError or Gio GLib.Error)."""
    code = _errno(exc)
    if code == errno.ENOENT:
        msg = f"{name(path)} doesn't exist. It may have been moved, renamed or deleted."
    elif code in (errno.EACCES, errno.EPERM):
        msg = f"You don't have permission to open {name(path)}."
    elif code == errno.EISDIR:
        msg = f"{name(path)} is a folder, not a file."
    else:
        msg = f"{name(path)} couldn't be read."
    return UserError(msg, technical(exc))


def save_error(exc, path):
    """UserError for a failed save to `path`.

    Saves are atomic, so a failed save never changes the existing file.
    """
    if isinstance(exc, UserError):
        return exc
    existed = os.path.exists(path)
    code = _errno(exc)
    if code in (errno.EACCES, errno.EPERM):
        msg = (f"You don't have permission to save in the folder {folder(path)}. "
               "Use Save As… to choose another folder.")
    elif code in (errno.ENOSPC, errno.EDQUOT):
        msg = "There isn't enough free disk space. Free up some space and try again."
    elif code == errno.EROFS:
        msg = (f"The folder {folder(path)} is on a read-only drive. "
               "Use Save As… to choose another folder.")
    elif code == errno.ENOENT:
        msg = (f"The folder {folder(path)} no longer exists. "
               "Use Save As… to choose another folder.")
    elif code == errno.ENAMETOOLONG:
        msg = "The file name is too long. Use Save As… to choose a shorter name."
    elif isinstance(exc, OSError):
        msg = f"{name(path)} couldn't be written to disk."
    else:
        log.error("unexpected error while saving %s", path, exc_info=exc)
        msg = f"Something went wrong while preparing {name(path)} for saving."
    if existed:
        msg += " The file on disk hasn't been changed."
    return UserError(msg, technical(exc))


def _errno(exc):
    if isinstance(exc, OSError):
        return exc.errno
    if isinstance(exc, GLib.Error) and exc.domain == GLib.quark_to_string(Gio.io_error_quark()):
        return {
            Gio.IOErrorEnum.NOT_FOUND: errno.ENOENT,
            Gio.IOErrorEnum.PERMISSION_DENIED: errno.EACCES,
            Gio.IOErrorEnum.IS_DIRECTORY: errno.EISDIR,
            Gio.IOErrorEnum.NO_SPACE: errno.ENOSPC,
            Gio.IOErrorEnum.READ_ONLY: errno.EROFS,
            Gio.IOErrorEnum.FILENAME_TOO_LONG: errno.ENAMETOOLONG,
        }.get(exc.code)
    return None
