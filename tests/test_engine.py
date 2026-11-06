"""End-to-end checks for the extraction engine. Run:  python3 -I tests/test_engine.py"""
import json
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
    cfg = dict(settings.DEFAULTS, archive_mode="delete", junk_mode="delete")
    cfg.update(min_age_minutes=0, threads=3, set_perms=True)
    cfg.update(kw)
    clean, errors = settings.validate(cfg)
    assert not errors, errors
    return clean


def run(engine, cfg, dry=False, trigger="test"):
    assert engine.start(cfg, trigger, dry)
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
    inner = os.path.join(work, "inner.zip")
    make_zip(inner, {"deep.txt": "d"})       # nested archive with a brand-new timestamp inside
    make_zip(old, {"o.txt": "o", "inner.zip": ("file", inner)})
    past = time.time() - 3600
    os.utime(old, (past, past))
    s = run(eng, base_cfg(sources=[src], min_age_minutes=10))
    t = tree(src)
    check("min age: old extracted, new skipped", "o.txt" in t and "new.zip" in t and "n.txt" not in t, t)
    check("min age: deferred counted", s["deferred"] == 1, s)
    w = s.get("waiting") or []
    check("min age: waiting folders listed with the archive and when it is ready",
          len(w) == 1 and w[0]["dir"] == src and w[0]["names"] == ["new.zip"] and w[0]["count"] == 1
          and abs(w[0]["ready_at"] - (os.path.getmtime(f"{src}/new.zip") + 600)) < 2, w)
    check("min age: nested archive with a recent date is still extracted",
          "deep.txt" in t and "inner.zip" not in t, t)
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


def stage_dirs(root):
    return [d for d, _, _ in os.walk(root) if ".autoextract-" in d]


def test_parallel_same_names():
    """Many archives in one folder that all contain the same file names, extracted 8 at a time."""
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    big = os.urandom(3_000_000)
    for i in range(12):
        make_zip(f"{src}/cd{i:02d}.zip", {"file_id.diz": "x", "release.nfo": big, f"part{i:02d}.bin": "p"})
    s = run(eng, base_cfg(sources=[src], threads=8))
    t = tree(src)
    check("parallel same names: all extracted, none failed", s["extracted"] == 12 and s["failed"] == 0, s)
    check("parallel same names: output complete", all(f"part{i:02d}.bin" in t for i in range(12))
          and "release.nfo" in t and not any(x.endswith(".zip") for x in t), t)
    check("parallel same names: no work folders left", not stage_dirs(src), stage_dirs(src))
    shutil.rmtree(work)


def test_parts_spread_over_zips():
    """Each zip holds one piece of the real content (like scene releases holding RAR parts)."""
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    inner = os.path.join(work, "inner.zip")
    make_zip(inner, {"movie/movie.mkv": "m"})
    make_zip(f"{src}/r01.zip", {"set.zip": ("file", inner), "file_id.diz": "a"})
    make_zip(f"{src}/r02.zip", {"readme.txt": "b", "file_id.diz": "a"})
    s = run(eng, base_cfg(sources=[src], threads=4))
    t = tree(src)
    check("parts over zips: nested content extracted", "movie/movie.mkv" in t and s["failed"] == 0, (t, s))
    shutil.rmtree(work)


def test_broken_archive():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    open(f"{src}/bad.zip", "wb").write(b"PK\x03\x04 this is not really a zip" * 50)
    make_zip(f"{src}/good.zip", {"g.txt": "g"})
    s = run(eng, base_cfg(sources=[src], threads=4))
    t = tree(src)
    err = s["failures"][0]["error"] if s["failures"] else ""
    check("broken: left in place, good one extracted", "bad.zip" in t and "g.txt" in t and s["failed"] == 1, (t, s))
    check("broken: error explained", "exit code" in err and "(" in err, err)
    check("broken: retried on its own", any("Retrying on its own" in m["msg"] for m in eng.log_since(0)))
    check("broken: no work folders left", not stage_dirs(src), stage_dirs(src))
    shutil.rmtree(work)


