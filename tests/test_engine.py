"""End-to-end checks for the extraction engine. Run:  python3 -I tests/test_engine.py"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
import tarfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from engine import Engine  # noqa: E402
import settings  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def base_cfg(**kw):
    cfg = dict(settings.DEFAULTS)
    cfg.update(min_age_minutes=0, threads=3, set_perms=True)
    cfg.update(kw)
    clean, errors = settings.validate(cfg)
    assert not errors, errors
    return clean


def run(engine, cfg, dry=False):
    assert engine.start(cfg, "test", dry)
    engine.thread.join(60)
    assert not engine.running
    return engine.snapshot()


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            if isinstance(data, tuple):          # ("file", path_on_disk)
                z.write(data[1], name)
            else:
                z.writestr(name, data)


def make_tgz(path, files):
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            tmp = tempfile.NamedTemporaryFile(delete=False)
            tmp.write(data.encode())
            tmp.close()
            t.add(tmp.name, arcname=name)
            os.unlink(tmp.name)


def tree(root):
    out = []
    for d, _, fs in os.walk(root):
        for f in fs:
            out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def fresh():
    work = tempfile.mkdtemp(prefix="ea-test-")
    return work, Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))


def build_tree(src):
    os.makedirs(f"{src}/a/sub", exist_ok=True)
    os.makedirs(f"{src}/b", exist_ok=True)
    inner = os.path.join(os.path.dirname(src), "inner.tgz")
    make_tgz(inner, {"deep.txt": "deep", "x.diz": "d"})
    make_zip(f"{src}/a/outer.zip", {"fa.txt": "a", "junk.DIZ": "j", "inner.tgz": ("file", inner)})
    make_zip(f"{src}/b/b1.zip", {"fb1.txt": "b1"})
    with open(f"{src}/b/bad.zip", "w") as f:
        f.write("not a zip")
    with open(f"{src}/keep.txt", "w") as f:
        f.write("keep")
    os.chmod(f"{src}/keep.txt", 0o600)


def test_nested_delete_and_perms():
    work, eng = fresh()
    src = os.path.join(work, "src")
    build_tree(src)
    s = run(eng, base_cfg(sources=[src]))
    t = tree(src)
    check("nested: results extracted", {"a/fa.txt", "a/deep.txt", "b/fb1.txt", "keep.txt"} <= set(t), t)
    check("nested: archives + junk gone", not any(x.endswith((".zip", ".tgz", ".diz", ".DIZ")) and "bad" not in x for x in t), t)
    check("nested: failed archive left in place", "b/bad.zip" in t)
    check("nested: counters (nested archive counted when it appears)",
          (s["found"], s["extracted"], s["failed"], s["remaining"]) == (4, 3, 1, 0), s)
    check("nested: final state idle/completed", s["state"] == "idle" and s["result"] == "completed")
    modes = {p: stat.S_IMODE(os.stat(os.path.join(src, p)).st_mode) for p in t}
    check("perms: files 666", all(m == 0o666 for m in modes.values()), modes)
    check("perms: dirs 777", all(stat.S_IMODE(os.stat(d).st_mode) == 0o777
                                 for d in [src, f"{src}/a", f"{src}/b"]))
    log = [e["msg"] for e in eng.log_since(0, 1000)]
    order = [i for i, m in enumerate(log) if m.startswith("Directory:")]
    dirs = [log[i] for i in order]
    check("order: one directory fully done before the next",
          dirs == [f"Directory: {src}", f"Directory: {src}/a", f"Directory: {src}/a/sub", f"Directory: {src}/b"], dirs)
    shutil.rmtree(work)


def test_move_mode():
    work, eng = fresh()
    src, mv = os.path.join(work, "src"), os.path.join(work, "moved")
    build_tree(src)
    run(eng, base_cfg(sources=[src], archive_mode="move", junk_mode="move", move_dir=mv))
    t = tree(mv)
    check("move: archives moved with structure", {"src/a/outer.zip", "src/a/inner.tgz", "src/b/b1.zip"} <= set(t), t)
    check("move: junk moved", any(x.lower().endswith(".diz") for x in t), t)
    check("move: source has no archives left (except failed)",
          [x for x in tree(src) if x.endswith((".zip", ".tgz"))] == ["b/bad.zip"], tree(src))
    # second run with the same names must not overwrite
    make_zip(f"{src}/b/b1.zip", {"fb1.txt": "again"})
    run(eng, base_cfg(sources=[src], archive_mode="move", junk_mode="move", move_dir=mv))
    check("move: clash gets .1 suffix", "src/b/b1.zip.1" in tree(mv), tree(mv))
    shutil.rmtree(work)


def test_dry_run():
    work, eng = fresh()
    src = os.path.join(work, "src")
    build_tree(src)
    before = tree(src)
    s = run(eng, base_cfg(sources=[src]), dry=True)
    check("dry run: nothing changed", tree(src) == before)
    check("dry run: reported", any("[dry run]" in e["msg"] for e in eng.log_since(0, 1000)))
    shutil.rmtree(work)


def test_min_age():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    make_zip(f"{src}/new.zip", {"n.txt": "n"})
    old = f"{src}/old.zip"
    make_zip(old, {"o.txt": "o"})
    past = time.time() - 3600
    os.utime(old, (past, past))
    s = run(eng, base_cfg(sources=[src], min_age_minutes=10))
    t = tree(src)
    check("min age: old extracted, new skipped", "o.txt" in t and "new.zip" in t and "n.txt" not in t, t)
    check("min age: deferred counted", s["deferred"] == 1, s)
    shutil.rmtree(work)


def test_excluded_and_stop():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(f"{src}/@eaDir")
    make_zip(f"{src}/@eaDir/skip.zip", {"s.txt": "s"})
    run(eng, base_cfg(sources=[src]))
    check("exclude: @eaDir untouched", tree(src) == ["@eaDir/skip.zip"], tree(src))
    # stop: a slow fake extractor
    fake = os.path.join(work, "bin")
    os.makedirs(fake)
    slow = os.path.join(fake, "unzip")
    with open(slow, "w") as f:
        f.write("#!/bin/sh\nsleep 30\n")
    os.chmod(slow, 0o755)
    os.makedirs(f"{src}/slow")
    make_zip(f"{src}/slow/s.zip", {"s.txt": "s"})
    eng.tools["unzip"] = slow
    eng.start(base_cfg(sources=[src]), "test", False)
    time.sleep(1.5)
    check("stop: shows a busy thread", any(s["path"] for s in eng.snapshot()["slots"]))
    t0 = time.time()
    eng.stop()
    eng.thread.join(20)
    s = eng.snapshot()
    check("stop: stops quickly, result 'stopped'", s["result"] == "stopped" and time.time() - t0 < 10, s)
    check("stop: interrupted archive is left alone", os.path.exists(f"{src}/slow/s.zip"))
    shutil.rmtree(work)


def test_settings():
    clean, errors = settings.validate({"sources": ["relative"], "threads": 99, "file_mode": "999"})
    check("settings: bad input rejected", len(errors) >= 3, errors)
    clean, errors = settings.validate({"sources": ["/a"], "archive_mode": "move", "move_dir": "/a/moved"})
    check("settings: move dir inside source rejected", any("must not be inside" in e for e in errors), errors)
    clean, errors = settings.validate({"sources": ["/a"], "archive_mode": "move"})
    check("settings: move needs a folder", any("required" in e for e in errors), errors)
    clean, errors = settings.validate({"sources": "/a\n/b, /c", "junk_patterns": "*.diz\n*.nfo"})
    check("settings: lists parsed", clean["sources"] == ["/a", "/b", "/c"] and clean["junk_patterns"] == ["*.diz", "*.nfo"] and not errors)


if __name__ == "__main__":
    print("tools:", {k: bool(v) for k, v in Engine(tempfile.mkdtemp(), os.devnull).tools.items()})
    for fn in (test_settings, test_nested_delete_and_perms, test_move_mode, test_dry_run,
               test_min_age, test_excluded_and_stop):
        fn()
    print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
