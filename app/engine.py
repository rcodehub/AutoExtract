"""Extraction engine.

Walks the source folders ONE DIRECTORY AT A TIME. For each directory it
extracts every archive in it (into the same directory), removes/moves the
archive and any junk files, rescans the same directory for nested archives,
and only then descends into the sub-directories.
"""
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


class Engine:
    def __init__(self, log_dir, history_path):
        self.lock = threading.RLock()
        self.move_lock = threading.Lock()
        self.merge_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.procs = set()
        self.thread = None
        self.history_path = history_path
        # archives that came out of an archive, remembered between scans: they are complete files,
        # so the "skip recent files" rule (meant for downloads in progress) never applies to them
        self.nested_path = os.path.join(os.path.dirname(history_path) or ".", "extracted-archives.json")
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
        self.nested = set()          # archives that came out of an archive in this run (never too recent)
        self._slots = []

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
                ({"n": i + 1, "path": sl["path"], "elapsed": int(now - sl["t"])} if sl
                 else {"n": i + 1, "path": None, "elapsed": 0})
                for i, sl in enumerate(self._slots)
            ] if self.status["state"] != "idle" else []
            failed = self.failures
            s["found"] = len(self.seen)
            s["failed"] = len(failed)
            s["done"] = s["extracted"] + s["failed"]
            s["remaining"] = max(0, s["found"] - s["done"])
            s["deferred"] = len(self.deferred)
            s["failures"] = failed[-50:]
            s["tools"] = self.tools
            return s

    # ------------------------------------------------------------ control
    def start(self, cfg, trigger="manual", dry_run=False):
        with self.lock:
            if self.running:
                return False
            self.stop_event.clear()
            self.thread = threading.Thread(
                target=self._run, args=(dict(cfg), trigger, dry_run), daemon=True)
            self.status = self._blank_status()
            self.status.update(state="running", trigger=trigger, dry_run=dry_run,
                               started_at=time.time())
            self.seen = set()
            self.failures = []
            self.failed_paths = set()
            self.deferred = set()
            self.nested = self._load_nested()
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
    def _run(self, cfg, trigger, dry_run):
        cfg["dry_run"] = dry_run
        result, message = "completed", ""
        self.log("info", f"=== Run started ({trigger}{', dry run' if dry_run else ''}) ===")
        try:
            sources = []
            for s in cfg["sources"]:
                s = os.path.realpath(s)
                if os.path.isdir(s):
                    sources.append(s)
                else:
                    self.log("warn", f"Source folder not found, skipping: {s}")
            if not sources:
                raise RuntimeError("No valid source folders configured")

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
        if name.startswith(STAGE_PREFIX):
            return True
        low = name.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in cfg["exclude_dirs"])

    def _group_mtime(self, path):
        """Newest mtime of the archive and all its other volumes."""
        newest = 0
        for f in self._archive_group(path):
            try:
                newest = max(newest, os.path.getmtime(f))
            except OSError:
                pass
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
            if new:
                mins = max(0, (time.time() - changed) / 60)
                self.log("info", f"Skipped for now (changed {mins:.0f} min ago, could still be downloading; "
                                 f"setting: skip archives newer than {age} min): {path}")
        return ok

    def _register(self, path):
        with self.lock:
            self.seen.add(path)
            self.deferred.discard(path)

    def _prescan(self, root, cfg):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not self._excluded(d, cfg)
                                 and not os.path.islink(os.path.join(dirpath, d)))
            for fn in filenames:
                if is_archive(fn) and not is_secondary_volume(fn):
                    p = os.path.join(dirpath, fn)
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
                    and not is_secondary_volume(e.name) and self._eligible(e.path, cfg)):
                out.append(e.path)
        return out

    # ------------------------------------------------------------ one directory
    def _process_dir(self, d, root, cfg, pool):
        if self.stop_event.is_set():
            return
        dry = cfg["dry_run"]
        with self.lock:
            self.status["current_dir"] = d
        self.log("info", f"Directory: {d}")

        self._remove_stale_stages(d)
        pending = []                     # failed once in parallel: retried alone at the end
        for p in range(1, cfg["max_passes"] + 1):
            if self.stop_event.is_set():
                return
            with self.lock:
                self.status["pass"] = p
            todo = []
            for a in self._list_archives(d, cfg):
                self._register(a)        # picks up nested archives that just appeared
                if a not in self.failed_paths and a not in pending:
                    todo.append(a)
            did = 0
            if dry:
                for a in todo:
                    act = f"move to {cfg['move_dir']}" if cfg["archive_mode"] == "move" else "delete"
                    self.log("info", f"[dry run] would extract, then {act}: {a}")
            elif todo:
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

        if not dry and not self.stop_event.is_set():
            self._fix_perms(d, cfg)

        try:
            subs = sorted(e.path for e in os.scandir(d)
                          if e.is_dir(follow_symlinks=False) and not self._excluded(e.name, cfg))
        except OSError as e:
            self.log("warn", f"Cannot read {d}: {e}")
            subs = []
        for s in subs:
            self._process_dir(s, root, cfg, pool)

    # ------------------------------------------------------------ worker
    def _acquire_slot(self, path):
        with self.lock:
            for i, sl in enumerate(self._slots):
                if sl is None:
                    self._slots[i] = {"path": path, "t": time.time()}
                    return i
        return 0

    def _release_slot(self, i):
        with self.lock:
            self._slots[i] = None

    def _work(self, path, root, cfg, final):
        """Returns "ok", "retry" (failed - try again alone later) or "fail"."""
        if self.stop_event.is_set():
            return "fail"
        slot = self._acquire_slot(path)
        try:
            self.log("info", f"[thread {slot + 1}] Extracting {path}")
            t0 = time.time()
            ok, err, out = self._extract(path, os.path.dirname(path), cfg)
            secs = time.time() - t0
            if ok:
                bytes_in = 0
                for f in self._archive_group(path):
                    try:
                        bytes_in += os.path.getsize(f)
                    except OSError:
                        pass
                was_nested = path in self.nested
                if self._dispose_archive(path, root, cfg):
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

    def _exec(self, cmd):
        try:
            # own process group so "Stop" can kill the extractor and anything it spawned
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace",
                                 start_new_session=True)
        except OSError as e:
            return None, str(e)
        with self.lock:
            self.procs.add(p)
        try:
            out, _ = p.communicate()
        finally:
            with self.lock:
                self.procs.discard(p)
        return p.returncode, out or ""

    def _extract(self, path, dest, cfg):
        """Extract into a private folder next to the archive, then move the result into place, so
        parallel extractions never write the same file at the same time. If the first extractor
        fails, the next one (7-Zip) is tried."""
        tools = [tool for tool, _ in self._commands(path, dest, cfg)]
        if not tools:
            return False, "No extractor installed for this file type", None
        errors = []
        for tool in tools:
            if self.stop_event.is_set():
                return False, "stopped", None
            try:
                stage = tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=dest)
            except OSError as e:
                return False, f"cannot create a work folder in {dest}: {e}", None
            try:
                cmd = dict(self._commands(path, stage, cfg))[tool]
                code, out = self._exec(cmd)
                if code == 0:
                    out = self._merge(stage, dest, cfg)
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

    def _merge(self, stage, dest, cfg):
        """Move everything from the private folder into the archive's folder."""
        ow = cfg["overwrite"]
        out = {"files": 0, "bytes": 0}
        with self.merge_lock:
            for cur, dirs, files in os.walk(stage):
                rel = os.path.relpath(cur, stage)
                tdir = dest if rel == "." else os.path.join(dest, rel)
                if not os.path.isdir(tdir):
                    dirs[:] = []                           # parent clashed with a file: skipped
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
                            continue
                        if not ow:
                            continue
                    try:
                        out["bytes"] += os.lstat(src).st_size
                    except OSError:
                        pass
                    os.replace(src, dst)
                    out["files"] += 1
                    if is_archive(fn):
                        with self.lock:
                            self.nested.add(dst)           # came out of an archive: complete, not a download
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

    def _dispose_archive(self, path, root, cfg):
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
                    act = f"move to {cfg['move_dir']}" if cfg["junk_mode"] == "move" else "delete"
                    self.log("info", f"[dry run] would {act} junk: {e.path}")
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
            if mode == "move":
                self._move(path, root, cfg)
            else:
                os.remove(path)
            return True
        except OSError as e:
            self.log("warn", f"Could not {'move' if mode == 'move' else 'delete'} {path}: {e}")
            return False

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