def test_volumes():
    """Split zips (name.zip + name.z01) and 7-Zip volumes (name.7z.001) - needs zip and 7z."""
    z7 = shutil.which("7z") or shutil.which("7zz")
    if not (shutil.which("zip") and z7):
        print("SKIP volumes (zip/7z not installed)")
        return
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(f"{src}/a")
    os.makedirs(f"{src}/b")
    payload = os.path.join(work, "payload.bin")
    open(payload, "wb").write(os.urandom(300_000))
    subprocess.run(["zip", "-q", "-s", "100k", f"{src}/a/split.zip", "payload.bin"], cwd=work, check=True)
    subprocess.run([z7, "a", "-bd", "-v100k", f"{src}/b/vol.7z", payload], check=True, stdout=subprocess.DEVNULL)
    s = run(eng, base_cfg(sources=[src], threads=4))
    t = tree(src)
    check("volumes: split zip and 7z volumes extracted", s["failed"] == 0 and t.count("a/payload.bin") == 1
          and "b/payload.bin" in t, (t, s["failures"]))
    check("volumes: every part cleaned up", not any(".z0" in x or ".7z.0" in x or x.endswith(".zip") for x in t), t)
    shutil.rmtree(work)



def test_nested_remembered_between_runs():
    """A nested archive left behind by an earlier run (e.g. it failed then) must not be skipped
    later just because its own date is recent."""
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    outer = f"{src}/outer.zip"
    make_zip(outer, {"inner.zip": b"PK\x03\x04 broken for now" * 20})   # entry date = now
    past = time.time() - 3600
    os.utime(outer, (past, past))
    s = run(eng, base_cfg(sources=[src], min_age_minutes=10))
    check("remembered: run 1 extracts outer, inner fails", "inner.zip" in tree(src) and s["failed"] == 1, s)
    make_zip(f"{src}/inner.zip", {"deep.txt": "d"})                     # fixed, still a recent date
    eng2 = Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))  # new process
    s = run(eng2, base_cfg(sources=[src], min_age_minutes=10))
    t = tree(src)
    check("remembered: run 2 extracts it despite the recent date", "deep.txt" in t and "inner.zip" not in t, (t, s))
    make_zip(f"{src}/fresh.zip", {"f.txt": "f"})                         # a real new download
    s = run(eng2, base_cfg(sources=[src], min_age_minutes=10))
    check("remembered: genuinely new downloads are still skipped and logged", "fresh.zip" in tree(src)
          and s["deferred"] == 1 and any("Skipped for now" in m["msg"] for m in eng2.log_since(0)), s)
    shutil.rmtree(work)



def test_stats():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    inner = os.path.join(work, "inner.zip")
    make_zip(inner, {"a.bin": b"a" * 1000, "b.bin": b"b" * 500})
    make_zip(f"{src}/outer.zip", {"inner.zip": ("file", inner), "c.txt": "c" * 100})
    open(f"{src}/x.diz", "w").write("junk")
    open(f"{src}/bad.zip", "wb").write(b"PK\x03\x04 nope" * 10)
    s = run(eng, base_cfg(sources=[src]))
    st = eng.stats.snapshot()
    t = st["totals"]
    check("stats: archives/files counted", t["archives"] == 2 and t["files"] == 4, t)
    check("stats: bytes out = sizes of extracted files",
          t["bytes_out"] == 1500 + 100 + os.path.getsize(inner), t)
    check("stats: freed space, junk, failures, nested, runs",
          t["freed"] > 0 and t["junk"] == 1 and t["failed"] == 1 and t["nested"] == 1 and t["runs"] == 1, t)
    check("stats: per type and per day", st["types"].get("zip") == 2
          and st["days"][time.strftime("%Y-%m-%d")]["archives"] == 2, st)
    check("stats: records", st["records"]["largest"]["value"] > 0 and st["records"]["most_files"]["value"] == 2, st["records"])
    h = eng.history[-1]
    check("stats: run size in history", h["files_out"] == 4 and h["bytes_out"] == t["bytes_out"], h)
    eng2 = Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))
    check("stats: survive a restart", eng2.stats.snapshot()["totals"]["archives"] == 2)
    eng2.stats.reset()
    check("stats: reset", eng2.stats.snapshot()["totals"]["archives"] == 0)
    shutil.rmtree(work)



