"""Structured (JSON-lines) logging with secret masking."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path

_SECRET = re.compile(r"(access_token|api_secret|request_token|app_token|token)([\"'=: ]+)([A-Za-z0-9_\-]{6,})", re.I)


class JsonFormatter(logging.Formatter):
    def format(self, r):
        msg = _SECRET.sub(lambda m: m.group(1) + m.group(2) + "***", r.getMessage())
        d = {"ts": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname, "logger": r.name, "msg": msg}
        if r.exc_info:
            d["exc"] = _SECRET.sub(lambda m: m.group(1) + m.group(2) + "***", self.formatException(r.exc_info))
        return json.dumps(d)


def setup_logging(folder: Path) -> logging.Logger:
    folder.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("cockpit")
    if not root.handlers:
        root.setLevel(logging.INFO)
        fh = logging.handlers.RotatingFileHandler(folder / "cockpit.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setFormatter(JsonFormatter())
        root.addHandler(sh)
    return root
