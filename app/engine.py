"""Extraction engine.

Walks the source folders ONE DIRECTORY AT A TIME. For each directory it
extracts every archive in it (into the same directory), removes/moves the
archive and any junk files, rescans the same directory for nested archives,
and only then descends into the sub-directories.
"""
import difflib
import fnmatch
import logging
import logging.handlers
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import mounts
from stats import Stats

ARCHIVE_SUFFIXES = (
    ".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz",
    ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
)
PART_RE = re.compile(r"\.part0*(\d+)\.rar$", re.I)
NUMVOL_RE = re.compile(r"^(.*\.(?:7z|zip))\.(\d{3})$", re.I)   # name.7z.001, name.zip.001
STAGE_PREFIX = ".autoextract-"     # private per-archive extraction folders
FIRST_PART_RE = re.compile(r"^(.*)\.part0*1\.rar$", re.I)
RAR_RE = re.compile(r"^(.*)\.rar$", re.I)


ARCHIVE_EXT_RE = re.compile(r"\.(?:tar\.gz|tar\.bz2|tar\.xz|tgz|tbz2|txz|tar|7z|zip|rar)$", re.I)
OLD_VOL_RE = re.compile(r"\.(?:r\d\d|z\d\d+)$", re.I)          # name.r00, name.z01
INFO_EXTS = (".nfo", ".diz", ".sfv", ".md5", ".sha1", ".sha256")   # small files that ride along with release archives
SIMILAR_RATIO = 0.85
MAX_NAME_BYTES = 247                   # 255 minus room for " (99999)"


def archive_base_name(filename):
    """Name of an archive without its extension and volume part: name.part01.rar, name.zip.001, name.tar.gz,
    name.r00 ... -> name. Trailing dots and spaces are trimmed; an empty result becomes <file name>_files."""
    base = os.path.basename(filename)
    name = base
    m = PART_RE.search(name)
    if m:
        name = name[:m.start()]
    else:
        m = NUMVOL_RE.match(name)
        if m:
            name = m.group(1)
        name = ARCHIVE_EXT_RE.sub("", name, count=1)
        if name == base:                                   # not an archive extension: maybe name.r00 / name.z01
            name = OLD_VOL_RE.sub("", name, count=1)
    name = name.rstrip(". ")
    return name or (base + "_files")


def _norm_name(s):
    return re.sub(r"[.\s_\-]+", " ", s.lower()).strip()


def names_similar(a, b):
    """True when two names (a folder name and an archive name without its extension) are about the same thing:
    equal, or one starts with the other at a separator boundary, or at least 85% alike. '.', '_', '-' and
    spaces count as one separator and case is ignored."""
    a, b = _norm_name(a), _norm_name(b)
    if not a or not b:
        return False
    if a == b or a.startswith(b + " ") or b.startswith(a + " "):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR_RATIO


def which_tools():
    return {
        "unzip": shutil.which("unzip"),
        "unrar": shutil.which("unrar"),
        "7z": shutil.which("7zz") or shutil.which("7z") or shutil.which("7za"),
        "tar": shutil.which("tar"),
    }


def is_archive(name):
    if name.startswith(STAGE_PREFIX):
        return False
    if NUMVOL_RE.match(name):
        return True
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def is_archive_piece(name):
    """An archive, or one piece of a split archive: .rar, .partN.rar, .rNN, .zip, .zNN, .7z, .7z.NNN, .zip.NNN, tar ..."""
    if name.startswith(STAGE_PREFIX):
        return False
    return is_archive(name) or bool(OLD_VOL_RE.search(name))


def is_secondary_volume(name):
    """name.part2.rar, name.part03.rar ... (only the first part is extracted)."""
    m = PART_RE.search(name)
    if m:
        return int(m.group(1)) != 1
    m = NUMVOL_RE.match(name)
    return bool(m) and int(m.group(2)) != 1


UNRAR_CODES = {1: "warning, some files were not extracted", 2: "fatal error", 3: "checksum error - the archive "
               "is damaged, or a part of the multi-part set is missing or incomplete", 4: "the archive is locked",
               5: "could not write the extracted files (disk full or permissions?)",
               6: "could not open a file or volume", 7: "bad command line", 8: "not enough memory",
               9: "could not create a file", 10: "no files to extract (not the first volume, or not a valid archive)",
               11: "wrong or missing password", 12: "read error"}
UNZIP_CODES = {1: "warning, some files were skipped", 2: "the zip file is damaged",
               3: "the zip file is damaged or not a zip file", 9: "zip file not found", 11: "no matching files",
               50: "disk full or write error", 51: "the zip file ends early (incomplete download?)",
               81: "unsupported compression method", 82: "wrong or missing password"}
SEVENZ_CODES = {1: "warning", 2: "fatal error (damaged archive, missing volume or wrong password)",
                7: "bad command line", 8: "not enough memory"}


def _human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def explain(tool, code):
    return {"unrar": UNRAR_CODES, "unzip": UNZIP_CODES, "7z": SEVENZ_CODES}.get(tool, {}).get(code, "")


FUTURE_SLACK = 120                      # seconds a file date may be ahead of the clock before it is not trusted
MAX_REMEMBERED = 5000                   # entries kept in each "remembered archives" file


