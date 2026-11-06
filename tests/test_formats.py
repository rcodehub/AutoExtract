"""Real-archive checks for the RAR paths of the engine: single RAR, multi-volume RAR5 set, old-style
name.rar + name.r00 set (when rar can make one), a RAR inside a ZIP, and a damaged RAR that must be
left in place. The archives are made with the rar CLI at test time. Needs unrar, 7z and rar; exits 2
(never skips) when one is missing. Run:  python3 -I tests/test_formats.py
"""
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
CFG_DIR = tempfile.mkdtemp(prefix="ea-formats-cfg-")
os.environ["DATA_DIR"] = CFG_DIR          # settings.py reads this at import time
from engine import Engine  # noqa: E402
import settings  # noqa: E402

FAILS = []
SEVEN_Z = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
MISSING = [name for name, found in (("unrar", shutil.which("unrar")), ("7z", SEVEN_Z),
                                    ("rar", shutil.which("rar"))) if not found]
if MISSING:
    print(f"ERROR: required tool(s) not installed: {', '.join(MISSING)}. "
          "Ubuntu: sudo add-apt-repository multiverse && sudo apt-get install unrar 7zip rar. "
          "These tests are not skipped when a tool is missing.", file=sys.stderr)
    sys.exit(2)


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def base_cfg(**kw):
    cfg = dict(settings.DEFAULTS)
    cfg.update(min_age_minutes=0, threads=4, set_perms=True, archive_mode="delete", junk_mode="delete")
    cfg.update(kw)
    clean, errors = settings.validate(cfg)
    assert not errors, errors
    return clean


def run(engine, cfg):
    assert engine.start(cfg, "test", False)
    engine.thread.join(120)
    assert not engine.running
    return engine.snapshot()


def tree(root):
    out = []
    for d, _, fs in os.walk(root):
        for f in fs:
            out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def stage_dirs(root):
    return [d for d, _, _ in os.walk(root) if ".autoextract-" in d]


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write_random(path, size):
    data = os.urandom(size)
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def rar(args, cwd):
    """Run the rar CLI; it is run inside the folder that holds the file, so the archive stores only the name."""
    r = subprocess.run(["rar", "a", "-idq", "-y", "-ep"] + args, cwd=cwd, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return r.returncode, r.stdout


def build(work):
    """Make the fixtures under work/src. Returns the expected sha256 per extracted file."""
    src = os.path.join(work, "src")
    expect = {}

    # (a) single RAR
    d = os.path.join(src, "single")
    os.makedirs(d)
    expect["single/single.bin"] = write_random(os.path.join(d, "single.bin"), 50_000)
    code, out = rar(["single.rar", "single.bin"], d)
    os.remove(os.path.join(d, "single.bin"))
    check("make: single RAR created", code == 0 and os.path.isfile(os.path.join(d, "single.rar")), out[-200:])

    # (b) multi-volume RAR5 set: name.part1.rar, name.part2.rar, ...
    d = os.path.join(src, "multi")
    os.makedirs(d)
    expect["multi/multi.bin"] = write_random(os.path.join(d, "multi.bin"), 60_000)
    code, out = rar(["-ma5", "-v20k", "multi.rar", "multi.bin"], d)
    os.remove(os.path.join(d, "multi.bin"))
    parts = sorted(glob.glob(os.path.join(d, "multi.part*.rar")))
    check("make: multi-volume RAR5 set has 2+ parts", code == 0 and len(parts) >= 2, (code, out[-200:], parts))

    # (c) old-style name.rar + name.r00 set: needs rar to write that naming scheme
    d = os.path.join(src, "old")
    os.makedirs(d)
    old_expect = write_random(os.path.join(d, "old.bin"), 60_000)
    code, out = rar(["-v20k", "-vn", "old.rar", "old.bin"], d)
    os.remove(os.path.join(d, "old.bin"))
    old_ok = code == 0 and os.path.isfile(os.path.join(d, "old.rar")) and os.path.isfile(os.path.join(d, "old.r00"))
    if old_ok:
        expect["old/old.bin"] = old_expect
    else:
        print("NOTE: this rar cannot write old-style name.rar + name.r00 volumes; skipping only that check")
        shutil.rmtree(d)

    # (d) a RAR inside a ZIP (nested archive)
    d = os.path.join(src, "nested")
    os.makedirs(d)
    stage = os.path.join(work, "stage")
    os.makedirs(stage)
    expect["nested/nested.bin"] = write_random(os.path.join(stage, "nested.bin"), 20_000)
    code, out = rar(["nested.rar", "nested.bin"], stage)
    check("make: nested RAR created", code == 0, out[-200:])
    with zipfile.ZipFile(os.path.join(d, "nest.zip"), "w") as z:
        z.write(os.path.join(stage, "nested.rar"), "nested.rar")

    # (e) damaged RAR: stored (-m0), then a range in the middle of its data is overwritten
    d = os.path.join(src, "broken")
    os.makedirs(d)
    write_random(os.path.join(stage, "broken.bin"), 80_000)
    code, out = rar(["-m0", "broken.rar", "broken.bin"], stage)
    shutil.copy(os.path.join(stage, "broken.rar"), os.path.join(d, "broken.rar"))
    with open(os.path.join(d, "broken.rar"), "r+b") as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(size // 2)
        f.write(b"\xaa" * 512)
    check("make: damaged RAR created", code == 0 and os.path.isfile(os.path.join(d, "broken.rar")), out[-200:])
    return src, expect, old_ok


def main():
    work = tempfile.mkdtemp(prefix="ea-formats-")
    try:
        print("tools:", {"unrar": shutil.which("unrar"), "7z": SEVEN_Z, "rar": shutil.which("rar")})
        src, expect, old_ok = build(work)
        eng = Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))
        s = run(eng, base_cfg(sources=[src]))
        t = tree(src)

        for rel, digest in expect.items():
            p = os.path.join(src, rel)
            check(f"extracted with correct content: {rel}", os.path.isfile(p) and sha(p) == digest,
                  (rel, os.path.isfile(p)))

        check("single RAR removed", not os.path.exists(os.path.join(src, "single", "single.rar")), t)
        check("multi-volume: every part removed", not any(x.startswith("multi/multi.part") for x in t), t)
        if old_ok:
            check("old-style volumes: every part removed", not any(x.startswith("old/old.r") for x in t), t)
        check("RAR in ZIP: zip and nested RAR removed", "nested/nest.zip" not in t
              and "nested/nested.rar" not in t, t)
        check("damaged RAR: reported failed and left in place",
              "broken/broken.rar" in t and s["failed"] == 1
              and any(f["path"].endswith("broken.rar") for f in s["failures"]), (t, s["failures"]))
        check("damaged RAR: no partial output kept", "broken/broken.bin" not in t, t)
        want = len(expect) + 1          # one expect entry per set, but the ZIP-with-RAR set is two archives
        check("counts: archives extracted as expected", s["extracted"] == want, (s["extracted"], want, s))
        check("no .autoextract-* work folders left", not stage_dirs(src), stage_dirs(src))
        check("run finished", s["state"] == "idle" and s["result"] == "completed", (s["state"], s["result"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(CFG_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
    print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
