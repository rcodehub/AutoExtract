"""Folder per archive: where the extracted files go (next to the archive, or in a folder named after it).
Uses real archives built at test time (zip, tar.gz, RAR5 volume sets). Needs unzip, unrar, 7z and rar.
Run:  python3 -I tests/test_folders.py   (as root and as a normal user)"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
CFG_DIR = tempfile.mkdtemp(prefix="ea-folders-cfg-")
os.environ["DATA_DIR"] = CFG_DIR
from engine import Engine, archive_base_name, names_similar  # noqa: E402
import settings  # noqa: E402

FAILS = []
MISSING = [t for t in ("unzip", "unrar", "7z", "rar", "zip") if not shutil.which(t)]
if MISSING:
    print(f"ERROR: required tool(s) not installed: {', '.join(MISSING)}", file=sys.stderr)
    sys.exit(2)


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def cfg_for(**kw):
    cfg = dict(settings.DEFAULTS, archive_mode="delete", junk_mode="delete", min_age_minutes=0, threads=3)
    cfg.update(kw)
    clean, errors = settings.validate(cfg)
    assert not errors, errors
    return clean


def fresh():
    work = tempfile.mkdtemp(prefix="ea-folders-")
    src = os.path.join(work, "src")
    os.makedirs(src)
    return work, src, Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))


def run(eng, cfg, dry=False, trigger="test", oneoff=None):
    assert eng.start(cfg, trigger, dry, oneoff=oneoff)
    eng.thread.join(120)
    assert not eng.running
    return eng.snapshot()


def tree(root):
    out = []
    for d, _, fs in os.walk(root):
        for f in fs:
            out.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(out)


def mkzip(path, files):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            if isinstance(data, tuple):
                z.write(data[1], name)
            else:
                z.writestr(name, data)


def logs(eng):
    return [e["msg"] for e in eng.log_since(0, 2000)]


def mkrar_set(folder, name, size=60_000, volume="20k"):
    """name.part1.rar, name.part2.rar ... (RAR5) holding one random file."""
    tmp = tempfile.mkdtemp(prefix="ea-rar-")
    try:
        with open(os.path.join(tmp, "payload.bin"), "wb") as f:
            f.write(os.urandom(size))
        os.makedirs(folder, exist_ok=True)
        r = subprocess.run(["rar", "a", "-idq", "-y", "-ep", "-ma5", f"-v{volume}", os.path.join(folder, name + ".rar"),
                            "payload.bin"], cwd=tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
        assert r.returncode == 0, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_names():
    cases = {
        "name.part1.rar": "name", "name.part01.rar": "name", "Name.PART001.RAR": "Name", "name.rar": "name",
        "name.r00": "name", "name.zip": "name", "name.z01": "name", "name.zip.001": "name", "name.7z.001": "name",
        "name.7z": "name", "name.tar.gz": "name", "name.tgz": "name", "name.tar.bz2": "name",
        "name.tar.xz": "name", "name.tar": "name", "My.Movie.2016.1080p-GRP.part2.rar": "My.Movie.2016.1080p-GRP",
        "name. .zip": "name", "a.b.c.zip": "a.b.c", ".zip": ".zip_files", "...rar": "...rar_files",
        "/x/y/name.tar.gz": "name",
    }
    for src, want in cases.items():
        got = archive_base_name(src)
        check(f"base name: {src} -> {want}", got == want, got)
    yes = [("Movie.2016.1080p-GRP", "Movie 2016"), ("movie.2016", "MOVIE_2016"), ("a-b_c d", "a.b.c.d"),
           ("Show", "Show.S01E01"), ("abcdefghij", "abcdefghik")]
    no = [("photos", "files"), ("abc", "abcdef"), ("", "x"), ("x", ""), ("movie", "moviemaker"),
          ("holiday", "work")]
    for a, b in yes:
        check(f"similar: {a!r} ~ {b!r}", names_similar(a, b) and names_similar(b, a))
    for a, b in no:
        check(f"not similar: {a!r} !~ {b!r}", not names_similar(a, b) and not names_similar(b, a))


def test_settings():
    check("setting: default is smart", settings.DEFAULTS["folder_per_archive"] == "smart")
    s, e = settings.validate({"folder_per_archive": "Always"})
    check("setting: accepted, case-insensitive", not e and s["folder_per_archive"] == "always", (s, e))
    s, e = settings.validate({"folder_per_archive": "sometimes"})
    check("setting: invalid value refused", e and "Folder per archive" in e[0], e)
    s, problems = settings._lenient({"folder_per_archive": "bogus"})
    check("setting: lenient load resets only this value", s["folder_per_archive"] == "smart" and problems, (s, problems))
    s, problems = settings._lenient({"threads": 7})                      # an install from before the setting existed
    check("setting: missing key (old install) is smart", s["folder_per_archive"] == "smart" and not problems, (s, problems))


def test_smart_basic():
    work, src, eng = fresh()
    mkzip(f"{src}/loose.zip", {"a.txt": "a", "b.txt": "b"})
    mkzip(f"{src}/single.zip", {"only.txt": "o"})
    mkzip(f"{src}/matching.zip", {"matching/m.txt": "m", "matching/sub/n.txt": "n"})
    mkzip(f"{src}/other.zip", {"Different Name/d.txt": "d"})
    mkzip(f"{src}/mix.zip", {"file.txt": "f", "folder/g.txt": "g"})
    mkzip(f"{src}/two.zip", {"one/1.txt": "1", "two/2.txt": "2"})
    s = run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check("smart: loose files -> folder named after the archive", {"loose/a.txt", "loose/b.txt"} <= set(t), t)
    check("smart: single file -> wrapped", "single/only.txt" in t, t)
    check("smart: one matching folder -> as is (no name/name)", {"matching/m.txt", "matching/sub/n.txt"} <= set(t), t)
    check("smart: one non-matching folder -> as is", "Different Name/d.txt" in t and "other/Different Name/d.txt" not in t, t)
    check("smart: mix of file and folder -> wrapped", {"mix/file.txt", "mix/folder/g.txt"} <= set(t), t)
    check("smart: several folders -> wrapped", {"two/one/1.txt", "two/two/2.txt"} <= set(t), t)
    check("smart: nothing loose in the source folder", not [x for x in t if "/" not in x], t)
    check("smart: counts", s["extracted"] == 6 and s["failed"] == 0 and s["remaining"] == 0, s)
    m = logs(eng)
    check("smart: wrapping is logged in plain English",
          any("Extracted into a new folder loose/ (the archive has no single top folder)" in x for x in m), m)
    check("smart: Done line kept", any(x.startswith("Done: loose.zip - 2 file(s)") for x in m), m)
    check("smart: no work folders left", not [x for x in os.listdir(src) if x.startswith(".autoextract-")])
    mode = stat.S_IMODE(os.stat(f"{src}/loose").st_mode)
    check("smart: created folder gets the folder permissions", mode == 0o777, oct(mode))
    shutil.rmtree(work)


def test_ten_zips():
    work, src, eng = fresh()
    for i in range(10):
        mkzip(f"{src}/set{i:02d}.zip", {"cover.jpg": f"c{i}", "readme.txt": f"r{i}", "01.mp3": f"m{i}"})
    s = run(eng, cfg_for(sources=[src], threads=4))
    t = tree(src)
    ok = all(open(f"{src}/set{i:02d}/{n}").read() == v + str(i) for i in range(10)
             for n, v in (("cover.jpg", "c"), ("readme.txt", "r"), ("01.mp3", "m")))
    check("ten zips: ten folders, nothing mixed", len(t) == 30 and ok and s["extracted"] == 10, (len(t), s))
    shutil.rmtree(work)


def test_release_folder():
    work, src, eng = fresh()
    d = f"{src}/Movie.2016"
    mkrar_set(d, "movie.2016")
    open(f"{d}/movie.2016.nfo", "w").write("nfo")
    open(f"{d}/cover.jpg", "w").write("jpg")
    d2 = f"{src}/Movie.2017"                       # same, but a second unrelated archive lives here
    mkrar_set(d2, "movie.2017")
    mkzip(f"{d2}/extras.zip", {"x.txt": "x"})
    s = run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check("release folder: parts extracted straight into the folder", "Movie.2016/payload.bin" in t, t)
    check("release folder: .nfo/.jpg untouched, all volumes removed",
          "Movie.2016/movie.2016.nfo" in t and "Movie.2016/cover.jpg" in t
          and not [x for x in t if x.startswith("Movie.2016/movie.2016.part")], t)
    check("release folder with another archive set: wrapped",
          "Movie.2017/movie.2017/payload.bin" in t and "Movie.2017/extras/x.txt" in t and "Movie.2017/payload.bin" not in t, t)
    check("release folder: all extracted", s["extracted"] == 3 and s["failed"] == 0, s)
    shutil.rmtree(work)


def test_release_inconsistency():
    """Archives of one folder all see the same neighbours, even after the first one was extracted and removed."""
    work, src, eng = fresh()
    d = f"{src}/Show"
    mkzip(f"{d}/show.zip", {"a.txt": "a"})
    mkzip(f"{d}/show.1.zip", {"b.txt": "b"})
    mkzip(f"{d}/zzz.zip", {"c.txt": "c"})
    run(eng, cfg_for(sources=[src], threads=1))
    t = tree(src)
    check("release folder: with several sets, every archive is wrapped",
          {"Show/show/a.txt", "Show/show.1/b.txt", "Show/zzz/c.txt"} <= set(t), t)
    shutil.rmtree(work)


def test_root_is_never_a_release_folder():
    work, src, eng = fresh()                      # src is called "src": an archive named src.zip sits in it
    mkzip(f"{src}/src.zip", {"a.txt": "a", "b.txt": "b"})
    run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check("source root named like the archive: wrapped", {"src/a.txt", "src/b.txt"} <= set(t), t)
    shutil.rmtree(work)
    work, src, eng = fresh()                      # a second source root, and a sub-folder with the same name
    other = os.path.join(work, "Music")
    os.makedirs(other)
    mkzip(f"{other}/music.zip", {"a.txt": "a", "b.txt": "b"})
    run(eng, cfg_for(sources=[src, other]))
    check("second source root: wrapped", {"music/a.txt", "music/b.txt"} <= set(tree(other)), tree(other))
    shutil.rmtree(work)


def test_existing_folder_and_files():
    work, src, eng = fresh()
    os.makedirs(f"{src}/x")
    open(f"{src}/x/same.txt", "w").write("mine")
    mkzip(f"{src}/x.zip", {"same.txt": "new", "other.txt": "o"})
    s = run(eng, cfg_for(sources=[src], overwrite=False))
    t = tree(src)
    check("existing folder: merged into, existing file kept, new file added",
          open(f"{src}/x/same.txt").read() == "mine" and "x/other.txt" in t, t)
    check("existing folder, overwrite off: archive kept and counted extracted",
          "x.zip" in t and s["extracted"] == 1 and s["failed"] == 0, (t, s))
    check("existing folder, overwrite off: warning names the count",
          any("Kept 1 file(s)" in m for m in logs(eng)), logs(eng)[-4:])
    import json
    kept = json.load(open(os.path.join(work, "cfg", "kept-archives.json")))
    check("existing folder, overwrite off: remembered as kept (exists)", kept.get(f"{src}/x.zip", {}).get("why") == "exists", kept)
    n = len(logs(eng))
    s = run(eng, cfg_for(sources=[src], overwrite=False), trigger="schedule")
    check("existing folder, overwrite off: next scan leaves it alone",
          s["extracted"] == 0 and not any("Extracting" in m for m in logs(eng)[n:]), (s, logs(eng)[n:]))
    s = run(eng, cfg_for(sources=[src], overwrite=True), trigger="schedule")
    check("overwrite on: extracted into the same folder and archive removed",
          open(f"{src}/x/same.txt").read() == "new" and "x.zip" not in tree(src), tree(src))
    check("existing folder: logged as existing", any("existing folder x/" in m for m in logs(eng)), logs(eng))
    shutil.rmtree(work)

    work, src, eng = fresh()
    open(f"{src}/x", "w").write("i am a file")
    os.symlink("nowhere", f"{src}/y")
    mkzip(f"{src}/x.zip", {"a.txt": "a", "b.txt": "b"})
    mkzip(f"{src}/y.zip", {"c.txt": "c", "d.txt": "d"})
    os.makedirs(f"{src}/x (1)")
    open(f"{src}/x (2)", "w").write("file too")
    run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check("file named like the target: the next free folder name is used", {"x (1)/a.txt", "x (1)/b.txt"} <= set(t)
          and open(f"{src}/x").read() == "i am a file", t)
    check("symlink named like the target: x (1) style name", {"y (1)/c.txt", "y (1)/d.txt"} <= set(t) and os.path.islink(f"{src}/y"), t)
    shutil.rmtree(work)
    work, src, eng = fresh()
    open(f"{src}/x", "w").write("file")
    open(f"{src}/x (1)", "w").write("file")
    mkzip(f"{src}/x.zip", {"a.txt": "a", "b.txt": "b"})
    run(eng, cfg_for(sources=[src]))
    check("file named like the target: skips x (1) when that is a file too", "x (2)/a.txt" in tree(src), tree(src))
    shutil.rmtree(work)


def test_nested():
    work, src, eng = fresh()
    inner = os.path.join(work, "inner.zip")
    mkzip(inner, {"b.txt": "b", "c.txt": "c"})
    outer = f"{src}/outer.zip"
    mkzip(outer, {"a.txt": "a", "z.txt": "z", "inner.zip": ("file", inner)})
    old = time.time() - 3600
    os.utime(outer, (old, old))                  # the outer archive is old enough, the inner one is brand new
    s = run(eng, cfg_for(sources=[src], min_age_minutes=10))
    t = tree(src)
    check("nested: outer wrapped, inner (new date) extracted and wrapped too",
          {"outer/a.txt", "outer/z.txt", "outer/inner/b.txt", "outer/inner/c.txt"} <= set(t) and not any(x.endswith(".zip") for x in t), t)
    check("nested: the min-age rule did not hold the inner archive back", s["deferred"] == 0 and s["extracted"] == 2, s)
    shutil.rmtree(work)

    work, src, eng = fresh()                     # outer holds one folder with an archive in it
    inner = os.path.join(work, "inner.zip")
    mkzip(inner, {"b.txt": "b"})
    mkzip(f"{src}/pack.zip", {"Pack/x.txt": "x", "Pack/inner.zip": ("file", inner)})
    run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check("nested: single-folder outer as is; inner (one file) wrapped in its folder",
          {"Pack/x.txt", "Pack/inner/b.txt"} <= set(t), t)
    shutil.rmtree(work)

    work, src, eng = fresh()                     # nested archive in an already existing (kept) place, run twice
    inner = os.path.join(work, "inner.zip")
    mkzip(inner, {"b.txt": "b", "c.txt": "c"})
    outer = f"{src}/outer.zip"
    mkzip(outer, {"a.txt": "a", "inner.zip": ("file", inner), "bad.zip": "not a zip"})
    os.utime(outer, (old, old))
    run(eng, cfg_for(sources=[src], min_age_minutes=10))
    mkzip(f"{src}/outer/bad.zip", {"ok.txt": "ok"})            # fixed afterwards, still a recent date
    eng2 = Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))
    s = run(eng2, cfg_for(sources=[src], min_age_minutes=10))
    t = tree(src)
    check("nested: remembered between runs also in the new sub-folder", "outer/bad/ok.txt" in t and s["failed"] == 0, (t, s))
    shutil.rmtree(work)


def test_modes():
    work, src, eng = fresh()
    mkzip(f"{src}/loose.zip", {"a.txt": "a", "b.txt": "b"})
    mkzip(f"{src}/same.zip", {"same/x.txt": "x"})
    mkzip(f"{src}/Same Name.zip", {"same.name/y.txt": "y"})
    mkzip(f"{src}/diff.zip", {"other/z.txt": "z"})
    run(eng, cfg_for(sources=[src], folder_per_archive="always"))
    t = tree(src)
    check("always: loose files wrapped", {"loose/a.txt", "loose/b.txt"} <= set(t), t)
    check("always: one folder named like the archive -> no name/name", "same/x.txt" in t and "same/same/x.txt" not in t, t)
    check("always: similar (not equal) name -> no doubling either", "same.name/y.txt" in t and not any("Same Name/" in x for x in t), t)
    check("always: one folder with another name -> still wrapped", "diff/other/z.txt" in t, t)
    shutil.rmtree(work)

    work, src, eng = fresh()
    mkzip(f"{src}/loose.zip", {"a.txt": "a", "b.txt": "b"})
    mkzip(f"{src}/single.zip", {"s.txt": "s"})
    s = run(eng, cfg_for(sources=[src], folder_per_archive="never"))
    check("never: old behaviour, everything next to the archive", tree(src) == ["a.txt", "b.txt", "s.txt"], tree(src))
    check("never: nothing logged about folders", not any("new folder" in m for m in logs(eng)))
    shutil.rmtree(work)

    work, src, eng = fresh()                      # always also puts a release-folder archive in a sub-folder
    mkzip(f"{src}/Movie/movie.zip", {"a.txt": "a", "b.txt": "b"})
    run(eng, cfg_for(sources=[src], folder_per_archive="always"))
    check("always: release folder is wrapped as well (it is the 'always' mode)",
          {"Movie/movie/a.txt", "Movie/movie/b.txt"} <= set(tree(src)), tree(src))
    shutil.rmtree(work)


def test_other_formats():
    work, src, eng = fresh()
    import tarfile
    tmp = os.path.join(work, "t")
    os.makedirs(tmp)
    for n in ("one", "two"):
        open(f"{tmp}/{n}.txt", "w").write(n)
    with tarfile.open(f"{src}/bundle.tar.gz", "w:gz") as t:
        t.add(f"{tmp}/one.txt", "one.txt")
        t.add(f"{tmp}/two.txt", "two.txt")
    subprocess.run(["7z", "a", "-bd", f"{src}/seven.7z", f"{tmp}/one.txt", f"{tmp}/two.txt"], stdout=subprocess.DEVNULL, check=True)
    subprocess.run(["zip", "-q", "-s", "100k", f"{src}/split.zip", "one.txt"], cwd=tmp, check=True)
    shutil.copy(f"{src}/split.zip", f"{work}/keep.zip")
    d = f"{src}/rar"
    mkrar_set(d, "multi")
    run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check(".tar.gz: folder is named without .tar.gz", {"bundle/one.txt", "bundle/two.txt"} <= set(t), t)
    check(".7z: wrapped", {"seven/one.txt", "seven/two.txt"} <= set(t), t)
    check("multi-part RAR5 set in an unrelated folder: single file -> wrapped under the set name",
          "rar/multi/payload.bin" in t and not [x for x in t if ".part" in x], t)
    shutil.rmtree(work)


def test_oneoff():
    work, src, eng = fresh()
    pick = os.path.join(work, "pick")
    inner = os.path.join(work, "inner.zip")
    mkzip(inner, {"deep.txt": "d", "deep2.txt": "d"})
    mkzip(f"{pick}/rel.zip", {"r.txt": "r", "r2.txt": "r", "inner.zip": ("file", inner)})
    mkzip(f"{pick}/older/untouched.zip", {"u.txt": "u"})
    cfg = cfg_for(sources=[os.path.join(work, "elsewhere")])
    cfg["archive_mode"] = "keep"
    s = run(eng, cfg, trigger="one-off", oneoff={"path": pick, "recursive": False})
    t = tree(pick)
    check("one-off: archive wrapped, nested one wrapped, existing sub-folders still skipped",
          {"rel/r.txt", "rel/inner/deep.txt", "rel.zip", "rel/inner.zip", "older/untouched.zip"} <= set(t)
          and "older/u.txt" not in t and s["result"] == "completed", (t, s))
    shutil.rmtree(work)
    work, src, eng = fresh()                      # the picked folder itself is never a release folder
    pick = os.path.join(work, "pick")
    mkzip(f"{pick}/pick.zip", {"a.txt": "a", "b.txt": "b"})
    run(eng, cfg_for(sources=[src]), trigger="one-off", oneoff={"path": pick, "recursive": True})
    check("one-off: the picked folder is not a release folder", {"pick/a.txt", "pick/b.txt"} <= set(tree(pick)), tree(pick))
    shutil.rmtree(work)


def test_dry_run():
    work, src, eng = fresh()
    mkzip(f"{src}/loose.zip", {"a.txt": "a", "b.txt": "b"})
    mkzip(f"{src}/Rel/rel.zip", {"a.txt": "a", "b.txt": "b"})
    s = run(eng, cfg_for(sources=[src]), dry=True)
    m = [x for x in logs(eng) if "[dry run]" in x]
    check("dry run: nothing changed", tree(src) == ["Rel/rel.zip", "loose.zip"], tree(src))
    check("dry run: says it extracts into a folder unless a single folder",
          any("into a folder named after the archive (loose/) unless it holds a single folder" in x for x in m), m)
    check("dry run: release folder detected", any("into this folder (it is the archive's own folder)" in x for x in m), m)
    shutil.rmtree(work)


def test_empty_and_root():
    work, src, eng = fresh()
    mkzip(f"{src}/empty.zip", {})
    run(eng, cfg_for(sources=[src]))
    check("empty archive: no empty folder is made", not os.path.exists(f"{src}/empty"), os.listdir(src))
    shutil.rmtree(work)


def main():
    for fn in (test_names, test_settings, test_smart_basic, test_ten_zips, test_release_folder,
               test_release_inconsistency, test_root_is_never_a_release_folder, test_existing_folder_and_files,
               test_nested, test_modes, test_other_formats, test_oneoff, test_dry_run, test_empty_and_root):
        fn()
    shutil.rmtree(CFG_DIR, ignore_errors=True)
    print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