class Engine:
    def __init__(self, log_dir, history_path):
        self.lock = threading.RLock()
        self.move_lock = threading.Lock()
        self.merge_lock = threading.Lock()
        self.cancel_file_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.procs = set()
        self.thread = None
        self.history_path = history_path
        # archives that came out of an archive, remembered between scans: they are complete files,
        # so the "skip recent files" rule (meant for downloads in progress) never applies to them
        self.nested_path = os.path.join(os.path.dirname(history_path) or ".", "extracted-archives.json")
        # archives the user cancelled: scheduled scans leave them alone until they change (Run now retries)
        self.cancelled_path = os.path.join(os.path.dirname(history_path) or ".", "cancelled-archives.json")
        # archives that failed for good (even when tried alone): scheduled scans leave them alone until they change
        self.failed_file = os.path.join(os.path.dirname(history_path) or ".", "failed-archives.json")
        # archives extracted and left in place (action "keep", or files skipped with overwrite off):
        # scans leave them alone until they change; {path: {"sig": ..., "why": "keep"|"exists"}}
        self.kept_file = os.path.join(os.path.dirname(history_path) or ".", "kept-archives.json")
        self._kept_mem = {}
        self._cancelled_mem = {}     # the files are read once per run and kept here
        self._failed_mem = {}
        self.skipped = 0             # archives left alone because they failed / were cancelled earlier and have not changed
        self.tools = which_tools()

        self.log_buf = deque(maxlen=2000)
        self.log_seq = 0
        self.log_cleared = 0
        self._file_log = None
        try:
            os.makedirs(log_dir, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "extract.log"), maxBytes=2_000_000, backupCount=3)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._file_log = logging.getLogger(f"extract-archives.{id(self)}")
            self._file_log.propagate = False
            self._file_log.setLevel(logging.INFO)
            self._file_log.addHandler(handler)
        except OSError:
            self._file_log = None

        self.history = self._load_history()
        self.stats = Stats(os.path.join(os.path.dirname(history_path) or ".", "stats.json"))
        self.status = self._blank_status()
        self._warned = set()
        self.seen = set()            # archives counted so far (for found/remaining)
        self.failures = []
        self.failed_paths = set()
        self.deferred = set()        # archives skipped because they are too recent
        self.deferred_ready = {}     # archive -> time it is old enough to be extracted
        self.nested = set()          # archives that came out of an archive in this run (never too recent)
        self.kept = set()
        self._mounts = []
        self.cancelled = 0
        self.cancelled_paths = set()
        self._slot_procs = {}        # slot -> running extractor processes (so one archive can be cancelled alone)
        self._skip_logged = set()
        self._slots = []
        self._dir_sets = {}          # folder -> archive sets found in it at the start of the current pass
        self._made = {}              # wrapper folder created in this run -> the archive set that created it
        self._inplace = set()        # archive pieces extracted in place in this run
        self._held = set()           # archive sets waiting for pieces still inside too-new archives
        self._parents = {}           # nested archive path -> the archive set it came out of (this run)

    # ------------------------------------------------------------ logging
    def log(self, level, msg):
        with self.lock:
            self.log_seq += 1
            self.log_buf.append({"id": self.log_seq, "t": time.time(), "level": level, "msg": msg})
        if self._file_log:
            getattr(self._file_log, "warning" if level == "warn" else
                    "error" if level == "error" else "info")(msg)

    def clear_log(self):
        """Empty the on-screen log (the log file in /data/logs is kept)."""
        with self.lock:
            self.log_buf.clear()
            self.log_cleared = self.log_seq

    def log_since(self, after=0, limit=400):
        with self.lock:
            items = [e for e in self.log_buf if e["id"] > after]
        return items[-limit:]

    def _warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            self.log("warn", msg)

    # ------------------------------------------------------------ history
    def _load_history(self):
        try:
            import json
            with open(self.history_path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self):
        import json
        try:
            tmp = self.history_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.history[-100:], f)
            os.replace(tmp, self.history_path)
        except OSError as e:
            self.log("warn", f"Could not save history: {e}")

    # ------------------------------------------------------------ status
    def _blank_status(self):
        return {
            "state": "idle", "trigger": None, "dry_run": False,
            "started_at": None, "finished_at": None, "result": None, "message": "",
            "current_dir": "", "pass": 0,
            "extracted": 0, "junk": 0, "slots": [], "files_out": 0, "bytes_out": 0, "bytes_in": 0,
        }

    @property
    def running(self):
        t = self.thread
        return bool(t and t.is_alive())

    def snapshot(self):
        with self.lock:
            s = dict(self.status)
            now = time.time()
            s["slots"] = [
                ({"n": i + 1, "path": sl["path"], "elapsed": int(now - sl["t"]), "phase": sl.get("phase", "extracting"),
                  "cancel": sl.get("cancel", False)} if sl
                 else {"n": i + 1, "path": None, "elapsed": 0})
                for i, sl in enumerate(self._slots)
            ] if self.status["state"] != "idle" else []
            failed = self.failures
            s["found"] = len(self.seen)
            s["failed"] = len(failed)
            s["cancelled"] = self.cancelled
            s["skipped"] = self.skipped
            s["done"] = s["extracted"] + s["failed"] + self.cancelled + self.skipped
            s["remaining"] = max(0, s["found"] - s["done"])
            s["deferred"] = len(self.deferred)
            s["waiting"] = self._waiting()
            s["failures"] = failed[-50:]
            s["tools"] = self.tools
            return s

    def _waiting(self):
        """Folders with archives skipped as too recent: [{dir, count, names, ready_at}], soonest first."""
        groups = {}
        for p in self.deferred:
            g = groups.setdefault(os.path.dirname(p), {"dir": os.path.dirname(p), "count": 0, "names": [], "ready_at": None})
            g["count"] += 1
            g["names"].append(os.path.basename(p))
            r = self.deferred_ready.get(p)
            if r and (g["ready_at"] is None or r > g["ready_at"]):
                g["ready_at"] = r            # the whole folder is ready when its newest archive is
        out = sorted(groups.values(), key=lambda g: (g["ready_at"] or 0, g["dir"]))
        for g in out:
            g["names"] = sorted(g["names"])[:5]
        return out[:100]

    # ------------------------------------------------------------ control
    def start(self, cfg, trigger="manual", dry_run=False, oneoff=None):
        """oneoff = {"path": folder, "recursive": bool}: extract just that folder now (one-off job)."""
        with self.lock:
            if self.running:
                return False
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run, args=(dict(cfg), trigger, dry_run, oneoff), daemon=True)
            self.status = self._blank_status()
            self.status.update(state="running", trigger=trigger, dry_run=dry_run,
                               started_at=time.time())
            self.seen = set()
            self.failures = []
            self.failed_paths = set()
            self.deferred = set()
            self.deferred_ready = {}
            self.nested = self._load_nested()
            self.kept = set()            # one-off "keep": extracted, left in place, not extracted again in this run
            self.cancelled = 0
            self.skipped = 0
            self.cancelled_paths = set()
            self._slot_procs = {}
            self._skip_logged = set()
            self._dir_sets = {}
            self._made = {}
            self._parents = {}
            self._held = set()
            self._inplace = set()
            self._slots = [None] * cfg["threads"]
            self.thread.start()
            return True

    def stop(self):
        with self.lock:
            if not self.running:
                return False
            self.stop_event.set()
            self.status["state"] = "stopping"
            for p in list(self.procs):
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        self.log("warn", "Stop requested - finishing up")
        return True

    # ------------------------------------------------------------ the run
    def _run(self, cfg, trigger, dry_run, oneoff=None):
        cfg["dry_run"] = dry_run
        cfg["_skip_dirs"] = set()
        cfg["_trigger"] = trigger
        result, message = "completed", ""
        self.log("info", f"=== Run started ({trigger}{', dry run' if dry_run else ''}) ===")
        try:
            self._mounts = mounts.list_mounts(os.path.dirname(self.history_path) or "/data")
            self._cancelled_mem = self._load_remembered(self.cancelled_path)
            self._failed_mem = self._load_remembered(self.failed_file)
            self._kept_mem = self._load_remembered(self.kept_file)
            cfg["_oneoff"] = bool(oneoff)
            sources = []
            if oneoff:
                target = os.path.realpath(oneoff["path"])
                if not os.path.isdir(target):
                    raise RuntimeError(f"Folder not found: {target}")
                if target == os.sep:
                    raise RuntimeError("The root folder (/) cannot be used here - choose a folder inside one of your shares")
                sources = [target]
                if not oneoff.get("recursive", True):
                    # only this folder: skip the sub-folders that are already there (new ones that come out of
                    # an archive are still processed, so nested archives get extracted)
                    try:
                        cfg["_skip_dirs"] = {e.path for e in os.scandir(target) if e.is_dir(follow_symlinks=False)}
                    except OSError:
                        pass
                self.log("info", f"One-off: extracting {target}" +
                         (" and its sub-folders" if oneoff.get("recursive", True) else " (this folder only)"))
            else:
                for s in cfg["sources"]:
                    s = os.path.realpath(s)
                    if os.path.isdir(s):
                        sources.append(s)
                    else:
                        self.log("warn", f"Source folder not found, skipping: {s}")
            if not sources:
                raise RuntimeError("No valid source folders configured")

            if "recycle" in (cfg["archive_mode"], cfg["junk_mode"]):
                for s in sources:
                    _, why = mounts.recycle_target(os.path.join(s, "x"), self._mounts)
                    if why:
                        raise RuntimeError(f"Cannot use the Recycle Bin for {s}: {why}. Choose delete or move "
                                           "instead, or map the share (or /mnt/user) into the container")
                    rb = mounts.recycle_root(os.path.join(s, "x"), self._mounts)[0]
                    host = mounts.path_info(rb, self._mounts)["host"]
                    self.log("info", f"Processed files from {s} go to the Recycle Bin: {rb}"
                                     + (f" (on the server: {host})" if host else ""))

            if cfg["move_dir"] and (cfg["archive_mode"] == "move" or cfg["junk_mode"] == "move"):
                cfg["move_dir"] = os.path.realpath(cfg["move_dir"])
                for s in sources:
                    if cfg["move_dir"] == s or cfg["move_dir"].startswith(s + os.sep):
                        raise RuntimeError(f"Move folder {cfg['move_dir']} is inside source {s}")
                    if s.startswith(cfg["move_dir"] + os.sep):
                        raise RuntimeError(f"Source {s} is inside the move folder {cfg['move_dir']}")
                if not dry_run:
                    os.makedirs(cfg["move_dir"], exist_ok=True)
                    self._apply_perm(cfg["move_dir"], True, cfg)
            elif cfg["archive_mode"] == "move" or cfg["junk_mode"] == "move":
                raise RuntimeError("'Move' is selected but no move folder is set")

            cfg["_roots"] = set(sources)           # a source root is never treated as a release folder
            for s in sources:
                self._prescan(s, cfg)
            self.log("info", f"Found {len(self.seen)} archive(s) to start with"
                             f" ({len(self.deferred)} too recent, skipped for now)")

            with ThreadPoolExecutor(max_workers=cfg["threads"]) as pool:
                for s in sources:
                    if self.stop_event.is_set():
                        break
                    self._process_dir(s, s, cfg, pool)
            if self.stop_event.is_set():
                result = "stopped"
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            result, message = "error", str(e)
            self.log("error", f"Run failed: {e}")
        finally:
            self._finish(trigger, dry_run, result, message)

    def _load_nested(self):
        try:
            with open(self.nested_path) as f:
                return set(json.load(f))
        except (OSError, ValueError, TypeError):
            return set()

    def _save_nested(self):
        with self.lock:
            keep = sorted(p for p in self.nested if os.path.exists(p))[-20000:]
        try:
            tmp = self.nested_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(keep, f)
            os.replace(tmp, self.nested_path)
        except OSError as e:
            self.log("warn", f"Could not save {self.nested_path}: {e}")

    def _finish(self, trigger, dry_run, result, message):
        if not dry_run:
            self._save_nested()
        with self.lock:
            st = self.status
            st["finished_at"] = time.time()
            st["state"] = "idle"
            st["result"] = result
            st["message"] = message
            st["current_dir"] = ""
            st["pass"] = 0
            entry = {
                "started_at": st["started_at"], "finished_at": st["finished_at"],
                "trigger": trigger, "dry_run": dry_run, "result": result, "message": message,
                "found": len(self.seen), "extracted": st["extracted"],
                "failed": len(self.failures), "junk": st["junk"],
                "deferred": len(self.deferred),
                "files_out": st["files_out"], "bytes_out": st["bytes_out"], "bytes_in": st["bytes_in"],
                "cancelled": self.cancelled,
                "skipped": self.skipped,
                "failures": self.failures[-50:],
            }
            self.history.append(entry)
            self._save_history()
        if not dry_run:
            self.stats.run_finished()
        self.log("info", f"=== Run {result}: {entry['extracted']} extracted, "
                         f"{entry['failed']} failed, {entry['junk']} junk handled ===")

    # ------------------------------------------------------------ scanning
    def _excluded(self, name, cfg):
        if name.startswith(STAGE_PREFIX) or name == mounts.RECYCLE_NAME:
            return True                               # never scan work folders or the Recycle Bin
        low = name.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in cfg["exclude_dirs"])

    def _group_mtime(self, path):
        """Newest change of the archive and all its other volumes. A file dated in the future (clock or timezone
        mismatch on the machine that wrote it) would look too new forever, so its change time (when it really
        arrived on this server) is used instead."""
        newest = 0
        now = time.time()
        for f in self._archive_group(path):
            try:
                st = os.stat(f)
            except OSError:
                continue
            t = st.st_mtime
            if t > now + FUTURE_SLACK:
                t = st.st_ctime
                self._warn_once(("future", path), f"{os.path.basename(f)} is dated in the future "
                                f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}) - using the time it "
                                f"arrived instead: {path}")
            newest = max(newest, t)
        return newest

    def _eligible(self, path, cfg):
        age = cfg["min_age_minutes"]
        if age <= 0 or path in self.nested:   # the age rule is for downloads, not extracted files
            return True
        changed = self._group_mtime(path)
        ok = (time.time() - changed) >= age * 60
        if not ok:
            with self.lock:
                new = path not in self.deferred
                self.deferred.add(path)
                self.deferred_ready[path] = changed + age * 60
            if new:
                mins = max(0, (time.time() - changed) / 60)
                self.log("info", f"Skipped for now (changed {mins:.0f} min ago, could still be downloading; "
                                 f"setting: skip archives newer than {age} min): {path}")
        return ok

    def _register(self, path):
        with self.lock:
            self.seen.add(path)
            self.deferred.discard(path)
            self.deferred_ready.pop(path, None)

    def _prescan(self, root, cfg):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not self._excluded(d, cfg)
                                 and not os.path.islink(os.path.join(dirpath, d)))
            for fn in filenames:
                if is_archive(fn) and not is_secondary_volume(fn):
                    p = os.path.join(dirpath, fn)
                    if self._kept_skip(p, cfg):
                        continue
                    if self._eligible(p, cfg):
                        self._register(p)

    def _list_archives(self, d, cfg):
        out = []
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError as e:
            self.log("warn", f"Cannot read {d}: {e}")
            return out
        for e in entries:
            if (e.is_file(follow_symlinks=False) and is_archive(e.name)
                    and not is_secondary_volume(e.name) and not self._kept_skip(e.path, cfg)
                    and self._eligible(e.path, cfg)):
                out.append(e.path)
        return out

    def _archive_sets(self, d):
        """The archive sets (first volumes) in a folder, listed once."""
        try:
            return {e.path for e in os.scandir(d)
                    if is_archive(e.name) and not is_secondary_volume(e.name) and e.is_file(follow_symlinks=False)}
        except OSError:
            return set()

    def _snapshot_sets(self, d):
        """Taken before each pass over a folder and added up, so every archive of the folder sees the same
        neighbours: one that was already extracted and removed (zips go before RARs) must not make the next
        one look like it is alone."""
        sets = self._archive_sets(d)
        with self.lock:
            self._dir_sets.setdefault(d, set()).update(sets)

    def _ancestors(self, path):
        """The archive set(s) this one came out of, recursively (zip holding the RAR volumes ...)."""
        out = set()
        with self.lock:
            p = self._parents.get(path)
            while p and p not in out and p != path:
                out.add(p)
                p = self._parents.get(p)
        return out

    def _has_other_set(self, d, path):
        with self.lock:
            sets = self._dir_sets.get(d)
        if sets is None:
            sets = self._archive_sets(d)
        # the archives it came out of are not neighbours - for every volume: the pieces of a set can come
        # out of different archives (movie1.zip holds part1, movie2.zip holds part2 ...)
        skip = {path}
        for f in self._archive_group(path):
            skip |= self._ancestors(f)
        return any(p not in skip for p in sets)

    def _volume_gaps(self, path):
        """True when the volume numbering of the set shows a missing piece (part1, part2, part4 ...)."""
        base = os.path.basename(path)
        nums = []
        for f in self._archive_group(path):
            n = os.path.basename(f)
            m = PART_RE.search(n) or NUMVOL_RE.match(n)
            if m:
                nums.append(int(m.group(1) if m.re is PART_RE else m.group(2)))
                continue
            m = re.search(r"\.r(\d\d)$", n, re.I)
            if m:
                nums.append(int(m.group(1)) + 1)       # name.rar is volume 1, name.r00 volume 2 ...
        if not nums:
            return False
        if RAR_RE.match(base) and not FIRST_PART_RE.match(base):
            nums.append(1)
        return set(nums) != set(range(1, max(nums) + 1))

    def _hold_for_pieces(self, d, a):
        """True when the archive set must wait: some archive in this folder is still too new (maybe still
        downloading) and this set has pieces that came out of an archive in this run, or its numbering has
        a gap - so a piece may still be inside one of the too-new archives."""
        with self.lock:
            waiting = [p for p in self.deferred if os.path.dirname(p) == d and p not in self._held]
            if not waiting:
                return False
            group = self._archive_group(a)
            came_out = any(f in self._inplace for f in group)
        if not (came_out or self._volume_gaps(a)):
            return False
        with self.lock:
            ready = max((self.deferred_ready.get(p, 0) for p in waiting), default=0)
            new = a not in self._held
            self.deferred.add(a)
            self.deferred_ready[a] = ready
            self._held.add(a)
        if new:
            self.log("info", f"Waiting: {os.path.basename(a)} - some of its pieces are still in archives that "
                             "are too new")
        return True

    def _is_release_folder(self, path, cfg):
        """The archive sits in a folder named like it, alone (other files are fine), and that folder is not
        a scanned root."""
        d = os.path.dirname(path)
        if d in cfg.get("_roots", ()):
            return False
        return names_similar(os.path.basename(d), archive_base_name(path)) and not self._has_other_set(d, path)

    def _dry_target_text(self, path, d, cfg):
        mode = cfg.get("folder_per_archive", "smart")
        if mode == "never":
            return "into this folder"
        name = archive_base_name(path)
        if mode == "smart" and self._is_release_folder(path, cfg):
            return "into this folder (it is the archive's own folder)"
        return f"into a folder named after the archive ({name}/) unless it holds a single folder"

    # ------------------------------------------------------------ one directory
    def _process_dir(self, d, root, cfg, pool):
        if self.stop_event.is_set():
            return
        dry = cfg["dry_run"]
        with self.lock:
            self.status["current_dir"] = d
        self.log("info", f"Directory: {d}")

        if not dry:
            self._remove_stale_stages(d)
        pending = []                     # failed once in parallel: retried alone at the end
        with self.lock:
            self._dir_sets.pop(d, None)
        for p in range(1, cfg["max_passes"] + 1):
            if self.stop_event.is_set():
                return
            with self.lock:
                self.status["pass"] = p
            todo = []
            self._snapshot_sets(d)
            sched = cfg.get("_trigger") == "schedule"
            for a in self._list_archives(d, cfg):
                if not dry and self._hold_for_pieces(d, a):
                    continue
                self._register(a)        # picks up nested archives that just appeared
                if a in self.failed_paths or a in pending or a in self.kept or a in self.cancelled_paths:
                    continue
                if sched:
                    why = self._remembered_why(a)
                    if why:
                        if a not in self._skip_logged:
                            self._skip_logged.add(a)
                            with self.lock:
                                self.skipped += 1
                            self.log("info", f"Skipped {a}: it {why} earlier and has not changed "
                                             "(Run now or Extract folder... tries it again)")
                        continue
                todo.append(a)
            did = 0
            if dry:
                for a in todo:
                    self.log("info", f"[dry run] would extract {self._dry_target_text(a, d, cfg)}, "
                                     f"then {self._act_text(cfg['archive_mode'], cfg)}: {a}")
            elif todo:
                # an archive that failed once in parallel may hold a piece of a RAR set that is about to be
                # extracted: try it alone first, so the set is not started with a piece missing
                early = [a for a in pending if not a.lower().endswith(".rar") and os.path.exists(a)]
                if early and any(a.lower().endswith(".rar") for a in todo):
                    pending = [a for a in pending if a not in early]
                    for a in early:
                        if self.stop_event.is_set():
                            return
                        self.log("info", f"Retrying on its own, before the RAR sets that may need it: {a}")
                        if self._work(a, root, cfg, True) == "ok":
                            did += 1
                    if did:                  # new pieces may have arrived: look at the folder again
                        self._clean_junk(d, root, cfg)
                        continue
                # zip/7z/tar first: they often contain the parts of a RAR set
                batch = [a for a in todo if not a.lower().endswith(".rar")] or todo
                futures = [(a, pool.submit(self._work, a, root, cfg, False)) for a in batch]
                for a, f in futures:
                    r = f.result()
                    if r == "ok":
                        did += 1
                    elif r == "retry":
                        pending.append(a)
            elif pending:
                retry, pending = [a for a in pending if os.path.exists(a)], []
                for a in retry:          # one at a time, after everything else in the folder
                    if self.stop_event.is_set():
                        return
                    self.log("info", f"Retrying on its own: {a}")
                    if self._work(a, root, cfg, True) == "ok":
                        did += 1
            self._clean_junk(d, root, cfg)
            if dry or (did == 0 and not pending):   # nothing new came out -> directory is finished
                break
        for a in pending:
            self._fail(a, "not retried: nesting pass limit reached")
        with self.lock:
            self._dir_sets.pop(d, None)

        if not dry and not self.stop_event.is_set():
            self._fix_perms(d, cfg)

        try:
            subs = sorted(e.path for e in os.scandir(d)
                          if e.is_dir(follow_symlinks=False) and not self._excluded(e.name, cfg))
        except OSError as e:
            self.log("warn", f"Cannot read {d}: {e}")
            subs = []
        for s in subs:
            if s in cfg.get("_skip_dirs", ()):
                continue
            self._process_dir(s, root, cfg, pool)

    # ------------------------------------------------------------ worker
    def _acquire_slot(self, path):
        with self.lock:
            for i, sl in enumerate(self._slots):
                if sl is None:
                    self._slots[i] = {"path": path, "t": time.time(), "phase": "extracting", "cancel": False}
                    return i
        return 0

    def _release_slot(self, i):
        with self.lock:
            self._slots[i] = None
            self._slot_procs.pop(i, None)

    def _halted(self, slot):
        """True when the whole run was stopped or this one archive was cancelled."""
        if self.stop_event.is_set():
            return True
        sl = self._slots[slot] if slot is not None and slot < len(self._slots) else None
        return bool(sl and sl.get("cancel"))

    def cancel_job(self, path):
        """Cancel the extraction of one archive while the others carry on. Nothing is changed: the partly
        extracted files are thrown away and the archive stays where it is. Possible until its files start
        being moved into place."""
        with self.lock:
            for i, sl in enumerate(self._slots):
                if sl and sl["path"] == path:
                    if sl.get("phase") == "finishing":
                        return False, "It is already finishing - too late to cancel"
                    sl["cancel"] = True
                    for p in list(self._slot_procs.get(i, ())):
                        try:
                            os.killpg(p.pid, signal.SIGTERM)
                        except OSError:
                            pass
                    break
            else:
                return False, "That archive is not being extracted any more"
        self.log("warn", f"Cancel requested: {path}")
        return True, ""

    def _signature(self, path):
        """Size and newest change of the whole volume set: a cancelled archive is tried again once it changes."""
        size, newest = 0, 0
        for f in self._archive_group(path):
            try:
                st = os.stat(f)
                size, newest = size + st.st_size, max(newest, st.st_mtime)
            except OSError:
                pass
        return [size, int(newest)]

    def _load_remembered(self, path):
        try:
            with open(path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_remembered(self, path, d):
        """Write a remembered-archives file: entries whose archive is gone are dropped, newest 5000 kept."""
        for k in [k for k in d if not os.path.exists(k)]:
            del d[k]
        for k in list(d)[:-MAX_REMEMBERED]:
            del d[k]
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, path)
        except OSError as e:
            self.log("warn", f"Could not save {path}: {e}")

    def _remembered_why(self, path):
        """"was cancelled" / "failed" when this archive is remembered and unchanged since, else ""."""
        with self.cancel_file_lock:
            cancelled, failed = path in self._cancelled_mem, path in self._failed_mem
            if not cancelled and not failed:
                return ""
            sig = self._signature(path)
            if cancelled and self._cancelled_mem[path] == sig:
                return "was cancelled"
            if failed and self._failed_mem[path] == sig:
                return "failed"
        return ""

    def _kept_skip(self, path, cfg):
        """True when a normal scan should quietly pass over this archive: it was extracted and left in place,
        has not changed, and the current settings would leave it there again. One-off jobs never skip."""
        if cfg.get("_oneoff"):
            return False
        with self.cancel_file_lock:
            e = self._kept_mem.get(path)
            if e is None:
                return False
            sig, why = (e.get("sig"), e.get("why", "keep")) if isinstance(e, dict) else (e, "keep")
            if sig != self._signature(path):
                return False
        if why == "exists":
            return not cfg["overwrite"]
        return cfg["archive_mode"] == "keep"

    def _remember_kept(self, path, why):
        """Record every volume of the set as extracted and left in place."""
        with self.cancel_file_lock:
            sig = self._signature(path)
            for f in self._archive_group(path):
                self._kept_mem.pop(f, None)
                self._kept_mem[f] = {"sig": sig, "why": why}
            self._save_remembered(self.kept_file, self._kept_mem)

    def _remember(self, kind, path):
        """Remember a cancelled/failed archive with its signature."""
        with self.cancel_file_lock:
            mem, file = ((self._cancelled_mem, self.cancelled_path) if kind == "cancelled"
                         else (self._failed_mem, self.failed_file))
            mem.pop(path, None)
            mem[path] = self._signature(path)
            self._save_remembered(file, mem)

    def _forget(self, path):
        """The archive was extracted: it is no longer remembered as cancelled or failed."""
        with self.cancel_file_lock:
            for mem, file in ((self._cancelled_mem, self.cancelled_path), (self._failed_mem, self.failed_file)):
                if mem.pop(path, None) is not None:
                    self._save_remembered(file, mem)
            if any([self._kept_mem.pop(f, None) is not None for f in self._archive_group(path) + [path]]):
                self._save_remembered(self.kept_file, self._kept_mem)

    def _work(self, path, root, cfg, final):
        """Returns "ok", "retry" (failed - try again alone later) or "fail"."""
        if self.stop_event.is_set():
            return "fail"
        slot = self._acquire_slot(path)
        try:
            self.log("info", f"[thread {slot + 1}] Extracting {path}")
            t0 = time.time()
            ok, err, out = self._extract(path, os.path.dirname(path), cfg, slot)
            secs = time.time() - t0
            if not ok and not self.stop_event.is_set() and self._halted(slot):   # cancelled by the user
                with self.lock:
                    self.cancelled += 1
                    self.cancelled_paths.add(path)
                self._remember("cancelled", path)
                self.log("warn", f"Cancelled: {path} - nothing was changed. Scheduled scans skip it until it changes; "
                                 "Run now (or Extract folder...) tries it again")
                return "cancelled"
            if ok:
                self._forget(path)
                bytes_in = 0
                for f in self._archive_group(path):
                    try:
                        bytes_in += os.path.getsize(f)
                    except OSError:
                        pass
                was_nested = path in self.nested
                if out.get("skipped"):
                    # files were left as they were, so the archive is the only full copy: keep it
                    with self.lock:
                        self.kept.update(self._archive_group(path))
                    self.log("warn", f"Kept {out['skipped']} file(s) as they were because they already exist and "
                                     f"overwriting is off. The archive was left in place so nothing is lost: {path}")
                    disposed = True
                elif self._dispose_archive(path, root, cfg):
                    disposed = True
                else:
                    disposed = False
                if disposed:
                    if out.get("skipped"):
                        self._remember_kept(path, "exists")
                    elif cfg["archive_mode"] == "keep":
                        self._remember_kept(path, "keep")
                    with self.lock:
                        self.status["extracted"] += 1
                        self.status["files_out"] += out["files"]
                        self.status["bytes_out"] += out["bytes"]
                        self.status["bytes_in"] += bytes_in
                    self.stats.archive(path, out["files"], out["bytes"], bytes_in, secs,
                                       cfg["archive_mode"], was_nested)
                    self.log("info", f"Done: {os.path.basename(path)} - {out['files']} file(s), "
                                     f"{_human(out['bytes'])} in {secs:.1f}s")
                    return "ok"
                self._fail(path, "Extracted, but the archive could not be removed/moved")
                return "fail"
            if self.stop_event.is_set():
                return "fail"
            if not final:
                self.log("warn", f"Failed, will retry on its own after the rest of the folder: {path} - {err}")
                return "retry"
            self._fail(path, err)
            self._remember("failed", path)       # scheduled scans leave it alone until it changes
            return "fail"
        except Exception as e:  # noqa: BLE001 - one archive must never stop the others
            self._fail(path, f"unexpected error: {e}")
            return "fail"
        finally:
            self._release_slot(slot)

    def _fail(self, path, err):
        with self.lock:
            self.failed_paths.add(path)
            self.failures.append({"path": path, "error": err})
        self.stats.failed()
        self.log("error", f"FAILED (left in place): {path} - {err}")

    # ------------------------------------------------------------ extraction
    def _commands(self, path, dest, cfg):
        """Extractors to try, best first: [(tool, command)]."""
        t, low, ow = self.tools, path.lower(), cfg["overwrite"]
        seven = [("7z", [t["7z"], "x", "-aoa" if ow else "-aos", "-y", "-bd", "-p-", "-o" + dest, path])] \
            if t["7z"] else []
        unzip = [("unzip", [t["unzip"], "-o" if ow else "-n", "-q", path, "-d", dest])] if t["unzip"] else []
        unrar = [("unrar", [t["unrar"], "x", "-o+" if ow else "-o-", "-idq", "-p-", "-y", path, dest + "/"])] \
            if t["unrar"] else []
        if NUMVOL_RE.match(os.path.basename(path)):
            return seven                                  # name.7z.001 / name.zip.001
        if low.endswith(".zip"):
            if self._split_zip_parts(path):
                return seven                              # name.zip + name.z01 ...: unzip can't read these
            return unzip + seven
        if low.endswith(".rar"):
            return unrar + seven
        if low.endswith(".7z"):
            return seven
        if low.endswith(ARCHIVE_SUFFIXES) and t["tar"]:
            return [("tar", [t["tar"], "-xf", path, "-C", dest] + ([] if ow else ["-k"]))]
        return []

    def _split_zip_parts(self, path):
        d, base = os.path.split(path)
        rx = re.compile("^" + re.escape(base[:-4]) + r"\.z\d\d+$", re.I)
        try:
            return [os.path.join(d, n) for n in os.listdir(d) if rx.match(n)]
        except OSError:
            return []

    def _exec(self, cmd, slot=None):
        try:
            # own process group so "Stop" can kill the extractor and anything it spawned
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 start_new_session=True)
        except OSError as e:
            return None, str(e)
        with self.lock:
            self.procs.add(p)
            if slot is not None:
                self._slot_procs.setdefault(slot, set()).add(p)
            cancel_now = self._halted(slot)
        if cancel_now:                                     # cancelled between the check and the start
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            out, _ = p.communicate()
        finally:
            with self.lock:
                self.procs.discard(p)
                if slot is not None:
                    self._slot_procs.get(slot, set()).discard(p)
        return p.returncode, out or ""

    def _extract(self, path, dest, cfg, slot=None):
        """Extract into a private folder next to the archive, then move the result into place, so
        parallel extractions never write the same file at the same time. If the first extractor
        fails, the next one (7-Zip) is tried."""
        tools = [tool for tool, _ in self._commands(path, dest, cfg)]
        if not tools:
            return False, "No extractor installed for this file type", None
        errors = []
        for tool in tools:
            if self._halted(slot):
                return False, "stopped", None
            try:
                stage = tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=dest)
            except OSError as e:
                return False, f"cannot create a work folder in {dest}: {e}", None
            try:
                cmd = dict(self._commands(path, stage, cfg))[tool]
                code, out = self._exec(cmd, slot)
                if self._halted(slot):                     # stopped or cancelled: throw the partial output away
                    return False, "stopped", None
                if code == 0:
                    with self.lock:                        # from here on the files are being moved into place
                        if slot is not None and slot < len(self._slots) and self._slots[slot]:
                            if self._slots[slot].get("cancel"):
                                return False, "stopped", None
                            self._slots[slot]["phase"] = "finishing"
                    out = self._merge(stage, dest, cfg, path)
                    if errors:
                        self.log("info", f"Extracted with {tool} after: {' | '.join(errors)}")
                    return True, "", out
                why = explain(tool, code) if code is not None else ""
                tail = " ".join(out.split())[-200:]
                errors.append(f"{tool} exit code {code}" + (f" ({why})" if why else "")
                              + (f": {tail}" if tail else ""))
            except OSError as e:
                errors.append(f"{tool}: {e}")
            finally:
                shutil.rmtree(stage, ignore_errors=True)
        return False, " | then ".join(errors), None

    def _choose_target(self, stage, dest, cfg, path):
        """Where the extracted files go: dest itself, or dest/<archive name>/ (see the folder_per_archive
        setting). Returns (folder, why, pieces); why is "" when nothing is wrapped, pieces is True when the
        archive only holds archive pieces (and info files) and was extracted in place for that reason."""
        mode = cfg.get("folder_per_archive", "smart")
        if mode == "never" or path is None:
            return dest, "", False
        try:
            top = [(e.name, e.is_dir(follow_symlinks=False)) for e in os.scandir(stage)]
        except OSError:
            return dest, "", False
        if not top:
            return dest, "", False                             # an empty archive extracts nothing
        pieces = self._only_pieces(top, cfg)
        if pieces:
            shown = pieces[0] + (f" and {len(pieces) - 1} more" if len(pieces) > 1 else "")
            self.log("info", f"Extracted in place: the archive only holds archive pieces ({shown}), "
                             "so they stay together with the others")
            return dest, "", True
        name = archive_base_name(path)
        one_folder = len(top) == 1 and top[0][1]
        if mode == "always":
            if one_folder and names_similar(top[0][0], name):
                return dest, "", False                         # avoid name/name
            why = "folder per archive is set to Always"
        else:
            if self._is_release_folder(path, cfg):
                return dest, "", False
            if one_folder:
                return dest, "", False
            why = "the archive has no single top folder"
        name = self._wrapper_base(name, cfg)
        n = 0
        last = ""
        target = os.path.join(dest, name)
        while True:
            base = os.path.basename(target)
            if os.path.lexists(target) and (os.path.islink(target) or not os.path.isdir(target)):
                last = f"a file named {base} is in the way"
            elif os.path.isdir(target) and self._made.get(target, path) != path:
                last = f"{base} was created in this run for another archive"
            elif n and self._excluded(base, cfg):
                last = f"{base} would be skipped by the folder exclusions"
            else:
                break
            n += 1
            target = os.path.join(dest, f"{name} ({n})")
        if n:
            why += f"; using {os.path.basename(target)}/ because {last}"
        return target, why, False

    def _is_info_file(self, name, cfg):
        """A small file that rides along with release archives: .nfo/.diz/.sfv/checksums, or a junk pattern."""
        low = name.lower()
        if low.endswith(INFO_EXTS):
            return True
        return any(fnmatch.fnmatch(low, p.lower()) for p in cfg.get("junk_patterns", ()) if p.strip())

    def _only_pieces(self, top, cfg):
        """The archive pieces at the top level when that is ALL it holds (apart from info files), else []."""
        pieces = []
        for name, is_dir in top:
            if is_dir:
                return []
            if is_archive_piece(name):
                pieces.append(name)
            elif not self._is_info_file(name, cfg):
                return []
        return sorted(pieces)

    def _wrapper_base(self, name, cfg):
        """Folder name for an archive: short enough for ' (n)' to fit in 255 bytes, and never one the scan
        skips (@eaDir, .Trash*, a user exclude pattern), or nested archives inside it would be ignored."""
        def cut(s, limit):
            b = os.fsencode(s)
            if len(b) <= limit:
                return s
            k = limit
            while k > 0 and (b[k] & 0xC0) == 0x80:         # do not split a UTF-8 character
                k -= 1
            return os.fsdecode(b[:k]).rstrip(". ") or "archive"
        plain = cut(name, MAX_NAME_BYTES)
        if not self._excluded(plain, cfg):
            return plain
        # <name>_files; a wildcard pattern (.Trash*, skipme*) can still match that, so then put the word first
        for pre, suf in (("", "_files"), ("files_", ""), ("extracted_", "")):
            cand = pre + cut(name, MAX_NAME_BYTES - len(pre) - len(suf)) + suf
            if not self._excluded(cand, cfg):
                return cand
        return "archive_files"

    def _merge(self, stage, dest, cfg, path=None):
        """Move everything from the private folder into the archive's folder (or into a new folder named
        after the archive, when path is given and the setting says so)."""
        ow = cfg["overwrite"]
        out = {"files": 0, "bytes": 0, "skipped": 0}
        with self.merge_lock:
            dest, why, in_place = self._choose_target(stage, dest, cfg, path)
            if why:
                rel = os.path.basename(dest)
                if os.path.isdir(dest):
                    self.log("info", f"Extracted into the existing folder {rel}/ ({why})")
                else:
                    os.makedirs(dest)
                    self._apply_perm(dest, True, cfg)
                    self._made[dest] = path
                    self.log("info", f"Extracted into a new folder {rel}/ ({why})")
            for cur, dirs, files in os.walk(stage):
                rel = os.path.relpath(cur, stage)
                tdir = dest if rel == "." else os.path.join(dest, rel)
                if not os.path.isdir(tdir):
                    dirs[:] = []                           # parent clashed with a file: skipped
                    out["skipped"] += sum(len(fs) for _, _, fs in os.walk(cur))
                    continue
                for dn in dirs:
                    td = os.path.join(tdir, dn)
                    if os.path.lexists(td) and not os.path.isdir(td):
                        if not ow:
                            continue
                        os.remove(td)
                    os.makedirs(td, exist_ok=True)
                for fn in files:
                    src, dst = os.path.join(cur, fn), os.path.join(tdir, fn)
                    if os.path.lexists(dst):
                        if os.path.isdir(dst) and not os.path.islink(dst):
                            self.log("warn", f"Not replacing folder {dst} with a file of the same name")
                            out["skipped"] += 1
                            continue
                        if not ow:
                            if not (in_place and self._is_info_file(fn, cfg)):
                                out["skipped"] += 1        # (an info file that is already there is simply kept)
                            continue
                    try:
                        out["bytes"] += os.lstat(src).st_size
                    except OSError:
                        pass
                    os.replace(src, dst)
                    out["files"] += 1
                    if is_archive_piece(fn):
                        with self.lock:
                            if is_archive(fn):
                                self.nested.add(dst)       # came out of an archive: complete, not a download
                            if path is not None:
                                self._parents[dst] = path
                            if in_place:
                                self._inplace.add(dst)
        return out

    def _remove_stale_stages(self, d):
        """Work folders left behind by a crash or a forced stop."""
        try:
            for e in os.scandir(d):
                if e.name.startswith(STAGE_PREFIX) and e.is_dir(follow_symlinks=False):
                    shutil.rmtree(e.path, ignore_errors=True)
        except OSError:
            pass

    # ------------------------------------------------------------ cleanup
    def _archive_group(self, path):
        """The archive plus its other volumes (name.part2.rar, name.r00, ...)."""
        d, base = os.path.split(path)
        try:
            names = os.listdir(d)
        except OSError:
            return [path]
        m = NUMVOL_RE.match(base)
        if m:
            rx = re.compile("^" + re.escape(m.group(1)) + r"\.\d{3}$", re.I)
            return [os.path.join(d, n) for n in names if rx.match(n)] or [path]
        if base.lower().endswith(".zip"):
            return [path] + self._split_zip_parts(path)
        m = FIRST_PART_RE.match(base)
        if m:
            rx = re.compile("^" + re.escape(m.group(1)) + r"\.part\d+\.rar$", re.I)
            return [os.path.join(d, n) for n in names if rx.match(n)] or [path]
        m = RAR_RE.match(base)
        if m:
            rx = re.compile("^" + re.escape(m.group(1)) + r"\.r\d\d$", re.I)
            return [path] + [os.path.join(d, n) for n in names if rx.match(n)]
        return [path]

    def _act_text(self, mode, cfg):
        return {"move": f"move to {cfg['move_dir']}", "recycle": "move to the Recycle Bin",
                "keep": "keep"}.get(mode, "delete")

    def _dispose_archive(self, path, root, cfg):
        if cfg["archive_mode"] == "keep":
            with self.lock:
                self.kept.update(self._archive_group(path))
            return True
        ok = True
        for f in self._archive_group(path):
            ok = self._dispose(f, root, cfg, cfg["archive_mode"]) and ok
        return ok

    def _clean_junk(self, d, root, cfg):
        pats = [p.lower() for p in cfg["junk_patterns"] if p.strip()]
        if not pats:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if not e.is_file(follow_symlinks=False):
                continue
            if any(fnmatch.fnmatch(e.name.lower(), p) for p in pats):
                if cfg["dry_run"]:
                    self.log("info", f"[dry run] would {self._act_text(cfg['junk_mode'], cfg)} junk: {e.path}")
                else:
                    try:
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0
                    if self._dispose(e.path, root, cfg, cfg["junk_mode"]):
                        with self.lock:
                            self.status["junk"] += 1
                        self.stats.junk(size, cfg["junk_mode"])

    def _dispose(self, path, root, cfg, mode):
        try:
            if mode == "keep":
                return True
            if mode == "move":
                self._move(path, root, cfg)
            elif mode == "recycle":
                self._recycle(path, cfg)
            else:
                os.remove(path)
            return True
        except (OSError, RuntimeError) as e:
            self.log("warn", f"Could not {self._act_text(mode, cfg)} {path}: {e}")
            return False

    def _recycle(self, src, cfg):
        """Move into <share>/.Recycle.Bin/<path inside the share>, where the Unraid Recycle Bin plugin keeps
        files deleted over SMB (so they are listed and emptied by the plugin)."""
        parent, why = mounts.recycle_target(src, self._mounts)
        if why:
            raise RuntimeError(f"Recycle Bin not usable: {why}")
        dest = os.path.join(parent, os.path.basename(src))
        with self.move_lock:
            missing, p = [], parent
            while not os.path.isdir(p):
                missing.append(p)
                p = os.path.dirname(p)
            for m in reversed(missing):
                os.mkdir(m)
                self._apply_perm(m, True, cfg)
            if os.path.exists(dest):                 # never overwrite something already in the bin
                n = 1
                while os.path.exists(f"{dest}.{n}"):
                    n += 1
                dest = f"{dest}.{n}"
            shutil.move(src, dest)
        self._apply_perm(dest, False, cfg)
        return dest

    def _move(self, src, root, cfg):
        """Move into move_dir/<source folder name>/<path relative to source>."""
        base = os.path.basename(root.rstrip(os.sep)) or "root"
        dest = os.path.join(cfg["move_dir"], base, os.path.relpath(src, root))
        with self.move_lock:
            missing, p = [], os.path.dirname(dest)
            while not os.path.isdir(p):
                missing.append(p)
                p = os.path.dirname(p)
            for m in reversed(missing):
                os.mkdir(m)
                self._apply_perm(m, True, cfg)
            if os.path.exists(dest):                 # never overwrite something already moved
                n = 1
                while os.path.exists(f"{dest}.{n}"):
                    n += 1
                dest = f"{dest}.{n}"
            shutil.move(src, dest)
        self._apply_perm(dest, False, cfg)

    # ------------------------------------------------------------ permissions
    def _apply_perm(self, path, is_dir, cfg):
        try:
            if cfg["set_perms"]:
                os.chmod(path, int(cfg["dir_mode" if is_dir else "file_mode"], 8))
            if cfg["chown_enabled"]:
                os.chown(path, cfg["uid"], cfg["gid"])
        except OSError as e:
            self._warn_once(("perm", type(e).__name__), f"Permission change failed ({path}): {e}")

    def _fix_perms(self, d, cfg):
        """The directory itself, the files in it and its immediate sub-folders."""
        if not (cfg["set_perms"] or cfg["chown_enabled"]):
            return
        self._apply_perm(d, True, cfg)
        try:
            for e in os.scandir(d):
                if e.is_symlink():
                    continue
                if e.is_file(follow_symlinks=False):
                    self._apply_perm(e.path, False, cfg)
                elif e.is_dir(follow_symlinks=False):
                    self._apply_perm(e.path, True, cfg)
        except OSError:
            pass
