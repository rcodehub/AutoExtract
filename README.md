# AutoExtract

A Docker container for Unraid (or any Docker host) that scans a folder on a schedule and automatically:

- extracts `zip`, `rar`, `7z` and `tar` archives (incl. `.tar.gz/.tgz/.tar.bz2/.tar.xz`) into the folder they are in,
- extracts archives found inside those archives, as many levels deep as needed,
- deletes the processed archives and junk files such as `*.diz`, or moves them to a trash folder,
- optionally sets permissions and ownership on everything it processed.

Multi-part sets are handled and cleaned up together: `name.part1.rar`…, `name.rar` + `.r00`…, `name.zip` + `.z01`…, `name.7z.001`…. An archive is only deleted or moved after it extracted successfully; anything that fails is left in place and listed with the reason.

Everything is set up in the web UI, which has a live status page, a settings page, scan history, statistics and themes.

> **Created entirely by AI (Claude).** It has been tested, but not in every situation. Try a **Dry run** first, keep backups of data you can't replace, and do your own due diligence before exposing the web UI to the internet.

## Install (Unraid)

Install **AutoExtract** from the **Apps** tab, or download the template by hand:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-AutoExtract.xml \
  https://raw.githubusercontent.com/rcodehub/Unraid-CA-Apps/main/templates/autoextract.xml
```

then **Docker → Add Container → Template: my-AutoExtract**.

> Keep only `my-AutoExtract.xml` in `templates-user`. If you also have another copy, such as `autoextract.xml`, Unraid may load it when you edit or update the container, and your paths go back to the defaults.

## Template settings

| Setting | Container path / variable | Description |
|---|---|---|
| Web UI port | `8080` (host default `8844`) | Web interface |
| App data | `/data` | Settings, history, statistics, logs. e.g. `/mnt/user/appdata/autoextract` |
| Folder to scan | `/downloads` | The share to scan, read/write. e.g. `/mnt/user/downloads` |
| Trash folder (optional) | `/trash` | If set, processed archives and junk are **moved** here instead of deleted. Must be a separate folder, not inside the scan folder or a parent of it. Use the same kind of path as the scan folder (both `/mnt/user/…` or both `/mnt/diskN/…`) |
| Timezone | `TZ` | For times in the logs |
| UI username | `UI_USERNAME` | Login name, default `admin` |
| UI password | `UI_PASSWORD` | Turns on the login page. Leave blank for no login (then anyone who can reach the port can use the UI) |

Only change the *Host Path* side of the paths. On first start `/downloads` is set as the folder to scan, and if `/trash` is mapped, archives and junk are set to be moved there. Everything else is configured in the web UI and saved in `/data`.

## Web UI

- **Status** – next/last scan, progress, what each thread is extracting, failed archives and a live log. Buttons: **Run now**, **Dry run** (shows what would happen without changing anything), **Stop**.
- **Settings** – all options below, with a folder picker and live checks that show each folder's real location on the server.
- **History** – every scan with counts, data size and failures.
- **Stats** – lifetime totals (data and files extracted, space freed, success rate, speed), a 30-day chart, archive types and records.
- **Theme** – 11 colour themes with light and dark versions, plus corner rounding, spacing, font, shadows and page width. Use ☾/☀ to switch light/dark for your browser.

## Settings (web UI)

| Setting | Default | Description |
|---|---|---|
| Source folders | `/downloads` | Folders to scan, including sub-folders. Use container paths (`/downloads`, not `/mnt/user/…`) |
| Scan automatically / interval | off / 60 min | Interval counts from the end of the previous scan |
| Also scan at container start | off | |
| Threads | 4 | Archives extracted at the same time in one folder. 2–4 suits spinning disks |
| Skip archives newer than | 5 min | Leaves archives that changed recently for a later scan, so downloads still in progress aren't touched. Archives that came out of other archives are never skipped. 0 = off |
| Max nested levels | 25 | Safety limit for archives inside archives |
| Overwrite existing files | on | Off keeps existing files |
| Archives that extracted fine | delete | Delete, or move to the move folder |
| Junk files | delete | Delete, or move to the move folder |
| Move folder | `/trash` if mapped | Keeps the folder layout: `<move folder>/<source name>/<path>`; name clashes get `.1`, `.2` |
| Junk file patterns | `*.diz` | One per line, case-insensitive wildcards (`*.nfo`, `Thumbs.db`, `*sample*`). Applies to every scanned folder, so keep them specific |
| Folders to skip | `@eaDir`, `.Recycle.Bin`, `#recycle`, `.Trash*` | Folder names to ignore |
| Set permissions | on, 666 / 777 | File and folder modes for processed files and folders |
| Also set the owner | off, 99:100 | `nobody:users` on Unraid |

## How it works

Each source folder is processed one directory at a time. AutoExtract extracts every archive in the directory, cleans junk and rescans the same directory for newly extracted archives. When the directory is clean it moves on to the sub-folders.

Archives in a directory are extracted in parallel. Each one goes into its own hidden work folder (`.autoextract-…`) and is then moved into place, so parallel extractions never collide. Zip, 7z and tar files go before RAR files, because they often contain the RAR parts.

Extractors are RarLab `unrar` for RAR, 7-Zip for 7z and split archives, `unzip` for zip and GNU `tar` for tarballs. If one fails, 7-Zip tries next. If an archive still fails, it is retried once on its own, then left in place and listed with the reason (damaged, missing part, wrong password, …).

## Docker Compose

See [`docker-compose.yml`](docker-compose.yml).

## License

MIT – see [LICENSE](LICENSE).
