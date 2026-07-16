"""
WorldState -- persistent, flexible world memory backed by a single JSON file.

Shape on disk: {"<object>": {<open dict the agent fills>}, ...}. `info` has NO required
schema. WorldState owns safe I/O:
  - atomic writes (temp file in the same dir, fsync, os.replace) so a crash mid-write can
    never corrupt the existing file;
  - per-object updates merge into that object only and never wipe other objects;
  - no implicit deletes (merge only adds/overwrites keys within the target object);
  - every write is logged.
"""
import json
import os
import tempfile


class WorldStateError(Exception):
    pass


class WorldState:
    def __init__(self, path: str, logger=None):
        self.path = path
        self.logger = logger
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # Fail loudly rather than silently returning {} and then wiping real data.
            raise WorldStateError(f"world state at {self.path} is unreadable/corrupt: {e}") from e
        if not isinstance(data, dict):
            raise WorldStateError(f"world state at {self.path} is not a JSON object")
        return data

    def _atomic_write(self, data: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".world-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)  # atomic on the same filesystem
        except BaseException:
            # On any failure the original file is untouched; clean up the temp file.
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def get_known_location(self, obj: str):
        """Return the stored info dict for `obj`, or None if unknown."""
        return self._load().get(obj)

    def update_world(self, obj: str, info: dict) -> dict:
        """
        Merge `info` (an open dict) into the record for `obj`. Other objects are left
        untouched; no keys are deleted. Returns the merged record for `obj`.
        """
        if not isinstance(info, dict):
            raise TypeError("info must be a dict (open schema, agent-filled)")

        data = self._load()
        existing = data.get(obj)
        if not isinstance(existing, dict):
            existing = {}
        merged = {**existing, **info}
        data[obj] = merged
        self._atomic_write(data)

        if self.logger:
            self.logger.log("world_state.write", {"object": obj, "info": info},
                            {"ok": True, "stored": merged})
        return merged
