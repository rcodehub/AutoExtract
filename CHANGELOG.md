# Changelog

Newest first. The latest entries are copied into the Unraid template (shown in Community Applications) each time an image is published.

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
