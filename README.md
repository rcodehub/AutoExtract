# AutoExtract

A small Docker container for Unraid (or any Docker host) that watches your folders and automatically:

1. extracts `zip`, `rar`, `7z`, `tar`, `tar.gz/tgz`, `tar.bz2`, `tar.xz` archives into the folder they are in,
2. extracts any archives that came out of those archives (as many levels deep as it takes),
3. cleans up - **deletes or moves** the processed archives and junk files such as `*.diz`,
4. sets everything to everybody read/write (optional, configurable).

It has a web page with a **live status board**, a **settings page for every option**, and a **scan history**. Scans can be started by hand or run on an **interval**.

> **Created entirely by AI (Claude).** It has been tested, but not in every situation. Do your own due diligence before pointing it at data you can't replace (try *Dry run* first), and before exposing it to the internet. Set a UI password (see below), and don't publish the port without a reverse proxy/VPN and proper authentication in front of it.

## How it works

The scan walks each source folder **one directory at a time**. For each directory it extracts every archive in it, removes/moves the archive and junk files, rescans *the same directory* for archives that just appeared, and only when the directory is clean moves on to its sub-folders (including any new ones the extraction created).

Several archives in the same directory are extracted in parallel (the **Threads** setting). A failed archive (corrupt, wrong password, missing tool) is **left in place** and listed on the status page; nothing is deleted unless its extraction succeeded.

Multi-part RARs (`name.part1.rar`, `name.part2.rar`, ... and `name.rar` + `name.r00`, `name.r01`, ...) are extracted once from the first part, and all parts are cleaned up together.

## Web UI

| Tab | What you get |
|---|---|
| **Status** | Next scan countdown, last result, current folder, progress (done / found / remaining / failed), what each thread is working on right now, failed archives, live log |
| **Settings** | Every option below, with a folder picker |
| **History** | Past scans with counts and failures |

Buttons: **Run now**, **Dry run** (shows what *would* happen, changes nothing - nested archives only show up in a real run), **Stop**.

## Settings

| Setting | Meaning |
|---|---|
| Source folders | Folders to scan, with all sub-folders (container paths) |
| Scan automatically / interval | Scan every N minutes, counted from the end of the previous scan. Optional scan at container start |
| Threads | Archives extracted at once within a folder |
| Skip archives newer than | Ignore archives modified in the last N minutes, so files that are still downloading aren't touched (they're picked up on a later scan) |
| Max nested levels | Safety cap for archives inside archives |
| Overwrite existing files | When an extracted file already exists |
| Archives that extracted fine | Delete, or move to the move folder |
| Junk files | Delete, or move to the move folder |
| Move folder | Where "move" puts things. Keeps the layout: `<move folder>/<source folder name>/<relative path>`. Name clashes get `.1`, `.2` |
| Junk file patterns | One per line, matched on file name, case-insensitive (`*.diz`, `Thumbs.db`, `*sample*`) |
| Folders to skip | Folder names to ignore (`@eaDir`, `.Recycle.Bin`, ...) |
| Permissions | Set file mode (default 666) and folder mode (default 777) on every processed folder, its files and its sub-folders; optionally set the owner (Unraid: 99 / 100) |

Settings are stored in `/data/settings.json`, edited from the page - you never have to touch the file, and there are no settings variables in the Unraid template (only the timezone and the login). The settings file is created the moment the container first starts, from the template: `/downloads` becomes the folder to scan, and if you mapped `/trash`, archives and junk files are moved there instead of deleted. Everything is ready to use straight away. Automatic scans start switched off until you enable them. Changes made while a scan is running apply from the next scan. The bottom of the Settings page shows where the file is and when it was last saved; a red banner appears if `/data` is not mapped to a real folder (then settings vanish whenever the container is recreated), and a yellow one lists any saved value that was invalid - the other values are kept.

(For docker-compose users, optional environment variables can override the first-start defaults: `SOURCE_DIRS`, `SCHEDULE_ENABLED`, `INTERVAL_MINUTES`, `THREADS`, `ARCHIVE_MODE`, `JUNK_MODE`, `MOVE_DIR`, `JUNK_PATTERNS`, `PUID`/`PGID`.)

## Login

Set **UI password** (`UI_PASSWORD`) in the template and the web UI shows a sign-in page. The username is **UI username** (`UI_USERNAME`, default `admin`; not case-sensitive; blank also means `admin`). Sessions last 30 days and survive restarts; there is a *Log out* button, and 8 wrong attempts from one address in 5 minutes locks it out for a few minutes. Failed and blocked sign-ins are written to the container log (`docker logs autoextract`) so you can see what is being tried. HTTP basic auth is also accepted, for scripts. With no password set, the UI is open to anyone who can reach it.