def fake_mounts(work, entries):
    """entries: [(container path, root inside the file system, fstype, device)] -> mountinfo file for mounts.py"""
    import mounts
    lines = ["20 1 0:20 / / rw - overlay overlay rw"]
    for i, (mp, root, fs, dev) in enumerate(entries):
        lines.append(f"{30 + i} 20 0:{30 + i} {root} {mp} rw,noatime - {fs} {dev} rw")
    path = os.path.join(work, "mountinfo")
    open(path, "w").write("\n".join(lines) + "\n")
    mounts.MOUNTINFO = path


def test_recycle_bin():
    import mounts
    old = mounts.MOUNTINFO
    try:
        # 1) the share itself is mapped (/mnt/user/downloads -> /downloads): bin at <share>/.Recycle.Bin
        work, eng = fresh()
        share = os.path.join(work, "downloads")
        os.makedirs(f"{share}/Show/S01")
        os.makedirs(f"{share}/.Recycle.Bin/old")
        make_zip(f"{share}/.Recycle.Bin/old/deleted.zip", {"x.txt": "x"})      # must never be touched
        make_zip(f"{share}/Show/S01/ep.zip", {"ep.mkv": "v"})
        open(f"{share}/Show/S01/file.diz", "w").write("j")
        fake_mounts(work, [(share, "/downloads", "fuse.shfs", "shfs")])
        s = run(eng, base_cfg(sources=[share], archive_mode="recycle", junk_mode="recycle"))
        t = tree(share)
        check("recycle: archive and junk moved into the share's .Recycle.Bin with their folder layout",
              "Show/S01/ep.mkv" in t and ".Recycle.Bin/Show/S01/ep.zip" in t and ".Recycle.Bin/Show/S01/file.diz" in t
              and "Show/S01/ep.zip" not in t, (t, s["failures"], s["message"]))
        check("recycle: existing Recycle Bin is never scanned", ".Recycle.Bin/old/deleted.zip" in t and "x.txt" not in " ".join(t), t)
        check("recycle: counted in stats", eng.stats.snapshot()["totals"]["recycled"] > 0)
        shutil.rmtree(work)

        # 2) a folder inside a share is mapped and the share root is not reachable: clear error, nothing touched
        work, eng = fresh()
        sub = os.path.join(work, "dl")
        os.makedirs(sub)
        make_zip(f"{sub}/a.zip", {"a.txt": "a"})
        fake_mounts(work, [(sub, "/data/downloads", "fuse.shfs", "shfs")])
        s = run(eng, base_cfg(sources=[sub], archive_mode="recycle"))
        check("recycle: unreachable share root stops the run with a reason",
              s["result"] == "error" and "not reachable" in s["message"] and "a.zip" in tree(sub), s["message"])
        shutil.rmtree(work)

        # 3) same, but all shares (/mnt/user) are mapped too: bin at <all>/data/.Recycle.Bin/downloads/...
        work, eng = fresh()
        sub, allsh = os.path.join(work, "dl"), os.path.join(work, "all")
        os.makedirs(sub)
        os.makedirs(f"{allsh}/data")
        make_zip(f"{sub}/a.zip", {"a.txt": "a"})
        fake_mounts(work, [(sub, "/data/downloads", "fuse.shfs", "shfs"), (allsh, "/", "fuse.shfs", "shfs")])
        s = run(eng, base_cfg(sources=[sub], archive_mode="recycle"))
        check("recycle: share root found through the all-shares mapping",
              os.path.isfile(f"{allsh}/data/.Recycle.Bin/downloads/a.zip") and "a.txt" in tree(sub), (tree(allsh), s["message"]))
        shutil.rmtree(work)
    finally:
        mounts.MOUNTINFO = old


