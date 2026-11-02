"""Path / mount handling checks. Run:  python3 -I tests/test_paths.py"""
import importlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


root = tempfile.mkdtemp(prefix="ea-paths-")
cfg, dl, proc, media = (os.path.join(root, d) for d in ("config", "downloads", "processed", "media"))
for d in (cfg, dl, proc, media):
    os.makedirs(d)
os.chmod(media, 0o555)   # read-only for non-root; root can still write, so only checked loosely


def mi(i, mp, fs, src="x"):
    return f"{i} 1 0:{i} /somewhere {mp} rw,noatime master:1 - {fs} {src} rw"


info = "\n".join([
    "20 1 0:20 / / rw - overlay overlay rw",
    "21 20 0:21 / /proc rw - proc proc rw",
    "22 20 0:22 / /dev rw - tmpfs tmpfs rw",
    "23 20 0:23 /etc/hosts /etc/hosts rw - ext4 /dev/sda1 rw",
    mi(30, cfg, "fuse.shfs", "shfs"),
    mi(31, dl, "fuse.shfs", "shfs"),
    mi(32, proc, "xfs", "/dev/md1"),
    mi(33, media.replace(" ", r"\040"), "btrfs", "/dev/sdb1"),
]) + "\n"
mi_path = os.path.join(root, "mountinfo")
open(mi_path, "w").write(info)

os.environ.update(MOUNTINFO_PATH=mi_path, CONFIG_DIR=cfg, NO_SCHEDULER="1")
for k in ("SOURCE_DIRS", "UI_PASSWORD", "UI_USERNAME"):
    os.environ.pop(k, None)
for m in ("mounts", "settings", "engine", "main"):
    sys.modules.pop(m, None)
mounts = importlib.import_module("mounts")
settings = importlib.import_module("settings")
main = importlib.import_module("main")

ms = mounts.list_mounts(cfg)
paths = [m["path"] for m in ms]
check("system mounts are hidden", not any(p in ("/", "/proc", "/dev", "/etc/hosts") for p in paths), paths)
check("mapped folders are listed", {cfg, dl, proc, media} <= set(paths), paths)
check("config folder is flagged", [m["path"] for m in ms if m["config"]] == [cfg])
check("free space reported", all(m["free"] is not None for m in ms))

sug = mounts.suggested_sources(cfg, skip=(proc,))
check("first-run suggestion = data folders only", sug == [media, dl] or sug == [dl, media] or set(sug) == {dl, media}, sug)

pi = mounts.path_info(dl + "/sub/dir", ms)
check("path_info: missing folder inside a mount", not pi["exists"] and pi["mount"] == dl and not pi["host_path"], pi)
pi = mounts.path_info("/mnt/user/Downloads", ms)
check("path_info: Unraid host path is recognised", not pi["exists"] and pi["host_path"], pi)
pi = mounts.path_info(dl, ms)
check("path_info: good folder", pi["exists"] and pi["readable"] and pi["writable"] and pi["mount"] == dl, pi)

# validation, both directions
s, e = settings.validate({"sources": [dl], "archive_mode": "move", "move_dir": dl + "/done"})
check("move folder inside a source is refused", any("must not be inside the source" in x for x in e), e)
s, e = settings.validate({"sources": [proc + "/incoming"], "archive_mode": "move", "move_dir": proc})
check("source inside the move folder is refused", any("must not be inside the move folder" in x for x in e), e)
s, e = settings.validate({"sources": [dl], "archive_mode": "move", "move_dir": proc})
check("separate folders are accepted", not e, e)

# first-run seeding uses detected mounts when SOURCE_DIRS is not set
# (in the real container the processed folder is /processed; here it is a temp path)
mounts.suggested_sources.__defaults__ = ("/config", (proc,))
os.remove(os.path.join(cfg, "settings.json")) if os.path.exists(os.path.join(cfg, "settings.json")) else None
seed = settings.load()
check("first run: sources auto-detected (not /processed or /config)",
      proc not in seed["sources"] and cfg not in seed["sources"] and dl in seed["sources"], seed["sources"])

# API
c = main.app.test_client()
d = c.get("/api/mounts").get_json()
check("api/mounts", {m["path"] for m in d["mounts"]} >= {dl, proc}, d)
d = c.get("/api/pathinfo?path=/mnt/user/foo").get_json()
check("api/pathinfo flags host paths", d["info"]["host_path"] is True, d)
r = c.post("/api/settings", json={"sources": ["/mnt/user/nope", dl], "move_dir": proc, "archive_mode": "move"})
d = c.get("/api/settings").get_json()
check("api/settings reports per-source info",
      d["sources_info"]["/mnt/user/nope"]["host_path"] and d["sources_info"][dl]["exists"] and d["move_info"]["exists"], d.get("sources_info"))

print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
