[README.md](https://github.com/user-attachments/files/32309412/README.md)
# AnimDB

AnimDB is a personal media library for your photos and videos. Point it at
folders on your computer and it builds a fast, searchable library on top of
them — thumbnails, tags, favorites, duplicate detection, and a full-screen
viewer — without moving, renaming, or reorganizing a single one of your
actual files unless you explicitly ask it to.

It runs entirely on your own computer. Nothing is uploaded anywhere.

## Installing

1. Download `AnimDB-Setup-<version>.exe` from the [Releases](../../releases) page.
2. Run it. It installs just for your user account, so **no administrator
   rights or UAC prompt are needed**.
3. When it finishes, it can launch AnimDB immediately, and it adds a Start
   Menu shortcut (and, if you checked the box, a desktop shortcut) for next
   time.

> **Windows SmartScreen warning:** AnimDB is a small, independently-built
> app and isn't digitally signed with a paid certificate, so on first run
> Windows may show a blue "Windows protected your PC" screen. This is
> expected for unsigned software, not a sign anything is wrong. Click
> **More info**, then **Run anyway**.

The very first time AnimDB runs, it downloads a few optional components
(thumbnail/EXIF support, video metadata, and Recycle Bin support) into a
small `vendor` folder right next to the app — this needs an internet
connection and takes a few seconds. Every launch after that is instant and
fully offline. If you're offline on first run, or you'd rather skip this,
AnimDB still works — just without thumbnails for images, GPS/EXIF data, and
video length/codec info.

## First run

The first time you open AnimDB, it asks which folder you'd like it to look
after. Click **Browse for a folder to add…** and pick a folder full of
photos or videos — AnimDB scans it (including subfolders) and imports
everything it finds. You can add more folders any time from the **Folders**
tab or the **Rescan / Ingest…** button in the toolbar.

Nothing is copied or moved. AnimDB reads your files where they already are
and keeps its own index (thumbnails, tags, metadata) in a small database
file next to the app.

## What you can do

**Browse and search.** Filenames and descriptions are searchable from the
top bar. Filter by media type, date taken, extension, or tags, and sort by
date taken, date added, file size, or name.

**Folders.** The left-hand rail mirrors your folder structure. Selecting a
folder shows everything inside it, subfolders included. You can create new
virtual folders, rename or move them, and delete them from within AnimDB
without touching anything on disk (unless you ask it to).

**Tags and favorites.** Tag files individually or in bulk, then filter by
tag. Star anything as a favorite from the grid, the list view, or the
full-screen viewer.

**The viewer.** Click any item to open it full-screen. Zoom and pan on
images, scrub video, star favorites, start a slideshow, and open the info
panel for dimensions, file size, dates, GPS location (when a photo has it),
folder membership, tags, and a description field you can edit.

**Duplicates.** AnimDB hashes every file it imports, so exact duplicates
(even ones you renamed or copied into different folders) are detected
automatically. Open **Review Duplicates** from the Stats tab to go through
them one group at a time, or use **Delete All Duplicates** to clear every
group in one step — AnimDB always keeps the largest copy in each group and
sends the rest to the Recycle Bin, never a permanent delete.

**Bulk actions.** Select multiple items (click their checkbox, or
Ctrl/Shift-click) to tag, move, or delete them all at once from the
floating action bar.

**Disk operations.** Renaming, moving, or deleting a file's entry in AnimDB
can optionally apply to the real file on disk too — these are clearly
separated from the "just remove it from my library" actions, and deletions
always go to the Recycle Bin unless you choose permanent delete.

## Settings

Open the **Settings** tab for:

- **Device management** — the master switch for everything that touches
  your disk (importing, renaming, moving, deleting). It's **on by default**
  so AnimDB works out of the box; turn it off to put the app into a
  read-only, browse-and-tag-only mode where it will never write to or
  delete anything on your computer, even from the command line.
- **Auto-ingest on startup** — off by default. Turn it on to have AnimDB
  automatically re-scan all your managed folders every time it starts, so
  new files show up without a manual rescan.
- **Appearance** — light or dark theme, and an accent color (six presets or
  any custom color you like).
- **Reset Managed Folders** — a danger-zone action that forgets every
  folder AnimDB has ingested and wipes its own records of the media inside
  them. It never touches your actual files on disk — only AnimDB's index of
  them.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `/` | Focus the search box |
| `G` | Switch to grid view |
| `L` | Switch to list view |
| `←` / `→` | Previous / next item (in the viewer) |
| `Esc` | Close the viewer or the current dialog |

## Privacy

AnimDB has no account, no analytics, and no network access beyond the
one-time dependency download on first run (which you can skip — see
"Installing" above). Your library never leaves your computer.

## Uninstalling

Uninstall AnimDB from Windows' **Settings → Apps** (or the Start Menu
shortcut it creates) like any other app. You'll be asked whether to keep
or delete your AnimDB database — your original photos and videos on disk
are never touched either way.

## Troubleshooting

**"Windows protected your PC" on first launch.** See the SmartScreen note
under Installing above — click **More info → Run anyway**.

**The app window doesn't open / shows a blank page.** AnimDB's desktop
window needs the Microsoft Edge WebView2 Runtime, which ships with Windows
10/11 by default. If it's missing, download it from
[Microsoft's WebView2 page](https://developer.microsoft.com/microsoft-edge/webview2/)
and try again.

**Thumbnails, video length, or GPS data are missing.** These need the
optional components AnimDB downloads on first run (see "Installing"). If
you were offline then, reconnect and restart AnimDB to retry, or install
them yourself with `pip install Pillow opencv-python-headless send2trash`
if you're running AnimDB from source rather than the installer.

**I want to check which version I have.** Settings tab, at the very
bottom — or run `AnimDB.exe --version` from a command prompt.