def test_oneoff():
    work, eng = fresh()
    top = os.path.join(work, "pick")
    os.makedirs(f"{top}/older")
    make_zip(f"{top}/older/untouched.zip", {"u.txt": "u"})                 # existing sub-folder: skipped when not recursive
    inner = os.path.join(work, "inner.zip")
    make_zip(inner, {"deep.txt": "d"})
    make_zip(f"{top}/rel.zip", {"Rel/inner.zip": ("file", inner), "r.txt": "r"})   # creates a new folder with a nested zip
    fresh_time = time.time()
    os.utime(f"{top}/rel.zip", (fresh_time, fresh_time))                 # brand new: one-off ignores the age rule
    cfg = base_cfg(sources=[os.path.join(work, "elsewhere")], min_age_minutes=0)
    cfg["archive_mode"] = "keep"
    assert eng.start(cfg, "one-off", False, oneoff={"path": top, "recursive": False})
    eng.thread.join(60)
    s = eng.snapshot()
    t = tree(top)
    check("one-off keep: extracted, archives left in place, no endless loop",
          s["result"] == "completed" and "r.txt" in t and "rel.zip" in t and "Rel/deep.txt" in t and "Rel/inner.zip" in t, (t, s))
    check("one-off this folder only: existing sub-folders skipped", "older/untouched.zip" in t and "older/u.txt" not in t, t)
    shutil.rmtree(work)



def test_keep_mode():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(f"{src}/k")
    make_zip(f"{src}/k/kept.zip", {"kf.txt": "kept"})
    cfg = base_cfg(sources=[src], archive_mode="keep", junk_mode="keep")
    s = run(eng, cfg)
    t = tree(src)
    check("keep: file extracted and archive left in place", "k/kf.txt" in t and "k/kept.zip" in t, t)
    extracts = [e["msg"] for e in eng.log_since(0, 1000) if "Extracting" in e["msg"] and "kept.zip" in e["msg"]]
    check("keep: archive extracted once within the run", len(extracts) == 1, extracts)
    kf = os.path.join(work, "cfg", "kept-archives.json")
    check("keep: archive recorded in the kept file", f"{src}/k/kept.zip" in json.load(open(kf)), os.path.exists(kf))
    n = len(eng.log_since(0, 2000))
    s2 = run(eng, cfg, trigger="schedule")
    new_msgs = [e["msg"] for e in eng.log_since(0, 2000)[n:]]
    check("keep: second run does not extract again", not any("Extracting" in m for m in new_msgs), new_msgs)
    check("keep: second run is quiet and counts nothing",
          not any("kept.zip" in m for m in new_msgs) and s2["skipped"] == 0 and s2["found"] == 0
          and s2["remaining"] == 0 and s2["extracted"] == 0, (s2, new_msgs))
    s2 = run(eng, cfg, trigger="manual")
    check("keep: Run now does not extract it again either", s2["extracted"] == 0 and s2["found"] == 0, s2)
    check("keep: archive and files still present", "k/kept.zip" in tree(src) and "k/kf.txt" in tree(src), tree(src))
    os.remove(f"{src}/k/kf.txt")
    make_zip(f"{src}/k/kept.zip", {"kf.txt": "changed", "more.txt": "m"})   # archive changed
    s2 = run(eng, cfg, trigger="schedule")
    check("keep: a changed archive is extracted again", s2["extracted"] == 1 and "k/more.txt" in tree(src), (s2, tree(src)))
    s2 = run(eng, cfg, trigger="schedule")
    check("keep: and then left alone again", s2["extracted"] == 0 and s2["found"] == 0, s2)
    # one-off job processes a kept archive anyway
    os.remove(f"{src}/k/more.txt")
    assert eng.start(cfg, "one-off", False, oneoff={"path": src, "recursive": True})
    eng.thread.join(60)
    s2 = eng.snapshot()
    check("keep: one-off re-extracts a kept archive", s2["extracted"] == 1 and "k/more.txt" in tree(src), (s2, tree(src)))
    check("keep: still recorded after the one-off", f"{src}/k/kept.zip" in json.load(open(kf)))
    # switching to delete processes it and removes it from the kept file
    cfg_del = base_cfg(sources=[src], archive_mode="delete", junk_mode="delete")
    s2 = run(eng, cfg_del, trigger="schedule")
    check("keep -> delete: archive processed and removed",
          s2["extracted"] == 1 and "k/kept.zip" not in tree(src) and json.load(open(kf)) == {}, (s2, tree(src)))
    # legacy plain-signature entries are understood (as "keep")
    make_zip(f"{src}/k/old.zip", {"o.txt": "o"})
    json.dump({f"{src}/k/old.zip": eng._signature(f"{src}/k/old.zip")}, open(kf, "w"))
    s2 = run(eng, cfg, trigger="schedule")
    check("keep: legacy entry format still skips", s2["extracted"] == 0 and "k/o.txt" not in tree(src), (s2, tree(src)))
    shutil.rmtree(work)