If you change the password in the template, just restart the container - old sessions stay valid until they expire or you log out, so for a forced sign-out delete `/data/secret.key` and restart.

## Install on Unraid

### 1. Get the image
**Easiest - build it on the server** (Terminal / SSH):

```bash
mkdir -p /mnt/user/appdata/autoextract-src
# copy this project's files into that folder, then:
cd /mnt/user/appdata/autoextract-src
docker build -t autoextract:latest .
```

**Or publish it from GitHub:** push this repo to GitHub - `.github/workflows/docker.yml` runs the tests and publishes `ghcr.io/<owner>/autoextract` (and Docker Hub too if you add the `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repository secrets).

### 2. Add the container
Copy `autoextract.xml` to `/boot/config/plugins/dockerMan/templates-user/`, then **Docker -> Add Container -> Template: autoextract**. If you built locally, change *Repository* to `autoextract:latest`.

Map:
* **App data** -> `/mnt/user/appdata/autoextract` (container path `/data`)
* **Folder to scan** -> your downloads share, e.g. `/mnt/user/downloads` (container path `/downloads`)
* **Trash folder** (only if you want to *move* archives instead of deleting them) -> e.g. `/mnt/user/trash` (container path `/trash`)
* **UI password** - set one.

Open the web UI. `/downloads` is already set as the folder to scan; check the options on **Settings**, and try **Dry run**.

**Upgrading from an older version:** the container paths changed to `/data`, `/downloads` and `/trash`. Edit the container and set those Container Paths (or remove the container and add it again from the template).

### Docker Compose
See `docker-compose.yml`.

## Paths: host path vs container path

Docker maps a folder on your server (the **host path**, e.g. `/mnt/user/downloads`) to a folder inside the container (the **container path**, e.g. `/downloads`). AutoExtract can only see container paths, so **everything on the Settings page uses container paths** - never the `/mnt/user/...` path.

* The template uses three fixed container paths: `/data` (app data), `/downloads` (folder to scan) and `/trash` (optional move folder). Only change the *Host Path* side. You can map more folders with *Add another Path* and add them as sources; any folder inside the container can be used.
* On the Settings page, the list **Folders mapped into this container** shows exactly what the container can see, with free space and read/write status. Use *Add as source* / *Use as move folder* there, or the **Browse** button.
* Under each folder you type, the page checks it right away: it tells you if the folder doesn't exist inside the container, if it looks like a host path, or if it is read-only.
* On the very first start `/downloads` is set as the folder to scan (if it isn't mapped, any other mapped folder is used instead).
* The **Trash folder** needs its own mapping (e.g. `/mnt/user/trash` -> `/trash`). It must not be inside a scanned folder, and a scanned folder must not be inside it - the page refuses both.
* After changing a mapping in Unraid (Edit -> Apply), reload the page so the list updates.

## Things worth knowing

* **Path mappings:** source folders and the move folder are container paths. Use the same kind of host path for all of them - all `/mnt/user/...` or all `/mnt/diskN/...`. Moving between a user share and a disk path of the same share can lose data on Unraid.
* **Move folder** must be outside every source folder (otherwise the moved archives would be extracted again). The settings page refuses it.
* **Junk patterns apply to every folder that is scanned**, not just files that came out of archives. Keep them specific (`*.txt` would delete every text file).
* **Permissions:** `666`/`777` makes extracted files and folders open to everybody on the network shares. Folders need the execute bit to be opened, which is why they are 777 rather than 666. Extracted scripts lose their execute bit. If you don't want this, turn "Set permissions" off.
* **Ownership:** the container runs as root, so extracted files are owned by root unless you enable "Also set the owner" (99/100 = nobody:users on Unraid).
* **Stop** ends the running extractions. An interrupted archive is left in place (it may have partly extracted - the next scan simply extracts it again over the top).
* **Password-protected archives** fail and are listed; they are never deleted.
* `.rar` files (including multi-part sets) are extracted with RarLab's official `unrar`, `.7z` with 7-Zip, `.zip` with `unzip`, and tar files with GNU `tar`. The image is Debian based because Alpine's 7-Zip has no RAR support.

## Development

```bash
pip install -r requirements.txt
python3 -I tests/test_engine.py            # engine tests (needs unzip + tar)
python3 -I tests/test_auth.py              # login tests
python3 -I tests/test_paths.py             # mapped-folder / path tests
DATA_DIR=/tmp/data DOWNLOADS_DIR=/tmp/downloads PORT=8080 python3 app/main.py   # run the web app locally
```

Layout: `app/engine.py` (extraction), `app/settings.py` (options + validation), `app/main.py` (web app + scheduler), `app/static/index.html` (UI).
