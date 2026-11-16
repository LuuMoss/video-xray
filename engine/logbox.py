"""Plugin-owned diagnostics: load errors and route crashes land in
<plugin>/logs/video-xray.log instead of only the host log tree. Stdlib only;
the path is derived from __file__ so no engine module needs to import first.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_NAME = "video-xray"


def log_file() -> str:
    return str(_LOG_DIR / "video-xray.log")


def get_logger() -> logging.Logger:
    lg = logging.getLogger("xray." + _NAME)
    if getattr(lg, "_xray_handler", False):
        return lg
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(_LOG_DIR / "video-xray.log",
                                maxBytes=400_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(process)d] %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        lg._xray_handler = True
    except OSError:
        pass
    return lg



def open_log():
    """Open this plugin's log with the system default viewer.

    The panel's log button posts /logs/open here so users can read the
    full failure trace anytime, not only when a red line appears.
    Windows hands off to the shell; POSIX falls back to open/xdg-open.
    Never raises: any problem comes back as {ok:False, reason_detail}
    for the panel to surface as a toast."""
    p = Path(log_file())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()          # the viewer must always find a file
        if sys.platform == "win32":
            import os
            try:
                # honour whatever editor the user associated with .log
                os.startfile(str(p))  # noqa: S606 - shell handoff
            except OSError:
                # ...but .log ships with no default handler on stock
                # Windows, so fall back to Notepad instead of silence
                subprocess.Popen(
                    ["notepad.exe", str(p)])
        else:
            subprocess.Popen(["open" if sys.platform == "darwin"
                              else "xdg-open", str(p)])
        return {"ok": True, "log_file": str(p)}
    except OSError as exc:
        return {"ok": False, "reason_code": "open_failed",
                "reason_detail": str(exc), "log_file": str(p)}