def test_cancel_one():
    """Cancel one archive while another carries on: nothing changed, remembered for scheduled scans."""
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    make_zip(f"{src}/slow.zip", {"big.bin": "s"})
    make_zip(f"{src}/fast.zip", {"f.txt": "f"})
    real = eng.tools["unzip"]
    fake = os.path.join(work, "unzip-slow")
    open(fake, "w").write("#!/bin/sh\ncase \"$*\" in *slow.zip*) echo partial > \"$5/partial.txt\"; sleep 30;; esac\nexec %s \"$@\"\n" % real)
    os.chmod(fake, 0o755)
    eng.tools = dict(eng.tools, unzip=fake, **{"7z": None})
    assert eng.start(base_cfg(sources=[src], threads=2), "manual")
    for _ in range(100):
        sl = [x for x in eng.snapshot()["slots"] if x["path"] and x["path"].endswith("slow.zip")]
        if sl and "fast.zip" not in tree(src):
            break
        time.sleep(0.1)
    t0 = time.time()
    ok, why = eng.cancel_job(f"{src}/slow.zip")
    eng.thread.join(30)
    s, t = eng.snapshot(), tree(src)
    check("cancel: accepted and quick", ok and time.time() - t0 < 10, (ok, why))
    check("cancel: archive untouched, partial output thrown away, other archive done",
          "slow.zip" in t and "partial.txt" not in " ".join(t) and "f.txt" in t and not stage_dirs(src), t)
    check("cancel: counted as cancelled, not failed", s["cancelled"] == 1 and s["failed"] == 0 and s["extracted"] == 1, s)
    check("cancel: too late / unknown archive refused", eng.cancel_job(f"{src}/nope.zip")[0] is False)
    eng.tools = dict(eng.tools, unzip=real)
    s = run(eng, base_cfg(sources=[src]), trigger="schedule")
    check("cancel: scheduled scans skip it while unchanged", "slow.zip" in tree(src) and s["extracted"] == 0, s)
    s = run(eng, base_cfg(sources=[src]), trigger="manual")
    check("cancel: Run now tries it again", "slow.zip" not in tree(src) and "big.bin" in tree(src), tree(src))
    shutil.rmtree(work)



def test_future_dated():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    z = f"{src}/ahead.zip"
    make_zip(z, {"a.txt": "a"})
    ahead = time.time() + 365 * 86400
    os.utime(z, (ahead, ahead))                              # dated a year ahead (clock / timezone mix-up)
    s = run(eng, base_cfg(sources=[src], min_age_minutes=5))
    check("future date: a brand-new arrival is still skipped (change time is now)", "ahead.zip" in tree(src) and s["deferred"] == 1, tree(src))
    real = time.time
    time.time = lambda: real() + 600                         # the archive "arrived" 10 minutes ago
    try:
        s = run(eng, base_cfg(sources=[src], min_age_minutes=5))
    finally:
        time.time = real
    check("future date: extracted once it arrived long enough ago", "a.txt" in tree(src) and "ahead.zip" not in tree(src), tree(src))
    msgs = " ".join(e["msg"] for e in eng.log_since(0, 1000))
    check("future date: warned once", msgs.count("dated in the future") == 1, msgs[-300:])
    shutil.rmtree(work)


