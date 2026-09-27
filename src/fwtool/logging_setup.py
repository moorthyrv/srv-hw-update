"""Structured file logging with credential redaction."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .credentials import known_secrets


class RedactFilter(logging.Filter):
    """Last line of defence: mask any known secret that reaches a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = known_secrets()
        if not secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in secrets:
            if s and s in redacted:
                redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg, record.args = redacted, ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(log_file: str | Path | None, verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)
    redact = RedactFilter()

    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    console.addFilter(redact)
    root.addHandler(console)

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG if verbose else logging.INFO)
        fh.setFormatter(JsonFormatter())
        fh.addFilter(redact)
        root.addHandler(fh)

    # urllib3 logs full URLs at DEBUG; keep it quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
