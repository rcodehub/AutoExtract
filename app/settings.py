"""Settings: defaults, first-start values from the mapped folders, validation, storage."""
import json
import os
import re
import threading

import mounts

# App data: settings.json, history, logs and the login key. Mapped to e.g. /mnt/user/appdata/autoextract.
CONFIG_DIR = os.environ.get("DATA_DIR") or "/data"
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "sources": [],                    # folders to scan (paths as seen inside the container)
    "schedule_enabled": False,        # scan on an interval
    "interval_minutes": 60,           # minutes between the end of one scan and the start of the next
    "run_on_start": False,            # also scan right after the container starts
    "threads": 4,                     # archives extracted in parallel within a directory
    "min_age_minutes": 5,             # ignore archives modified more recently than this (still downloading)
    "max_passes": 25,                 # nesting-depth safety cap per directory
    "overwrite": True,                # overwrite existing files when extracting
    "archive_mode": "delete",         # what to do with an archive after a successful extract: delete | move
    "junk_mode": "delete",            # what to do with junk files: delete | move
    "move_dir": "",                   # where "move" puts things
    "junk_patterns": ["*.diz"],       # file name patterns (case-insensitive)
    "exclude_dirs": ["@eaDir", ".Recycle.Bin", "#recycle", ".Trash*"],
    "set_perms": True,                # chmod everything that was processed
    "file_mode": "666",
    "dir_mode": "777",
    "chown_enabled": False,           # also set the owner
    "uid": 99,                        # Unraid: nobody
    "gid": 100,                       # Unraid: users
}

_lock = threading.Lock()


def env_defaults():
    """First-start settings, from the folders mapped in the template: /downloads is scanned, and if
    /trash is mapped, processed archives and junk are moved there instead of deleted."""
    d = dict(DEFAULTS)
    d["sources"] = mounts.suggested_sources(CONFIG_DIR)
    move = mounts.suggested_move_dir(CONFIG_DIR)
    if move:
        d.update(move_dir=move, archive_mode="move", junk_mode="move")
    return d


def _lines(value):
    """Accept a list or a newline/comma separated string; return clean unique entries."""
    if isinstance(value, str):
        value = re.split(r"[\n,]", value)
    out = []
    for x in value or []:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def _within(path, parent):
    path, parent = os.path.normpath(path), os.path.normpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def validate(data):
    """Return (clean_settings, errors). Unknown keys are dropped, missing ones get defaults."""
    errors = []
    s = dict(DEFAULTS)

    def num(key, lo, hi, label):
        try:
            v = int(data.get(key, s[key]))
        except (TypeError, ValueError):
            errors.append(f"{label} must be a whole number")
            return
        if not lo <= v <= hi:
            errors.append(f"{label} must be between {lo} and {hi}")
            return
        s[key] = v

    def boolean(key):
        v = data.get(key, s[key])
        s[key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")

    s["sources"] = [os.path.normpath(p) for p in _lines(data.get("sources", s["sources"]))]
    for p in s["sources"]:
        if not p.startswith("/"):
            errors.append(f"Source folder must be an absolute path: {p}")
        elif _within(p, CONFIG_DIR):
            errors.append(f"Source folder {p} is the app data folder - choose a folder to scan such as /downloads")

    for k in ("schedule_enabled", "run_on_start", "overwrite", "set_perms", "chown_enabled"):
        boolean(k)
    num("interval_minutes", 1, 525600, "Scan interval")
    num("threads", 1, 32, "Threads")
    num("min_age_minutes", 0, 100000, "Minimum file age")
    num("max_passes", 1, 100, "Max nesting passes")
    num("uid", 0, 4294967295, "User ID")
    num("gid", 0, 4294967295, "Group ID")

    for k, label in (("archive_mode", "Archive action"), ("junk_mode", "Junk action")):
        v = str(data.get(k, s[k])).lower()
        if v not in ("delete", "move"):
            errors.append(f"{label} must be delete or move")
        else:
            s[k] = v

    s["junk_patterns"] = _lines(data.get("junk_patterns", s["junk_patterns"]))
    s["exclude_dirs"] = _lines(data.get("exclude_dirs", s["exclude_dirs"]))

    for k, label in (("file_mode", "File permissions"), ("dir_mode", "Folder permissions")):
        v = str(data.get(k, s[k])).strip()
        if not re.fullmatch(r"[0-7]{3,4}", v):
            errors.append(f"{label} must be an octal mode such as 666 or 777")
        else:
            s[k] = v

    s["move_dir"] = str(data.get("move_dir", "") or "").strip()
    if s["move_dir"]:
        s["move_dir"] = os.path.normpath(s["move_dir"])
        if not s["move_dir"].startswith("/"):
            errors.append("Move folder must be an absolute path")
        for src in s["sources"]:
            if _within(s["move_dir"], src):
                errors.append(f"Move folder {s['move_dir']} must not be inside the source folder {src}")
            elif _within(src, s["move_dir"]):
                errors.append(f"Source folder {src} must not be inside the move folder {s['move_dir']} - "
                              "give the scanned folder and the processed folder separate mappings")
    if "move" in (s["archive_mode"], s["junk_mode"]) and not s["move_dir"]:
        errors.append("A move folder is required when an action is set to move")

    return s, errors


def load_with_report():
    """Return (settings, problems, source). One bad value never throws the others away:
    every valid saved setting is kept, only the invalid ones fall back to their default.
    source: "file" (saved settings), "defaults" (nothing saved yet) or "unreadable"."""
    with _lock:
        data, source = None, "defaults"
        try:
            with open(SETTINGS_PATH) as f:
                data = json.load(f)
            source = "file"
            if not isinstance(data, dict):
                data, source = None, "unreadable"
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            source = "unreadable"
        if data is None:                      # nothing usable saved: first-run defaults
            data = env_defaults()
        clean, problems = validate(data)
        return clean, problems, source


def load():
    return load_with_report()[0]


def storage_info():
    """Where settings live and whether they are really being kept."""
    try:
        saved_at = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        saved_at = None
    return {"path": SETTINGS_PATH, "saved_at": saved_at}


def save(data):
    clean, errors = validate(data)
    if errors:
        return None, errors
    with _lock:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = SETTINGS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(clean, f, indent=2)
            os.replace(tmp, SETTINGS_PATH)
            with open(SETTINGS_PATH) as f:        # read it back: prove it was stored
                if json.load(f) != clean:
                    raise OSError("settings file did not read back correctly")
        except (OSError, ValueError) as e:
            return None, [f"Could not store settings in {SETTINGS_PATH} ({e}). "
                          "Check that the App data folder is mapped to a writable folder."]
    return clean, []