def test_overwrite_off_keeps_archive():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    make_zip(f"{src}/x.zip", {"same.txt": "new", "other.txt": "o", "d/deep.txt": "d"})
    open(f"{src}/same.txt", "w").write("mine")
    s = run(eng, base_cfg(sources=[src], overwrite=False))
    check("overwrite off: existing file untouched, rest extracted",
          open(f"{src}/same.txt").read() == "mine" and "other.txt" in tree(src) and "d/deep.txt" in tree(src), tree(src))
    check("overwrite off: archive kept, counted as extracted not failed",
          "x.zip" in tree(src) and s["extracted"] == 1 and s["failed"] == 0 and s["remaining"] == 0, s)
    msgs = " ".join(e["msg"] for e in eng.log_since(0, 1000))
    check("overwrite off: warning names the count", "Kept 1 file(s)" in msgs, msgs[-300:])
    kf = os.path.join(work, "cfg", "kept-archives.json")
    check("overwrite off: recorded as kept (exists)", json.load(open(kf))[f"{src}/x.zip"]["why"] == "exists", open(kf).read())
    n = len(eng.log_since(0, 2000))
    s = run(eng, base_cfg(sources=[src], overwrite=False), trigger="schedule")
    new_msgs = [e["msg"] for e in eng.log_since(0, 2000)[n:]]
    check("overwrite off: next scan does not extract it again, counts nothing",
          not any("Extracting" in m or "x.zip" in m for m in new_msgs) and s["extracted"] == 0
          and s["skipped"] == 0 and s["found"] == 0 and s["remaining"] == 0, (s, new_msgs))
    s = run(eng, base_cfg(sources=[src], overwrite=True), trigger="schedule")
    check("overwrite on: archive is processed once and removed",
          s["extracted"] == 1 and "x.zip" not in tree(src) and json.load(open(kf)) == {}, (s, tree(src)))
    open(f"{src}/same.txt", "w").write("mine")
    make_zip(f"{src}/y.zip", {"same.txt": "new", "z.txt": "z"})
    run(eng, base_cfg(sources=[src], overwrite=False))
    for f in ("same.txt", "z.txt"):
        os.remove(f"{src}/{f}")                                # nothing clashes any more
    changed = time.time() + 5
    os.utime(f"{src}/y.zip", (changed, changed))               # the archive changed: tried again
    run(eng, base_cfg(sources=[src], overwrite=False), trigger="schedule")
    check("overwrite off: archive removed once nothing clashes", "y.zip" not in tree(src) and "z.txt" in tree(src), tree(src))
    shutil.rmtree(work)


def test_failed_remembered():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    make_zip(f"{src}/good.zip", {"g.txt": "g"})
    with open(f"{src}/bad.zip", "w") as f:
        f.write("not a zip")
    ff = os.path.join(work, "cfg", "failed-archives.json")
    s = run(eng, base_cfg(sources=[src]), trigger="schedule")
    check("failed: first scheduled run fails it and remembers it",
          s["failed"] == 1 and s["skipped"] == 0 and f"{src}/bad.zip" in json.load(open(ff)), (s, os.path.exists(ff)))
    s = run(eng, base_cfg(sources=[src]), trigger="schedule")
    check("failed: next scheduled run skips it, counted as done", s["skipped"] == 1 and s["failed"] == 0
          and s["remaining"] == 0 and s["found"] == 1 and s["done"] == 1, s)
    check("failed: skip is in the history", eng.history[-1]["skipped"] == 1, eng.history[-1])
    s = run(eng, base_cfg(sources=[src]), trigger="manual")
    check("failed: manual run retries", s["failed"] == 1 and s["skipped"] == 0, s)
    s = run(eng, base_cfg(sources=[src]), trigger="schedule")
    check("failed: skipped again after the manual retry", s["skipped"] == 1, s)
    make_zip(f"{src}/bad.zip", {"b.txt": "b"})              # fixed: signature changed
    s = run(eng, base_cfg(sources=[src]), trigger="schedule")
    check("failed: changed archive is retried, extracted, forgotten",
          s["extracted"] == 1 and "b.txt" in tree(src) and json.load(open(ff)) == {}, (s, tree(src)))
    # entries for vanished archives are dropped on save, cap applies
    d = {f"/nope/{i}.zip": [1, 1] for i in range(6000)}
    d[f"{src}/keep.zip"] = [1, 1]
    open(f"{src}/keep.zip", "w").close()
    eng._save_remembered(ff, d)
    check("failed: vanished paths dropped when saving", json.load(open(ff)) == {f"{src}/keep.zip": [1, 1]})
    shutil.rmtree(work)


