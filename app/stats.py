"""Lifetime statistics, kept in /data/stats.json (survive restarts and updates)."""
import json
import os
import threading
import time

TOTAL_KEYS = ("archives", "files", "bytes_out", "bytes_in", "freed", "moved", "junk", "junk_bytes",
              "failed", "runs", "seconds", "nested")
DAY_KEYS = ("archives", "files", "bytes_out", "bytes_in", "failed")
MAX_DAYS = 400


def archive_type(path):
    low = path.lower()
    for ext, name in ((".rar", "rar"), (".zip", "zip"), (".7z", "7z"), (".7z.001", "7z"), (".zip.001", "zip")):
        if low.endswith(ext):
            return name
    return "tar"


def blank():
    return {"since": time.time(), "totals": {k: 0 for k in TOTAL_KEYS}, "types": {}, "type_bytes": {},
            "days": {}, "records": {}}


class Stats:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()
        self._dirty = 0

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            b = blank()
            for k in b:
                d.setdefault(k, b[k])
            for k in TOTAL_KEYS:
                d["totals"].setdefault(k, 0)
            return d
        except (OSError, ValueError, AttributeError):
            return blank()

    def save(self):
        with self.lock:
            snap = json.dumps(self.data)
            self._dirty = 0
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.write(snap)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _day(self):
        days = self.data["days"]
        key = time.strftime("%Y-%m-%d")
        if key not in days:
            days[key] = {k: 0 for k in DAY_KEYS}
            for old in sorted(days)[:-MAX_DAYS]:
                del days[old]
        return days[key]

    def _record(self, name, value, path, unit):
        r = self.data["records"].get(name)
        if not r or value > r["value"]:
            self.data["records"][name] = {"value": value, "path": path, "unit": unit, "at": time.time()}

    def archive(self, path, files, bytes_out, bytes_in, seconds, mode, nested):
        t = archive_type(path)
        with self.lock:
            tot, day = self.data["totals"], self._day()
            for d in (tot, day):
                d["archives"] += 1
                d["files"] += files
                d["bytes_out"] += bytes_out
                d["bytes_in"] += bytes_in
            tot["seconds"] += seconds
            tot["freed" if mode == "delete" else "moved"] += bytes_in
            tot["nested"] += 1 if nested else 0
            self.data["types"][t] = self.data["types"].get(t, 0) + 1
            self.data["type_bytes"][t] = self.data["type_bytes"].get(t, 0) + bytes_out
            self._record("largest", bytes_out, path, "bytes")
            self._record("most_files", files, path, "files")
            if seconds >= 1 and bytes_out >= 50_000_000:
                self._record("fastest", bytes_out / seconds, path, "bytes/s")
            self._dirty += 1
            due = self._dirty >= 10
        if due:
            self.save()

    def junk(self, size, mode):
        with self.lock:
            self.data["totals"]["junk"] += 1
            self.data["totals"]["junk_bytes"] += size
            self.data["totals"]["freed" if mode == "delete" else "moved"] += size

    def failed(self):
        with self.lock:
            self.data["totals"]["failed"] += 1
            self._day()["failed"] += 1

    def run_finished(self):
        with self.lock:
            self.data["totals"]["runs"] += 1
        self.save()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def reset(self):
        with self.lock:
            self.data = blank()
        self.save()
