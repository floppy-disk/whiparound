"""JSON lines: one object per event, appended to the log file and echoed to stdout. No URLs, no secrets."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .http import redact


class JsonlLog:
    def __init__(self, path: Path | None):
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> dict:
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        line = redact(json.dumps(rec, default=str, ensure_ascii=False))
        if self.path is not None:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        print(line, flush=True, file=sys.stdout)
        return rec