def test_cancelled_skip_counts_done():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    make_zip(f"{src}/c.zip", {"c.txt": "c"})
    open(os.path.join(work, "cfg", "cancelled-archives.json"), "w").write(json.dumps({f"{src}/c.zip": eng._signature(f"{src}/c.zip")}))
    s = run(eng, base_cfg(sources=[src]), trigger="schedule")
    check("cancelled skip: counted as skipped and done, remaining 0",
          s["skipped"] == 1 and s["remaining"] == 0 and s["cancelled"] == 0 and "c.zip" in tree(src), s)
    shutil.rmtree(work)


def test_unexpected_error():
    work, eng = fresh()
    src = os.path.join(work, "src")
    os.makedirs(src)
    make_zip(f"{src}/boom.zip", {"x.txt": "x"})
    make_zip(f"{src}/fine.zip", {"f.txt": "f"})
    orig = eng._extract

    def bad(path, *a, **k):
        if path.endswith("boom.zip"):
            raise ValueError("kaboom")
        return orig(path, *a, **k)
    eng._extract = bad
    s = run(eng, base_cfg(sources=[src], threads=1))
    msgs = " ".join(e["msg"] for e in eng.log_since(0, 1000))
    check("unexpected error: only that archive fails, run completes",
          s["result"] == "completed" and s["failed"] == 1 and s["extracted"] == 1 and "f.txt" in tree(src)
          and "boom.zip" in tree(src) and "unexpected error: kaboom" in msgs, (s, msgs[-300:]))
    check("unexpected error: slot released", all(x["path"] is None for x in eng.snapshot()["slots"]) or not eng.snapshot()["slots"])
    shutil.rmtree(work)


def test_dry_run_leaves_stages():
    work, eng = fresh()
    src = os.path.join(work, "src")
    from engine import STAGE_PREFIX
    stale = f"{src}/{STAGE_PREFIX}old"
    os.makedirs(stale)
    run(eng, base_cfg(sources=[src]), dry=True)
    check("dry run: stale work folder not removed", os.path.isdir(stale))
    run(eng, base_cfg(sources=[src]))
    check("real run: stale work folder removed", not os.path.isdir(stale))
    shutil.rmtree(work)


def test_oneoff_root_refused():
    work, eng = fresh()
    assert eng.start(base_cfg(sources=[work]), "one-off", False, oneoff={"path": "/", "recursive": True})
    eng.thread.join(30)
    s = eng.snapshot()
    check("one-off: / refused with a clear message", s["result"] == "error" and "root folder" in s["message"], s)
    shutil.rmtree(work)


if __name__ == "__main__":
    print("tools:", {k: bool(v) for k, v in Engine(tempfile.mkdtemp(), os.devnull).tools.items()})
    for fn in (test_settings, test_nested_delete_and_perms, test_move_mode, test_dry_run,
               test_min_age, test_excluded_and_stop, test_parallel_same_names, test_parts_spread_over_zips,
               test_broken_archive, test_volumes, test_nested_remembered_between_runs, test_stats,
               test_recycle_bin, test_oneoff, test_cancel_one, test_future_dated,
               test_overwrite_off_keeps_archive, test_keep_mode, test_failed_remembered, test_cancelled_skip_counts_done,
               test_unexpected_error, test_dry_run_leaves_stages, test_oneoff_root_refused):
        fn()
    print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
