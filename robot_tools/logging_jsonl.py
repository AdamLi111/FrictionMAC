"""
Append-only JSONL tool-call log.

Every tool call logs exactly one line: {timestamp, name, args, result}. Image-bearing
results are redacted by the caller before they reach here (frame count/size, not raw
base64) so the log stays readable.
"""
import json
import os
import threading
from datetime import datetime, timezone


def _json_safe(value):
    """Best-effort coercion to something json.dumps can handle."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        return repr(value)


class ToolLogger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()

    def log(self, name: str, args, result) -> dict:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "name": name,
            "args": _json_safe(args),
            "result": _json_safe(result),
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return entry

    def read_all(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
