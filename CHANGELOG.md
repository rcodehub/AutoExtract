# Changelog

Newest first. The latest entries are copied into the Unraid template (shown in Community Applications) each time an image is published.

### 2026-11-08
- **Folder per archive:** an archive that only holds pieces of other archives (for example `release1.zip` ... `release20.zip`, each holding one `release.partNN.rar`, plus a `file_id.diz` or `.nfo`) is now extracted in place instead of into its own folder, so the pieces stay together and the RAR set can be extracted. This applies to Smart and Always. The shared `.diz`/`.nfo` files no longer make the zips count as "files already exist", and a zip that fails is retried before the RAR set that may need it.

### 2026-11-07
- New setting **Folder per archive** (default **Smart**). An archive whose contents have no single top folder is now extracted into a folder named after the archive, so ten archives of loose files in one folder no longer end up mixed together. An archive that already holds one folder, or sits alone in a folder named like it (a release folder), is extracted as before. Choose **Always** or **Never** to force either way. Existing installs get Smart; it also applies to nested archives and **Extract folder…**.

### 2026-11-06
- **Cancel** button for each archive being extracted: stops that archive only, throws its partly extracted files away and leaves the archive as it was, while the others carry on. Scheduled scans skip a cancelled archive until it changes; Run now or Extract folder… tries it again.
- Folder pickers and **Extract folder…** only show folders mapped into the container (`/downloads`, `/trash`, `/mnt/user`, ...); container folders such as `/var` are hidden, and the app data folder too.
- Files dated in the future (for example from a clock or timezone mix-up) no longer count as "too new" forever. The time they arrived on the server is used instead, and the log shows a warning.
- With **Overwrite** off, an archive is now kept when any of its files were skipped because they already exist, so nothing is lost. The log says how many files were skipped.
- New **Keep** action for archives and junk. It is the default on a fresh install when there is no trash folder and no reachable Recycle Bin; existing settings are unchanged.
- Kept archives, and archives left in place because their files already existed, are remembered: scheduled scans and Run now no longer extract them again until the archive changes, the action is changed or Overwrite is turned on. **Extract folder…** still extracts them.
- Archives that fail even when retried on their own are remembered too: scheduled scans skip them until they change, Run now tries them again. Status and History show a "skipped" count.
- Archives skipped because they were cancelled earlier now count as done, so "remaining" reaches 0.
- An unexpected error on one archive no longer stops the whole scan.
- A dry run no longer deletes leftover work folders, and **Extract folder…** refuses `/`.
- Settings: the "Where your folders are on the server" box updates as you type, including each source's Recycle Bin. Source and move folders must be inside a mapped folder and outside the app data folder, and invalid saved folders are reset when the settings load.
- Folder picker fixes: a message when no folder is mapped, browsing from the top level works, and the move folder check refreshes after Browse.
- Security: scripts using HTTP basic auth now count toward the failed-login lockout, malformed requests get a clear error instead of a server error, and statistics can no longer be corrupted when several saves happen at once.

### 2026-11-04
- The Status page lists the folders whose archives are waiting because they changed too recently (may still be downloading), with the archive names and when they will be old enough to extract.
- Stats: one "Moved to Recycle Bin / trash" tile with the split underneath, instead of two separate tiles.

### 2026-11-03
- Processed archives and junk can go to the share's **`.Recycle.Bin`**, the folder the Unraid Recycle Bin plugin uses for files deleted over SMB. This is the default on a fresh install when the share can be reached; existing settings are unchanged. Settings shows where each source's Recycle Bin is on your server, and `.Recycle.Bin` folders are never scanned.
- New **Extract folder…** button: pick any folder and extract it now as a one-off job, with or without its sub-folders, choosing for that run what happens to the archives (including keeping them).
- New optional path **All shares** (`/mnt/user`, under advanced settings in the template): with it mapped, the folder pickers and Extract folder can reach any share, and Recycle Bins can be found for folders mapped from inside a share.
- Community Applications shows the publish date, build number and recent changes.
- Build number shown in the web UI (links to the commit); images also tagged `build-N`.
- Wider layout that uses the full screen; appearance panel with 11 themes (light and dark), corners, spacing, font, shadows and page width.
- New Stats tab: data and files extracted, space freed, success rate, speed, 30-day chart, archive types and records.
- Fixed: scans could repeat every few seconds when the folders were empty.
- Clear-log button, log stays scrolled to the newest line; Settings shows the server path behind each folder; folder picker shows hidden folders.

### 2026-11-02
- First release: scheduled scans, nested extraction, junk cleanup (delete or move), permissions, live status page, settings page, history and login.
- Container paths are now `/data` (app data), `/downloads` (folder to scan) and `/trash` (optional move folder); settings are created on first start.
- RAR support fixed (official unrar on a Debian base image); split zips (`.z01`) and `.7z.001` volumes supported.
- More reliable extraction: each archive extracts in its own work folder, 7-Zip is tried if the first extractor fails, failures are retried once, errors are explained.
- Archives found inside archives are no longer skipped as "too recent"; skipped files are listed in the log.
- One invalid setting no longer resets all settings.
