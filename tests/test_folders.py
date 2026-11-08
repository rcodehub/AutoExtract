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


def mk_other(kind, out, files):
    """A 7z or RAR archive holding the given loose files (name -> text)."""
    tmp = tempfile.mkdtemp(prefix="ea-mk-")
    try:
        for n, v in files.items():
            open(os.path.join(tmp, n), "w").write(v)
        cmd = (["7z", "a", "-bd", "-y", out] if kind == "7z" else ["rar", "a", "-idq", "-y", "-ep", "-ma5", out])
        r = subprocess.run(cmd + list(files), cwd=tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
        assert r.returncode == 0, r.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_same_base_name():
    work, src, eng = fresh()
    mkzip(f"{src}/name.zip", {"readme.txt": "FROM ZIP", "z.txt": "z"})
    mk_other("7z", f"{src}/name.7z", {"readme.txt": "FROM 7Z", "s.txt": "s"})
    mk_other("rar", f"{src}/name.rar", {"readme.txt": "FROM RAR", "r.txt": "r"})
    s = run(eng, cfg_for(sources=[src], archive_mode="keep", overwrite=True))
    dirs = sorted(e for e in os.listdir(src) if os.path.isdir(f"{src}/{e}"))
    texts = sorted(open(f"{src}/{d}/readme.txt").read() for d in dirs if os.path.exists(f"{src}/{d}/readme.txt"))
    check("same base name: name/, name (1)/, name (2)/", dirs == ["name", "name (1)", "name (2)"], dirs)
    check("same base name: each folder has its own readme.txt intact",
          texts == ["FROM 7Z", "FROM RAR", "FROM ZIP"] and s["extracted"] == 3, (texts, s))
    check("same base name: each folder holds exactly its own two files",
          all(len(os.listdir(f"{src}/{d}")) == 2 for d in dirs), [os.listdir(f"{src}/{d}") for d in dirs])
    check("same base name: the log says which name was used and why",
          any("name (1)/" in m and "created in this run for another archive" in m for m in logs(eng)), logs(eng))
    shutil.rmtree(work)

    # a folder that existed before the run is still merged into (Overwrite applies); a later run reuses it
    work, src, eng = fresh()
    os.makedirs(f"{src}/name")
    open(f"{src}/name/old.txt", "w").write("old")
    mkzip(f"{src}/name.zip", {"a.txt": "a", "b.txt": "b"})
    run(eng, cfg_for(sources=[src], archive_mode="keep"))
    t = tree(src)
    check("same base name: a folder from before the run is merged into", {"name/old.txt", "name/a.txt"} <= set(t)
          and not os.path.exists(f"{src}/name (1)"), t)
    cfg = cfg_for(sources=[src], archive_mode="keep")
    run(eng, cfg, trigger="one-off", oneoff={"path": src, "recursive": False})
    check("same base name: the same archive extracted again reuses its folder", not os.path.exists(f"{src}/name (1)"),
          os.listdir(src))
    shutil.rmtree(work)


def test_release_zip_with_rar_parts():
    for mode in ("keep", "delete"):
        work, src, eng = fresh()
        d = f"{src}/Movie.2016-GRP"
        inner = os.path.join(work, "inner")
        mkrar_set(inner, "movie.2016-grp")
        open(f"{inner}/movie.2016-grp.nfo", "w").write("nfo")
        files = {n: ("file", f"{inner}/{n}") for n in os.listdir(inner)}
        mkzip(f"{d}/movie.2016-grp.zip", files)
        s = run(eng, cfg_for(sources=[src], archive_mode=mode))
        t = tree(src)
        check(f"release zip holding RAR parts ({mode}): payload directly in the release folder",
              "Movie.2016-GRP/payload.bin" in t and not any("/movie.2016-grp/" in x for x in t), t)
        check(f"release zip holding RAR parts ({mode}): both archives extracted", s["extracted"] == 2 and s["failed"] == 0, s)
        shutil.rmtree(work)
    work, src, eng = fresh()                       # a really unrelated RAR set next to the zip is still a neighbour
    d = f"{src}/Movie.2016"
    mkzip(f"{d}/extras.zip", {"x.txt": "x"})
    mkrar_set(d, "movie.2016")
    run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check("release folder: a zip extracted first still counts as a neighbour of an unrelated RAR set",
          "Movie.2016/movie.2016/payload.bin" in t and "Movie.2016/extras/x.txt" in t, t)
    shutil.rmtree(work)


def test_long_names():
    work, src, eng = fresh()
    base = "\u00e9" * 125                           # 250 bytes in UTF-8
    mkzip(f"{src}/{base}.zip", {"readme.txt": "ZIP", "z.txt": "z"})
    mk_other("7z", f"{src}/{base}.7z", {"readme.txt": "7Z", "s.txt": "s"})
    mk_other("rar", f"{src}/{base}.rar", {"readme.txt": "RAR", "r.txt": "r"})
    s = run(eng, cfg_for(sources=[src], archive_mode="keep"))
    dirs = sorted(e for e in os.listdir(src) if os.path.isdir(f"{src}/{e}"))
    check("long name: three folders, every name fits in 255 bytes",
          len(dirs) == 3 and all(len(os.fsencode(x)) <= 255 for x in dirs) and s["failed"] == 0 and s["extracted"] == 3,
          ([len(os.fsencode(x)) for x in dirs], s))
    check("long name: cut at a character boundary", all(x.encode("utf-8", "strict") and x.startswith("\u00e9")
          for x in dirs) and "(1)" in "".join(dirs) and "(2)" in "".join(dirs), dirs)
    check("long name: contents intact", sorted(open(f"{src}/{d}/readme.txt").read() for d in dirs) == ["7Z", "RAR", "ZIP"])
    shutil.rmtree(work)


def test_excluded_names():
    work, src, eng = fresh()
    inner = os.path.join(work, "inner.zip")
    mkzip(inner, {"deep.txt": "d", "deep2.txt": "d"})
    for n in ("@eaDir", ".Trash-1000", ".Recycle.Bin", "skipme"):
        mkzip(f"{src}/{n}.zip", {"a.txt": "a", "b.txt": "b", "inner.zip": ("file", inner)})
    s = run(eng, cfg_for(sources=[src], exclude_dirs=["skipme*", "@eaDir", ".Recycle.Bin", ".Trash*"]))
    t = tree(src)
    names = {"@eaDir": "@eaDir_files", ".Trash-1000": "files_.Trash-1000", ".Recycle.Bin": ".Recycle.Bin_files",
             "skipme": "files_skipme"}         # <name>_files, or files_<name> when a wildcard still matches that
    check("excluded name: scannable wrapper (<name>_files) and nested archives inside are extracted",
          all(f"{w}/a.txt" in t and f"{w}/inner/deep.txt" in t for w in names.values()) and s["failed"] == 0, t)
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


def make_pieces(tmp, name, size=90_000, old_style=False):
    """RAR5 volume set (loose payload.bin) built in tmp; returns (piece file names in order, payload bytes).
    old_style renames the set to name.rar + name.r00 ... (only if unrar still reads it, else None)."""
    os.makedirs(tmp, exist_ok=True)
    mkrar_set(tmp, name, size=size)
    parts = sorted(os.listdir(tmp))
    # payload is random: recover it by extracting the set once
    chk = tempfile.mkdtemp(prefix="ea-chk-")
    try:
        subprocess.run(["unrar", "x", "-idq", "-y", os.path.join(tmp, parts[0]), chk + "/"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data = open(os.path.join(chk, "payload.bin"), "rb").read()
    finally:
        shutil.rmtree(chk, ignore_errors=True)
    if old_style:
        os.rename(os.path.join(tmp, parts[0]), os.path.join(tmp, name + ".rar"))
        names = [name + ".rar"]
        for i, f in enumerate(parts[1:]):
            os.rename(os.path.join(tmp, f), os.path.join(tmp, f"{name}.r{i:02d}"))
            names.append(f"{name}.r{i:02d}")
        chk = tempfile.mkdtemp(prefix="ea-chk-")
        try:
            r = subprocess.run(["unrar", "x", "-idq", "-y", os.path.join(tmp, names[0]), chk + "/"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ok = r.returncode == 0 and os.path.exists(os.path.join(chk, "payload.bin"))
        finally:
            shutil.rmtree(chk, ignore_errors=True)
        if not ok:
            return None, None
        return names, data
    return parts, data


def zips_of_pieces(work, src, folder, prefix, tmp, names, extra=None, dup=None):
    """one zip per piece: <prefix>1.zip ... each holding the piece (+ extra files)."""
    zips = []
    for i, n in enumerate(names, 1):
        files = {n: ("file", os.path.join(tmp, n))}
        files.update(extra or {})
        z = os.path.join(folder, f"{prefix}{i}.zip")
        mkzip(z, files)
        zips.append(z)
    return zips


def test_volume_archives():
    for fpa in ("smart", "always"):
        for ow in (False, True):
            for mode in ("delete", "keep"):
                work, src, eng = fresh()
                tmp = os.path.join(work, "pieces")
                names, data = make_pieces(tmp, "rel")
                zips = zips_of_pieces(work, src, src, "release", tmp, names, {"file_id.diz": "same diz"})
                s = run(eng, cfg_for(sources=[src], folder_per_archive=fpa, overwrite=ow, archive_mode=mode))
                t = tree(src)
                tag = f"pieces in zips ({fpa}, overwrite {'on' if ow else 'off'}, {mode})"
                check(f"{tag}: set extracted once into rel/", "rel/payload.bin" in t
                      and open(f"{src}/rel/payload.bin", "rb").read() == data, t)
                check(f"{tag}: no release1/.. folders",
                      not [x for x in os.listdir(src) if x.startswith("release") and os.path.isdir(f"{src}/{x}")], os.listdir(src))
                check(f"{tag}: diz cleaned by the junk rules", not [x for x in t if x.endswith(".diz")], t)
                if mode == "delete":
                    check(f"{tag}: zips and pieces disposed", not [x for x in t if x.endswith((".zip", ".rar"))], t)
                else:
                    check(f"{tag}: zips and the pieces they held are kept (keep mode)",
                          sorted(x for x in t if x.endswith(".zip")) == sorted(os.path.basename(z) for z in zips)
                          and sorted(x for x in t if x.endswith(".rar")) == sorted(names), t)
                check(f"{tag}: all archives extracted, nothing failed",
                      s["extracted"] == len(zips) + 1 and s["failed"] == 0, s)
                m = logs(eng)
                check(f"{tag}: logged in plain words",
                      any("Extracted in place: the archive only holds archive pieces (" in x
                          and "so they stay together with the others" in x for x in m), m)
                if not ow and mode == "keep":
                    kept = settings_kept(eng)
                    check(f"{tag}: info-file clash did not mark zips as kept-because-exists",
                          not any(v.get("why") == "exists" for v in kept.values() if isinstance(v, dict)), kept)
                shutil.rmtree(work)
    # never: unchanged (each zip wrapped, set split)
    work, src, eng = fresh()
    tmp = os.path.join(work, "pieces")
    names, data = make_pieces(tmp, "rel")
    zips_of_pieces(work, src, src, "release", tmp, names)
    run(eng, cfg_for(sources=[src], folder_per_archive="never"))
    t = tree(src)
    check("pieces in zips (never): pieces land in the source folder, set extracted into it",
          "payload.bin" in t, t)
    shutil.rmtree(work)


def settings_kept(eng):
    import json
    try:
        return json.load(open(eng.kept_file))
    except (OSError, ValueError):
        return {}


def test_volume_archives_old_style():
    work, src, eng = fresh()
    tmp = os.path.join(work, "pieces")
    names, data = make_pieces(tmp, "rel", old_style=True)
    if names is None:
        print("NOTE: old-style .rar + .rNN pieces cannot be built in this sandbox (unrar does not accept the "
              "renamed set) - that case is skipped")
        shutil.rmtree(work)
        return
    zips_of_pieces(work, src, src, "rel-", tmp, names, {"rel.nfo": "nfo"})
    s = run(eng, cfg_for(sources=[src]))
    t = tree(src)
    check("old-style pieces (.rar + .rNN) in zips: set extracted once into rel/",
          "rel/payload.bin" in t and open(f"{src}/rel/payload.bin", "rb").read() == data and s["failed"] == 0, (t, s))
    check("old-style pieces: no wrapper folders per zip", not [x for x in os.listdir(src) if x.startswith("rel-")], os.listdir(src))
    shutil.rmtree(work)
    # .z01/.zip pieces and 7z.001 pieces are recognised as pieces too
    from engine import is_archive_piece
    check("pieces: every kind recognised",
          all(is_archive_piece(x) for x in ("a.rar", "a.part02.rar", "a.r00", "a.z01", "a.zip", "a.7z", "a.7z.001",
                                            "a.zip.002", "a.tar.gz", "a.tgz")) and not is_archive_piece("a.pdf"))


def test_volume_archives_release_folder():
    work, src, eng = fresh()
    d = f"{src}/Movie.2016-GRP"
    tmp = os.path.join(work, "pieces")
    names, data = make_pieces(tmp, "movie.2016-grp")
    zips_of_pieces(work, src, d, "movie", tmp, names, {"movie.2016-grp.nfo": "nfo"})
    s = run(eng, cfg_for(sources=[src], overwrite=False))
    t = tree(src)
    check("pieces in a release folder: contents directly in the release folder",
          "Movie.2016-GRP/payload.bin" in t and open(f"{d}/payload.bin", "rb").read() == data, t)
    check("pieces in a release folder: no extra folders, nfo kept",
          not [x for x in t if x.count("/") > 1] and "Movie.2016-GRP/movie.2016-grp.nfo" in t, t)
    check("pieces in a release folder: all extracted", s["failed"] == 0 and s["extracted"] == len(names) + 1, s)
    shutil.rmtree(work)


def test_volume_archives_user_example():
    work, src, eng = fresh()
    for n in (1, 2):
        tmp = os.path.join(work, f"in{n}")
        os.makedirs(tmp)
        for fn in ("one.txt", "two.txt"):
            open(os.path.join(tmp, f"{fn}"), "w").write(f"{n}{fn}")
        subprocess.run(["rar", "a", "-idq", "-y", "-ep", "-ma5", os.path.join(work, f"archive{n}.rar"),
                        os.path.join(tmp, "one.txt"), os.path.join(tmp, "two.txt")], stdin=subprocess.DEVNULL, check=True)
        mkzip(f"{src}/archive{n}.zip", {f"archive{n}.rar": ("file", os.path.join(work, f"archive{n}.rar"))})
    # dry run first: nothing changes
    run(eng, cfg_for(sources=[src], archive_mode="keep"), dry=True)
    check("user example: dry run changes nothing", tree(src) == ["archive1.zip", "archive2.zip"], tree(src))
    s = run(eng, cfg_for(sources=[src], archive_mode="delete"))
    t = tree(src)
    check("user example: each .rar extracts by the normal rule into its own folder",
          t == ["archive1/one.txt", "archive1/two.txt", "archive2/one.txt", "archive2/two.txt"], t)
    check("user example: nothing failed", s["failed"] == 0 and s["extracted"] == 4, s)
    shutil.rmtree(work)
    # keep: the two .rar files sit side by side in the folder, not in archive1/ archive2/
    work, src, eng = fresh()
    for n in (1, 2):
        tmp = os.path.join(work, f"in{n}")
        os.makedirs(tmp)
        open(os.path.join(tmp, "one.txt"), "w").write(str(n))
        subprocess.run(["rar", "a", "-idq", "-y", "-ep", "-ma5", os.path.join(work, f"archive{n}.rar"),
                        os.path.join(tmp, "one.txt")], stdin=subprocess.DEVNULL, check=True)
        mkzip(f"{src}/archive{n}.zip", {f"archive{n}.rar": ("file", os.path.join(work, f"archive{n}.rar"))})
    run(eng, cfg_for(sources=[src], archive_mode="keep", min_age_minutes=0), oneoff=None)
    t = tree(src)
    check("user example (keep): .rar files side by side with the zips, extracted to archive1/ archive2/",
          {"archive1.rar", "archive2.rar", "archive1.zip", "archive2.zip", "archive1/one.txt", "archive2/one.txt"} == set(t), t)
    shutil.rmtree(work)


def test_volume_archives_not_only_pieces():
    work, src, eng = fresh()
    tmp = os.path.join(work, "x")
    os.makedirs(tmp)
    open(f"{tmp}/a.txt", "w").write("a")
    subprocess.run(["rar", "a", "-idq", "-y", "-ep", "-ma5", os.path.join(tmp, "doc.rar"), os.path.join(tmp, "a.txt")],
                   stdin=subprocess.DEVNULL, check=True)
    mkzip(f"{src}/withpdf.zip", {"doc.rar": ("file", f"{tmp}/doc.rar"), "readme.pdf": "pdf"})
    mkzip(f"{src}/infoonly.zip", {"x.nfo": "n", "file_id.diz": "d"})
    run(eng, cfg_for(sources=[src], junk_patterns=[]))
    t = tree(src)
    check("rar plus a real file: wrapped as before", "withpdf/readme.pdf" in t and "withpdf/doc/a.txt" in t, t)
    check("only .nfo/.diz: normal rules (wrapped)", {"infoonly/x.nfo", "infoonly/file_id.diz"} <= set(t), t)
    shutil.rmtree(work)


def test_volume_archives_failure():
    work, src, eng = fresh()
    tmp = os.path.join(work, "pieces")
    names, data = make_pieces(tmp, "rel")
    zips = zips_of_pieces(work, src, src, "release", tmp, names, {"file_id.diz": "d"})
    with open(zips[2], "wb") as f:                 # the zip holding part 3 is corrupt
        f.write(b"this is not a zip file")
    s = run(eng, cfg_for(sources=[src], archive_mode="delete"))
    t = tree(src)
    rar_fail = [f for f in s["failures"] if f["path"].endswith(".rar")]
    zip_fail = [f for f in s["failures"] if f["path"].endswith(".zip")]
    check("missing piece: one failure for the RAR set, one for the broken zip",
          len(rar_fail) == 1 and len(zip_fail) == 1 and s["failed"] == 2, s["failures"])
    left = {n for n in names if n in t}
    check("missing piece: the other pieces are still there, nothing lost",
          left == {n for i, n in enumerate(names) if i != 2}, (left, t))
    check("missing piece: the broken zip stays", os.path.basename(zips[2]) in t, t)
    check("missing piece: nothing half-extracted", not [x for x in t if x.startswith("rel/")], t)
    shutil.rmtree(work)
    # a zip that fails once but works on its own retry: the set waits for it
    work, src, eng = fresh()
    tmp = os.path.join(work, "pieces")
    names, data = make_pieces(tmp, "rel")
    zips = zips_of_pieces(work, src, src, "release", tmp, names)
    real = eng._exec
    state = {"n": 0}

    def flaky(cmd, slot=None):
        if zips[1] in cmd and state["n"] < 2:           # unzip and the 7z fallback both glitch once
            state["n"] += 1
            return 9, "simulated glitch"
        return real(cmd, slot)
    eng._exec = flaky
    s = run(eng, cfg_for(sources=[src], archive_mode="delete"))
    t = tree(src)
    check("flaky zip: retried before the RAR set, set extracted once and complete",
          t == ["rel/payload.bin"] and s["failed"] == 0 and open(f"{src}/rel/payload.bin", "rb").read() == data, (t, s["failures"]))
    check("flaky zip: the RAR set was never started with a piece missing",
          not [x for x in logs(eng) if "Failed, will retry" in x and ".rar" in x], logs(eng))
    shutil.rmtree(work)


def test_volume_archives_duplicate_piece():
    for ow in (False, True):
        work, src, eng = fresh()
        tmp = os.path.join(work, "pieces")
        names, data = make_pieces(tmp, "rel")
        zips_of_pieces(work, src, src, "release", tmp, names)
        mkzip(f"{src}/zdup.zip", {names[1]: ("file", os.path.join(tmp, names[1]))})   # same piece twice
        s = run(eng, cfg_for(sources=[src], overwrite=ow, archive_mode="delete"))
        t = tree(src)
        if ow:
            check("duplicate piece (overwrite on): replaced, everything extracted and disposed",
                  "rel/payload.bin" in t and s["failed"] == 0 and not [x for x in t if x.endswith(".zip")], (t, s))
        else:
            check("duplicate piece (overwrite off): the duplicate's zip is kept (a file was skipped), set extracted",
                  "rel/payload.bin" in t and "zdup.zip" in t and s["failed"] == 0, (t, s))
        shutil.rmtree(work)


def test_volume_archives_wait_for_downloads():
    work, src, eng = fresh()
    tmp = os.path.join(work, "pieces")
    names, data = make_pieces(tmp, "rel")
    zips = zips_of_pieces(work, src, src, "release", tmp, names, {"file_id.diz": "d"})
    old = time.time() - 3600
    for z in zips:
        os.utime(z, (old, old))
    for z in zips[3:]:                                # the last two are still "downloading"
        os.utime(z, None)
    cfg = cfg_for(sources=[src], min_age_minutes=5)
    s = run(eng, cfg)
    t = tree(src)
    check("waiting: old zips extracted in place, pieces gathered",
          all(n in t for n in names[:3]) and not [x for x in os.listdir(src) if os.path.isdir(f"{src}/{x}")], t)
    check("waiting: set not attempted, nothing failed", s["failed"] == 0 and "rel/payload.bin" not in t
          and not os.path.exists(f"{src}/rel"), (s["failures"], t))
    w = s["waiting"]
    check("waiting: the set is listed as waiting with the new zips",
          w and w[0]["dir"] == src and w[0]["count"] == 3 and "rel.part1.rar" in w[0]["names"], w)
    m = logs(eng)
    check("waiting: logged once in plain words",
          len([x for x in m if x.startswith("Waiting: rel.part1.rar - some of its pieces are still in archives that are too new")]) == 1, m)
    check("waiting: nothing remembered as failed", not settings_failed(eng), settings_failed(eng))
    for z in zips[3:]:
        os.utime(z, (old, old))
    s = run(eng, cfg)
    t = tree(src)
    check("waiting: later scan extracts the rest in place and then the set once",
          t == ["rel/payload.bin"] and open(f"{src}/rel/payload.bin", "rb").read() == data and s["failed"] == 0, (t, s))
    shutil.rmtree(work)


def settings_failed(eng):
    import json
    try:
        return json.load(open(eng.failed_file))
    except (OSError, ValueError):
        return {}


def test_volume_archives_wait_across_scans():
    work, src, eng = fresh()
    tmp = os.path.join(work, "pieces")
    names, data = make_pieces(tmp, "rel")
    zips = zips_of_pieces(work, src, src, "release", tmp, names)
    old = time.time() - 3600
    for z in zips[:-1]:
        os.utime(z, (old, old))
    cfg = cfg_for(sources=[src], min_age_minutes=5)
    run(eng, cfg)
    for i in (2, 3):
        eng.log_buf.clear()
        s = run(eng, cfg)
        t = tree(src)
        waits = [x for x in logs(eng) if x.startswith("Waiting: rel.part1.rar")]
        check(f"across scans: run {i} holds the set again, no failure, one Waiting line",
              s["failed"] == 0 and len(waits) == 1 and not os.path.exists(f"{src}/rel")
              and all(n in t for n in names[:-1]) and not [x for x in os.listdir(src) if os.path.isdir(f"{src}/{x}")],
              (s["failures"], waits, t))
        if i == 2:
            check("across scans: the set shows as waiting", s["waiting"] and "rel.part1.rar" in s["waiting"][0]["names"], s["waiting"])
            eng = Engine(os.path.join(work, "cfg", "logs"), os.path.join(work, "cfg", "history.json"))   # a restart
    os.utime(zips[-1], (old, old))
    s = run(eng, cfg)
    t = tree(src)
    check("across scans: once the last zip is old, it and the set extract once",
          t == ["rel/payload.bin"] and open(f"{src}/rel/payload.bin", "rb").read() == data and s["failed"] == 0, (t, s))
    check("across scans: nothing left remembered once the set is extracted", not settings_inplace(eng), settings_inplace(eng))
    shutil.rmtree(work)
    # an ordinary nested archive is never held
    work, src, eng = fresh()
    inner = os.path.join(work, "inner.zip")
    mkzip(inner, {"a.txt": "a"})
    mkzip(f"{src}/outer.zip", {"inner.zip": ("file", inner), "readme.txt": "r"})
    mkzip(f"{src}/new.zip", {"n.txt": "n"})
    old = time.time() - 3600
    os.utime(f"{src}/outer.zip", (old, old))
    run(eng, cfg_for(sources=[src], min_age_minutes=5))
    run(eng, cfg_for(sources=[src], min_age_minutes=5))
    check("across scans: an ordinary nested archive is not held", any(x.endswith("a.txt") for x in tree(src)), tree(src))
    shutil.rmtree(work)


def settings_inplace(eng):
    import json
    try:
        return json.load(open(eng.inplace_path))
    except (OSError, ValueError):
        return {}


def main():
    for fn in (test_names, test_settings, test_smart_basic, test_ten_zips, test_release_folder,
               test_release_inconsistency, test_root_is_never_a_release_folder, test_existing_folder_and_files,
               test_nested, test_same_base_name, test_release_zip_with_rar_parts, test_long_names,
               test_excluded_names, test_volume_archives, test_volume_archives_old_style,
               test_volume_archives_release_folder, test_volume_archives_user_example,
               test_volume_archives_not_only_pieces, test_volume_archives_failure,
               test_volume_archives_duplicate_piece, test_volume_archives_wait_for_downloads, test_volume_archives_wait_across_scans,
               test_modes, test_other_formats, test_oneoff, test_dry_run, test_empty_and_root):
        fn()
    shutil.rmtree(CFG_DIR, ignore_errors=True)
    print("\nALL PASSED" if not FAILS else f"\nFAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
