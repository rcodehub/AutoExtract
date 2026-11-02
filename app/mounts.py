"""What folders did the user actually map into this container?

Reads /proc/self/mountinfo, so the Settings page can show the container paths that exist
(the right-hand side of the Unraid "Host path -> Container path" mappings).
"""
import os
import re
import shutil

MOUNTINFO = os.environ.get("MOUNTINFO_PATH", "/proc/self/mountinfo")

# pseudo / system file systems we never want to offer
SKIP_FS = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "overlay",
    "shm", "nsfs", "securityfs", "debugfs", "tracefs", "fusectl", "configfs", "pstore", "bpf",
    "autofs", "binfmt_misc", "hugetlbfs", "rpc_pipefs", "selinuxfs",
}
SKIP_PREFIX = ("/proc", "/sys", "/dev", "/run", "/etc", "/app", "/usr", "/lib", "/bin", "/sbin", "/var/run")
HOST_PATH_RE = re.compile(r"^/mnt/(user0?|disk\d+|cache\w*|remotes|disks|addons)(/|$)")


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def list_mounts(config_dir="/config"):
    """Mapped folders inside the container: [{path, fstype, writable, free, total, config}]."""
    try:
        with open(MOUNTINFO) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out, seen = [], set()
    for ln in lines:
        left, sep, right = ln.partition(" - ")
        fields, rest = left.split(), right.split()
        if not sep or len(fields) < 5 or not rest:
            continue
        mp, fstype = _unescape(fields[4]), rest[0]
        if mp == "/" or mp in seen or fstype in SKIP_FS or mp.startswith(SKIP_PREFIX):
            continue
        if not os.path.isdir(mp):
            continue
        seen.add(mp)
        item = {"path": mp, "fstype": fstype, "config": os.path.normpath(mp) == os.path.normpath(config_dir),
                "writable": os.access(mp, os.W_OK | os.X_OK), "free": None, "total": None}
        try:
            u = shutil.disk_usage(mp)
            item["free"], item["total"] = u.free, u.total
        except OSError:
            pass
        out.append(item)
    return sorted(out, key=lambda m: m["path"])


def containing_mount(path, mounts):
    """The mapped folder that holds `path` (longest match), or None."""
    best = None
    p = os.path.normpath(path)
    for m in mounts:
        mp = os.path.normpath(m["path"])
        if p == mp or p.startswith(mp.rstrip(os.sep) + os.sep):
            if best is None or len(mp) > len(best["path"]):
                best = m
    return best


def path_info(path, mounts):
    """Can the app use this folder? Used for the status line under each source folder."""
    exists = os.path.isdir(path)
    info = {
        "exists": exists,
        "readable": exists and os.access(path, os.R_OK | os.X_OK),
        "writable": exists and os.access(path, os.W_OK | os.X_OK),
        "mount": None,
        "host_path": False,
    }
    m = containing_mount(path, mounts)
    if m:
        info["mount"] = m["path"]
    if not exists and HOST_PATH_RE.match(path):
        info["host_path"] = True   # looks like an Unraid host path, not a container path
    return info


def suggested_sources(config_dir="/config", skip=("/processed",)):
    """First-run guess: every writable mapped folder except app data and the processed folder."""
    skip = {os.path.normpath(s) for s in skip} | {os.path.normpath(config_dir)}
    return [m["path"] for m in list_mounts(config_dir)
            if os.path.normpath(m["path"]) not in skip and m["writable"]]
