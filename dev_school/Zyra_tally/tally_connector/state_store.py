"""Tiny persisted state for incremental polling: the highest Tally ALTERID
already seen per collection type. Tally's XML gateway has no webhook/push
mechanism, so detecting "what changed since last time" means asking for
everything with ALTERID greater than the last one we processed -- see
ARCHITECTURE.md's inbound flow and TALLY_INTEGRATION.md's polling contract.

A single small JSON file is enough for this -- there is no need for a real
database just to remember a handful of integers across restarts."""
import json
import logging
import os
import tempfile

_logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path):
        self.path = path
        self._state = self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except (ValueError, OSError):
            _logger.exception("Could not read state file %s -- starting fresh", self.path)
            return {}

    def _save(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        # Write to a temp file and rename over the target so a crash
        # mid-write never leaves a half-written, unparseable state file.
        fd, tmp_path = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp_path, self.path)
        except Exception:
            os.unlink(tmp_path)
            raise

    def get_last_alterid(self, object_type):
        return int(self._state.get(f"last_alterid.{object_type}", 0))

    def set_last_alterid(self, object_type, value):
        self._state[f"last_alterid.{object_type}"] = int(value)
        self._save()
