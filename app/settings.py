"""Settings: defaults, first-run seeding from environment variables, validation, storage."""
import json
import os
import re
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
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


def _env_list(name):
    v = os.environ.get(name, "")
    return [x.strip() for x in re.split(r"[,\n]", v) if x.strip()]


def _env_bool(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def env_defaults():
    """Template variables only seed the very first settings file; the settings page wins after that."""
    d = dict(DEFAULTS)
    if _env_list("SOURCE_DIRS"):
        d["sources"] = _env_list("SOURCE_DIRS")
    d["schedule_enabled"] = _env_bool("SCHEDULE_ENABLED", d["schedule_enabled"])
    d["interval_minutes"] = _env_int("INTERVAL_MINUTES", d["interval_minutes"])
    d["run_on_start"] = _env_bool("RUN_ON_START", d["run_on_start"])
    d["threads"] = _env_int("THREADS", d["threads"])
    d["min_age_minutes"] = _env_int("MIN_AGE_MINUTES", d["min_age_minutes"])
    if _env_list("JUNK_PATTERNS"):
        d["junk_patterns"] = _env_list("JUNK_PATTERNS")
    for k in ("archive_mode", "junk_mode"):
        v = os.environ.get(k.upper(), "").strip().lower()
        if v in ("delete", "move"):
            d[k] = v
    d["move_dir"] = os.environ.get("MOVE_DIR", d["move_dir"]).strip()
    d["set_perms"] = _env_bool("SET_PERMS", d["set_perms"])
    d["file_mode"] = os.environ.get("FILE_MODE", d["file_mode"]).strip() or d["file_mode"]
    d["dir_mode"] = os.environ.get("DIR_MODE", d["dir_mode"]).strip() or d["dir_mode"]
    if os.environ.get("PUID") or os.environ.get("PGID"):
        d["chown_enabled"] = True
        d["uid"] = _env_int("PUID", d["uid"])
        d["gid"] = _env_int("PGID", d["gid"])
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
                errors.append(f"Move folder {s['move_dir']} must be outside the source folder {src}")
    if "move" in (s["archive_mode"], s["junk_mode"]) and not s["move_dir"]:
        errors.append("A move folder is required when an action is set to move")

    return s, errors


def load():
    with _lock:
        try:
            with open(SETTINGS_PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = None
        if data is None:                      # first run: seed from template variables
            data = env_defaults()
        clean, errors = validate(data)
        if errors:                            # a hand-damaged file must not stop the app
            clean = dict(DEFAULTS)
        return clean


def save(data):
    clean, errors = validate(data)
    if errors:
        return None, errors
    with _lock:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(clean, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    return clean, []
