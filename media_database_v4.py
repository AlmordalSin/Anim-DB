#!/usr/bin/env python3
"""
Media Library v4 - clean rewrite.
Run:  python media_database_v4.py ui
      python media_database_v4.py app   (native desktop window, requires pywebview)
      python media_database_v4.py ingest /path/to/folder --mirror-folders
Optional deps: pip install Pillow opencv-python send2trash pywebview
Default DB:    media_library_v4.db (resolved next to this script/exe, not the CWD)
Default port:  7500
"""

import os, sys, json, sqlite3, hashlib, argparse, datetime, threading, email.utils
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PIL import Image, ImageOps
    from PIL.ExifTags import TAGS, GPSTAGS
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import cv2
    CV2_OK = True
except ImportError:
    CV2_OK = False

try:
    import send2trash
    TRASH_OK = True
except ImportError:
    TRASH_OK = False

IMAGE_EXTS = {".jpg",".jpeg",".png",".gif",".bmp",".webp",".tiff",".heic"}
VIDEO_EXTS = {".mp4",".mov",".avi",".mkv",".webm",".m4v",".flv",".wmv"}
DEFAULT_DB   = "media_library_v4.db"
DEFAULT_PORT = 7500

def app_base_dir():
    """Directory the default DB path is resolved against: the folder containing
    this script when run with `python media_database_v4.py`, or the folder
    containing the .exe when frozen with PyInstaller. Deliberately NOT the
    process's current working directory, which is unpredictable for a
    double-clicked desktop app (and was the previous, CWD-relative default)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

SORT_MAP = {
    "date_taken": "COALESCE(m.date_taken, m.date_added)",
    "date_added": "m.date_added",
    "file_size":  "m.file_size",
    "file_name":  "m.file_name COLLATE NOCASE",
}

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
def get_db(path):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT NOT NULL UNIQUE,
            file_name   TEXT NOT NULL,
            media_type  TEXT NOT NULL,
            extension   TEXT NOT NULL,
            file_size   INTEGER,
            width       INTEGER,
            height      INTEGER,
            duration    REAL,
            date_taken  TEXT,
            date_added  TEXT NOT NULL,
            description TEXT DEFAULT '',
            sha256      TEXT,
            extra_meta  TEXT DEFAULT '{}',
            is_favorite INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tags (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            tag      TEXT NOT NULL,
            UNIQUE(media_id, tag)
        );
        CREATE TABLE IF NOT EXISTS folders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            parent_id  INTEGER REFERENCES folders(id) ON DELETE CASCADE,
            disk_path  TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(name, parent_id)
        );
        CREATE TABLE IF NOT EXISTS folder_media (
            folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
            media_id  INTEGER NOT NULL REFERENCES media(id)  ON DELETE CASCADE,
            added_at  TEXT NOT NULL,
            PRIMARY KEY(folder_id, media_id)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS media_fts
            USING fts5(file_name, description, content=media, content_rowid=id);
        CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media BEGIN
            INSERT INTO media_fts(rowid,file_name,description)
            VALUES(new.id,new.file_name,new.description);
        END;
        CREATE TRIGGER IF NOT EXISTS media_ad AFTER DELETE ON media BEGIN
            INSERT INTO media_fts(media_fts,rowid,file_name,description)
            VALUES('delete',old.id,old.file_name,old.description);
        END;
        CREATE TRIGGER IF NOT EXISTS media_au AFTER UPDATE ON media BEGIN
            INSERT INTO media_fts(media_fts,rowid,file_name,description)
            VALUES('delete',old.id,old.file_name,old.description);
            INSERT INTO media_fts(rowid,file_name,description)
            VALUES(new.id,new.file_name,new.description);
        END;
        CREATE INDEX IF NOT EXISTS idx_sha256     ON media(sha256);
        CREATE INDEX IF NOT EXISTS idx_date_taken ON media(date_taken);
        CREATE INDEX IF NOT EXISTS idx_mtype      ON media(media_type);
        CREATE INDEX IF NOT EXISTS idx_tags_tag   ON tags(tag);
        CREATE INDEX IF NOT EXISTS idx_fp_pid     ON folders(parent_id);
        CREATE INDEX IF NOT EXISTS idx_fm_fid     ON folder_media(folder_id);
        CREATE INDEX IF NOT EXISTS idx_fm_mid     ON folder_media(media_id);
    """)
    conn.commit()

    # ── Migration for databases created before is_favorite existed ──
    # Must run before the idx_favorite index below: on an existing DB the
    # CREATE TABLE IF NOT EXISTS above is a no-op, so an older media table
    # won't have this column yet and the index create would fail on it.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    if "is_favorite" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_favorite ON media(is_favorite)")
    conn.commit()

    return conn

# ─────────────────────────────────────────────────────────────────────────────
# METADATA
# ─────────────────────────────────────────────────────────────────────────────
def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            d = f.read(chunk)
            if not d:
                break
            h.update(d)
    return h.hexdigest()

def _dms_to_decimal(dms, ref):
    """Convert an EXIF GPS (degrees, minutes, seconds) triple plus a
    hemisphere reference ('N'/'S'/'E'/'W') into a signed decimal coordinate.
    Returns None if the data is missing or malformed."""
    if not dms or not ref:
        return None
    try:
        def to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return float(v[0]) / float(v[1])
        d, mnt, s = (to_float(v) for v in dms)
        dec = d + mnt / 60 + s / 3600
        if str(ref).upper() in ("S", "W"):
            dec = -dec
        return round(dec, 6)
    except Exception:
        return None

def image_meta(path):
    m = {"width": None, "height": None, "date_taken": None, "extra": {}}
    if not PIL_OK:
        return m
    try:
        with Image.open(path) as img:
            m["width"], m["height"] = img.size
            exif = img._getexif() if hasattr(img, "_getexif") else None
            if exif:
                for tid, val in exif.items():
                    tag = TAGS.get(tid, tid)
                    if tag == "DateTimeOriginal":
                        try:
                            dt = datetime.datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S")
                            m["date_taken"] = dt.isoformat()
                        except Exception:
                            pass
                    elif tag in ("Make", "Model", "Software"):
                        m["extra"][tag] = str(val)
                    elif tag == "GPSInfo":
                        try:
                            gps = {GPSTAGS.get(k, k): v for k, v in val.items()}
                            lat = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
                            lon = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
                            if lat is not None and lon is not None:
                                m["extra"]["gps"] = {"lat": lat, "lon": lon}
                        except Exception:
                            pass
    except Exception as e:
        m["extra"]["err"] = str(e)
    return m

def video_meta(path):
    m = {"width": None, "height": None, "duration": None, "date_taken": None, "extra": {}}
    try:
        mtime = os.path.getmtime(path)
        m["date_taken"] = datetime.datetime.fromtimestamp(mtime).isoformat()
    except Exception:
        pass
    if not CV2_OK:
        return m
    try:
        cap = cv2.VideoCapture(path)
        if cap.isOpened():
            m["width"]  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            m["height"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            fc  = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if fps and fps > 0:
                m["duration"] = round(fc / fps, 2)
        cap.release()
    except Exception as e:
        m["extra"]["err"] = str(e)
    return m

def make_video_thumbnail(src_path, dest_path, max_width=480):
    """Grab one representative frame from a video and save it as a small JPEG
    at dest_path. Returns True on success. Used to give videos a real preview
    image instead of a generic icon, both in the grid and as the lightbox
    video's poster frame."""
    if not CV2_OK:
        return False
    cap = cv2.VideoCapture(src_path)
    try:
        if not cap.isOpened():
            return False
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = (frame_count / fps) if fps > 0 else 0
        # A frame a little into the clip is usually more representative than
        # the very first frame (which is often a black/fade-in frame), but
        # for very short clips just take whatever's available near the start.
        target_sec = min(1.0, duration * 0.1) if duration > 0 else 0
        if target_sec > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, target_sec * 1000)
        ok, frame = cap.read()
        if not ok or frame is None:
            # Some codecs don't seek cleanly; fall back to the very first frame.
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            return False
        h, w = frame.shape[:2]
        if w > max_width:
            new_h = int(h * (max_width / w))
            frame = cv2.resize(frame, (max_width, new_h), interpolation=cv2.INTER_AREA)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        return bool(cv2.imwrite(dest_path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82]))
    except Exception:
        return False
    finally:
        cap.release()

def make_image_thumbnail(src_path, dest_path, max_width=480):
    """Save a small, EXIF-orientation-corrected JPEG preview of an image at
    dest_path. This is what the grid/list views load instead of the full
    original file, since serving full-resolution photos just to show a
    140px tile was the single biggest load-speed issue in the UI. Returns
    True on success."""
    if not PIL_OK:
        return False
    try:
        with Image.open(src_path) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            w, h = img.size
            if w > max_width:
                new_h = max(1, int(h * (max_width / w)))
                img = img.resize((max_width, new_h), Image.LANCZOS)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            img.save(dest_path, "JPEG", quality=82)
        return True
    except Exception:
        return False

def thumbs_dir_for(db_path):
    return os.path.abspath(db_path) + "_thumbs"

def get_thumbnail_path(db_path, media_id, src_path, media_type):
    """Return a cached thumbnail path for this media item (image or video),
    generating - or regenerating, if the source file changed since - as
    needed. Returns None if a thumbnail isn't available (missing file,
    unsupported format, required library not installed, etc.) so callers
    can fall back gracefully."""
    if not os.path.isfile(src_path):
        return None
    thumbs = thumbs_dir_for(db_path)
    dest = os.path.join(thumbs, f"{media_id}.jpg")
    try:
        if os.path.isfile(dest) and os.path.getmtime(dest) >= os.path.getmtime(src_path):
            return dest
    except OSError:
        pass
    ok = (make_image_thumbnail(src_path, dest) if media_type == "image"
          else make_video_thumbnail(src_path, dest))
    return dest if ok else None

# ─────────────────────────────────────────────────────────────────────────────
# INGEST
# ─────────────────────────────────────────────────────────────────────────────
def compute_file_record(path):
    """Pure computation for one file: stat, hash, and metadata extraction
    (EXIF/video-probe). Touches no database connection, so it's safe to run
    concurrently across worker threads - only the actual INSERT/UPDATE needs
    to stay on the caller's thread. Returns a dict ready to write, or None
    if the extension isn't a supported image/video type."""
    path = str(Path(path).resolve())
    ext  = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        mtype, meta = "image", image_meta(path)
    elif ext in VIDEO_EXTS:
        mtype, meta = "video", video_meta(path)
    else:
        return None
    stat = os.stat(path)
    return {
        "file_path":  path,
        "file_name":  Path(path).name,
        "media_type": mtype,
        "extension":  ext.lstrip("."),
        "file_size":  stat.st_size,
        "width":      meta.get("width"),
        "height":     meta.get("height"),
        "duration":   meta.get("duration"),
        "date_taken": meta.get("date_taken"),
        "date_added": datetime.datetime.now().isoformat(),
        "sha256":     file_sha256(path),
        "extra_meta": json.dumps(meta.get("extra", {})),
    }

def write_media_record(conn, row, existing):
    """Insert or update one media row. Always called from the thread that
    owns `conn` - this is the only part of ingest that touches SQLite."""
    if existing:
        conn.execute("""UPDATE media SET file_name=:file_name,file_size=:file_size,
            width=:width,height=:height,duration=:duration,date_taken=:date_taken,
            sha256=:sha256,extra_meta=:extra_meta WHERE file_path=:file_path""", row)
        conn.commit()
        return "updated"
    else:
        conn.execute("""INSERT INTO media(file_path,file_name,media_type,extension,
            file_size,width,height,duration,date_taken,date_added,sha256,extra_meta)
            VALUES(:file_path,:file_name,:media_type,:extension,:file_size,
            :width,:height,:duration,:date_taken,:date_added,:sha256,:extra_meta)""", row)
        conn.commit()
        return "added"

def ingest_file(conn, path, force=False):
    """Ingest a single file, compute + write on the caller's thread. Used by
    the CLI's single-file case and anywhere else a plain sequential ingest
    of one path is wanted."""
    path = str(Path(path).resolve())
    ext  = Path(path).suffix.lower()
    if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
        return "skipped"
    existing = conn.execute("SELECT id FROM media WHERE file_path=?", (path,)).fetchone()
    if existing and not force:
        return "skipped"
    row = compute_file_record(path)
    if row is None:
        return "skipped"
    return write_media_record(conn, row, existing)

def ingest_dir(conn, directory, recursive=True, force=False, mirror=False,
               progress_cb=None, workers=None):
    """Walk `directory` and ingest every supported file found. The slow,
    I/O-bound per-file work (hashing + EXIF/video metadata extraction) runs
    across a small thread pool since it never touches the database, while
    every actual SQLite write happens back on this (the caller's) thread -
    this is what lets a large first-time import run several files at once
    without risking concurrent-write issues."""
    root = Path(directory)
    folder_cache = {}

    def get_or_make_folder(disk_path_str, parent_id=None):
        if disk_path_str in folder_cache:
            return folder_cache[disk_path_str]
        name = Path(disk_path_str).name
        now  = datetime.datetime.now().isoformat()
        row  = conn.execute("SELECT id FROM folders WHERE disk_path=?", (disk_path_str,)).fetchone()
        if row:
            fid = row["id"]
        else:
            cur = conn.execute(
                "INSERT OR IGNORE INTO folders(name,parent_id,disk_path,created_at) VALUES(?,?,?,?)",
                (name, parent_id, disk_path_str, now))
            conn.commit()
            fid = cur.lastrowid or conn.execute(
                "SELECT id FROM folders WHERE disk_path=?", (disk_path_str,)).fetchone()["id"]
        folder_cache[disk_path_str] = fid
        return fid

    def mirror_into_folder(fp, resolved_path):
        try:
            rel_parts = fp.parent.relative_to(root).parts
        except ValueError:
            rel_parts = ()
        pid = None
        running = str(root)
        for part in rel_parts:
            running = str(Path(running) / part)
            pid = get_or_make_folder(running, pid)
        if pid is not None:
            mid_row = conn.execute(
                "SELECT id FROM media WHERE file_path=?", (resolved_path,)).fetchone()
            if mid_row:
                now = datetime.datetime.now().isoformat()
                conn.execute(
                    "INSERT OR IGNORE INTO folder_media(folder_id,media_id,added_at) VALUES(?,?,?)",
                    (pid, mid_row["id"], now))
                conn.commit()

    counts = {"added": 0, "skipped": 0, "updated": 0, "errors": 0}
    pattern = "**/*" if recursive else "*"
    files = [f for f in root.glob(pattern) if f.is_file()]
    total = len(files)
    done  = 0

    def tick():
        nonlocal done
        done += 1
        if done % 50 == 0 or done == total:
            print(f"  [{done}/{total}] {counts}", end="\r")
        if progress_cb:
            progress_cb(done, total, dict(counts))

    # Cheap pass first: skip unsupported extensions and files already in the
    # library (unless --force) without ever hashing them, exactly like the
    # old sequential version did - this needs only one query, on this thread.
    existing_paths = {r["file_path"] for r in
                       conn.execute("SELECT file_path FROM media").fetchall()}
    candidates = []
    for fp in files:
        ext = fp.suffix.lower()
        if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
            counts["skipped"] += 1
            tick()
            continue
        resolved = str(fp.resolve())
        if resolved in existing_paths and not force:
            counts["skipped"] += 1
            tick()
            continue
        candidates.append(fp)

    if workers is None:
        workers = min(8, max(2, (os.cpu_count() or 4)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(compute_file_record, str(fp)): fp for fp in candidates}
        for fut in as_completed(futures):
            fp = futures[fut]
            try:
                row = fut.result()
                if row is None:
                    counts["skipped"] += 1
                else:
                    existing = conn.execute(
                        "SELECT id FROM media WHERE file_path=?", (row["file_path"],)).fetchone()
                    result = write_media_record(conn, row, existing)
                    counts[result] = counts.get(result, 0) + 1
                    if mirror and result in ("added", "updated"):
                        mirror_into_folder(fp, row["file_path"])
            except Exception as e:
                counts["errors"] += 1
                print(f"  ERR {fp.name}: {e}")
            tick()
    print()
    return counts

# ─────────────────────────────────────────────────────────────────────────────
# SEARCH
# ─────────────────────────────────────────────────────────────────────────────
def search(conn, query=None, media_type=None, tags=None, date_from=None,
           date_to=None, ext=None, folder_id=None, dupes_only=False,
           favorites_only=False, sort_by="date_taken", sort_dir="desc",
           limit=50, offset=0):
    wheres, params = [], []
    if folder_id is not None:
        wheres.append("m.id IN (SELECT media_id FROM folder_media WHERE folder_id=?)")
        params.append(folder_id)
    if query:
        wheres.append("m.id IN (SELECT rowid FROM media_fts WHERE media_fts MATCH ?)")
        params.append(query)
    if media_type:
        wheres.append("m.media_type=?")
        params.append(media_type)
    if tags:
        for t in tags:
            wheres.append("m.id IN (SELECT media_id FROM tags WHERE tag=?)")
            params.append(t.strip().lower())
    if date_from:
        wheres.append("m.date_taken >= ?")
        params.append(date_from)
    if date_to:
        wheres.append("m.date_taken <= ?")
        params.append(date_to)
    if ext:
        wheres.append("m.extension=?")
        params.append(ext.lstrip(".").lower())
    if dupes_only:
        wheres.append(
            "m.sha256 IN (SELECT sha256 FROM media WHERE sha256 IS NOT NULL "
            "GROUP BY sha256 HAVING COUNT(*)>1)")
    if favorites_only:
        wheres.append("m.is_favorite=1")

    where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    col = SORT_MAP.get(sort_by, SORT_MAP["date_taken"])
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

    total = conn.execute(
        f"SELECT COUNT(DISTINCT m.id) FROM media m {where_clause}", params).fetchone()[0]
    sql = f"""SELECT m.*, GROUP_CONCAT(t.tag, ', ') AS tags
              FROM media m LEFT JOIN tags t ON t.media_id=m.id
              {where_clause} GROUP BY m.id
              ORDER BY {col} {direction} LIMIT ? OFFSET ?"""
    rows = conn.execute(sql, params + [limit, offset]).fetchall()
    return total, [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────────────────────
# TAGS / FOLDERS
# ─────────────────────────────────────────────────────────────────────────────
def add_tags(conn, media_id, tags):
    for t in tags:
        t = t.strip().lower()
        if t:
            conn.execute("INSERT OR IGNORE INTO tags(media_id,tag) VALUES(?,?)", (media_id, t))
    conn.commit()

def remove_tags(conn, media_id, tags):
    for t in tags:
        conn.execute("DELETE FROM tags WHERE media_id=? AND tag=?", (media_id, t.strip().lower()))
    conn.commit()

def get_tags(conn, media_id):
    return [r["tag"] for r in
            conn.execute("SELECT tag FROM tags WHERE media_id=? ORDER BY tag", (media_id,)).fetchall()]

def get_folders(conn, parent_id=None):
    if parent_id is None:
        rows = conn.execute(
            "SELECT * FROM folders WHERE parent_id IS NULL ORDER BY name").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM folders WHERE parent_id=? ORDER BY name", (parent_id,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["child_count"] = conn.execute(
            "SELECT COUNT(*) FROM folders WHERE parent_id=?", (r["id"],)).fetchone()[0]
        d["media_count"] = conn.execute(
            "SELECT COUNT(*) FROM folder_media WHERE folder_id=?", (r["id"],)).fetchone()[0]
        result.append(d)
    return result

def folder_tree_flat(conn, parent_id=None, depth=0):
    rows = get_folders(conn, parent_id)
    out = []
    for f in rows:
        f["depth"] = depth
        out.append(f)
        out.extend(folder_tree_flat(conn, f["id"], depth + 1))
    return out

def create_folder(conn, name, parent_id=None, disk_path=""):
    now = datetime.datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO folders(name,parent_id,disk_path,created_at) VALUES(?,?,?,?)",
        (name.strip(), parent_id, disk_path, now))
    conn.commit()
    return cur.lastrowid

# ─────────────────────────────────────────────────────────────────────────────
# DISK OPERATIONS
# ─────────────────────────────────────────────────────────────────────────────
def disk_rename(conn, media_id, new_name):
    row = conn.execute("SELECT file_path FROM media WHERE id=?", (media_id,)).fetchone()
    if not row:
        raise FileNotFoundError("Not in database")
    old = Path(row["file_path"])
    new = old.parent / new_name
    old.rename(new)
    conn.execute("UPDATE media SET file_path=?,file_name=? WHERE id=?",
                 (str(new), new_name, media_id))
    conn.commit()
    return str(new)

def disk_move(conn, media_id, dest_folder):
    row = conn.execute("SELECT file_path,file_name FROM media WHERE id=?", (media_id,)).fetchone()
    if not row:
        raise FileNotFoundError("Not in database")
    dest = Path(dest_folder)
    dest.mkdir(parents=True, exist_ok=True)
    new_path = dest / row["file_name"]
    Path(row["file_path"]).rename(new_path)
    conn.execute("UPDATE media SET file_path=? WHERE id=?", (str(new_path), media_id))
    conn.commit()
    return str(new_path)

def disk_delete(conn, media_id, permanent=False):
    row = conn.execute("SELECT file_path FROM media WHERE id=?", (media_id,)).fetchone()
    if not row:
        raise FileNotFoundError("Not in database")
    p = Path(row["file_path"])
    if not p.exists():
        raise FileNotFoundError(f"File not on disk: {p}")
    if permanent:
        p.unlink()
    else:
        if not TRASH_OK:
            raise RuntimeError("send2trash not installed. Run: pip install send2trash")
        send2trash.send2trash(str(p))
    conn.execute("DELETE FROM media WHERE id=?", (media_id,))
    conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def fmt_size(n):
    if n is None:
        return "?"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def _not_modified(headers, mtime):
    """True if the client's If-Modified-Since header shows it already has a
    cached copy at least as new as mtime, so a 304 can be sent instead of
    re-transferring the whole file."""
    ims = headers.get("If-Modified-Since")
    if not ims:
        return False
    try:
        ims_ts = email.utils.parsedate_to_datetime(ims).timestamp()
        return int(mtime) <= int(ims_ts)
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
# WEB UI
# ─────────────────────────────────────────────────────────────────────────────
def _build_server(db_path, host="127.0.0.1", port=DEFAULT_PORT):
    """Build (but do not run) the HTTP server: opens the DB, defines the page
    and the request Handler, and binds the socket. Returns (server, conn) so
    callers can choose how to run it - blocking in the foreground (`run_ui`,
    the `ui` CLI command) or on a background thread inside a native window
    (`run_desktop_app`, the `app` CLI command)."""
    import http.server, urllib.parse

    conn = get_db(db_path)
    if not TRASH_OK:
        print("  NOTE: install send2trash for Recycle Bin support:  pip install send2trash")

    # ── Background ingest (UI-driven Rescan / Ingest button) ──
    # Runs on its own thread with its own DB connection (WAL mode makes this
    # safe alongside the request-handling connection) so a long scan never
    # blocks the HTTP server from answering other requests, including the
    # status polls the UI uses to show progress.
    ingest_lock  = threading.Lock()
    ingest_state = {"running": False, "folder": None, "current": 0, "total": 0,
                     "counts": {}, "done": False, "error": None,
                     "started_at": None, "finished_at": None}

    def _run_ingest_bg(folder, mirror, force, recursive):
        ing_conn = get_db(db_path)
        try:
            def progress(i, total, counts):
                with ingest_lock:
                    ingest_state["current"] = i
                    ingest_state["total"]   = total
                    ingest_state["counts"]  = counts
            counts = ingest_dir(ing_conn, folder, recursive=recursive,
                                 force=force, mirror=mirror, progress_cb=progress)
            with ingest_lock:
                ingest_state["counts"] = counts
        except Exception as e:
            with ingest_lock:
                ingest_state["error"] = str(e)
        finally:
            with ingest_lock:
                ingest_state["running"]     = False
                ingest_state["done"]        = True
                ingest_state["finished_at"] = datetime.datetime.now().isoformat()
            ing_conn.close()

    PAGE = (
        "<!DOCTYPE html>\n"
        "<html lang='en'>\n"
        "<head>\n"
        "<meta charset='UTF-8'>\n"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
        "<title>Media Library v4</title>\n"
        "<style>\n"
        ":root{"
        "--bg:#111318;--sf:#1a1d24;--sf2:#22262f;--bd:#2d3140;"
        "--ac:#5b8af0;--ac2:#7aa3f5;--tx:#dde2f0;--mu:#7880a0;"
        "--red:#e05468;--green:#3ecf8e;--tagbg:#1e2d5a;--tagtx:#7aa3f5;"
        "--r:6px;--fn:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "--mo:'SF Mono','Fira Code',monospace;"
        "}\n"
        "*{box-sizing:border-box;margin:0;padding:0}\n"
        "body{background:var(--bg);color:var(--tx);font-family:var(--fn);"
        "font-size:14px;height:100vh;overflow:hidden;display:flex;flex-direction:column}\n"

        ".topbar{height:52px;background:var(--sf);border-bottom:1px solid var(--bd);"
        "display:flex;align-items:center;padding:0 16px;gap:12px;flex-shrink:0}\n"
        ".logo{font-weight:700;font-size:15px;color:var(--ac2);white-space:nowrap}\n"
        ".logo small{color:var(--mu);font-weight:400;font-size:11px;margin-left:4px}\n"
        ".srch{flex:1;max-width:500px;background:var(--sf2);border:1px solid var(--bd);"
        "border-radius:var(--r);color:var(--tx);padding:7px 12px;font-size:13px;outline:none}\n"
        ".srch:focus{border-color:var(--ac)}\n"
        ".sel{background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r);"
        "color:var(--tx);padding:5px 8px;font-size:12px;outline:none;cursor:pointer}\n"
        ".sel:focus{border-color:var(--ac)}\n"
        ".icoBtn{background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r);"
        "color:var(--tx);padding:5px 10px;font-size:13px;cursor:pointer}\n"
        ".icoBtn:hover{border-color:var(--ac)}\n"
        ".filtmenu{position:absolute;top:100%;right:0;margin-top:6px;width:260px;"
        "max-height:80vh;overflow-y:auto;background:var(--sf);border:1px solid var(--bd);"
        "border-radius:var(--r);padding:12px;z-index:50;box-shadow:0 8px 24px rgba(0,0,0,.35)}\n"

        ".shell{display:flex;flex:1;overflow:hidden}\n"

        ".sidebar{width:230px;flex-shrink:0;background:var(--sf);"
        "border-right:1px solid var(--bd);display:flex;flex-direction:column;overflow:hidden}\n"
        ".stabs{display:flex;border-bottom:1px solid var(--bd);flex-shrink:0}\n"
        ".stab{flex:1;padding:9px 0;text-align:center;font-size:11px;font-weight:600;"
        "color:var(--mu);cursor:pointer;border-bottom:2px solid transparent}\n"
        ".stab.on{color:var(--ac2);border-bottom-color:var(--ac)}\n"
        ".sbody{overflow-y:auto;flex:1;padding:12px}\n"
        ".fg{margin-bottom:12px}\n"
        ".fg label{display:block;font-size:11px;color:var(--mu);margin-bottom:4px}\n"
        ".fg select,.fg input[type=text],.fg input[type=date]{"
        "width:100%;background:var(--sf2);border:1px solid var(--bd);"
        "border-radius:var(--r);color:var(--tx);padding:6px 8px;font-size:12px;outline:none}\n"
        ".fg select:focus,.fg input:focus{border-color:var(--ac)}\n"
        ".fg .chk{display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer}\n"
        ".fbtn{width:100%;background:var(--ac);color:#fff;border:none;"
        "border-radius:var(--r);padding:7px;font-size:12px;font-weight:600;cursor:pointer}\n"
        ".fbtn:hover{opacity:.85}\n"
        ".fbtn.ghost{background:var(--sf2);color:var(--mu);border:1px solid var(--bd);margin-top:5px}\n"

        ".ftrow{display:flex;align-items:center;gap:4px;padding:4px 6px;"
        "border-radius:var(--r);cursor:default;font-size:12px;user-select:none}\n"
        ".ftrow:hover{background:var(--sf2)}\n"
        ".ftrow.on{background:var(--sf2);color:var(--ac2)}\n"
        ".tgl{width:16px;height:16px;display:flex;align-items:center;justify-content:center;"
        "font-size:9px;color:var(--mu);cursor:pointer;flex-shrink:0;border-radius:3px}\n"
        ".tgl:hover{background:var(--bd);color:var(--tx)}\n"
        ".fn{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}\n"
        ".fc{font-size:10px;color:var(--mu);flex-shrink:0}\n"
        ".fnew{width:100%;background:none;border:1px dashed var(--bd);border-radius:var(--r);"
        "color:var(--mu);padding:5px;font-size:11px;cursor:pointer;margin-top:6px}\n"
        ".fnew:hover{border-color:var(--ac);color:var(--ac2)}\n"

        ".tchip{display:inline-flex;align-items:center;gap:3px;background:var(--tagbg);"
        "color:var(--tagtx);border-radius:3px;padding:2px 7px;font-size:11px;"
        "margin:2px;cursor:pointer}\n"
        ".tchip:hover{opacity:.8}\n"

        ".srow{display:flex;justify-content:space-between;font-size:12px;"
        "padding:4px 0;border-bottom:1px solid var(--bd)}\n"
        ".srow span:last-child{color:var(--ac2);font-weight:600}\n"

        ".content{flex:1;display:flex;flex-direction:column;overflow:hidden}\n"
        ".toolbar{padding:8px 16px;background:var(--sf);border-bottom:1px solid var(--bd);"
        "display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap}\n"
        ".tcnt{font-size:12px;color:var(--mu);flex:1}\n"
        ".ingpill{font-size:11px;color:var(--ac2);background:var(--sf2);"
        "border:1px solid var(--bd);border-radius:12px;padding:3px 10px;white-space:nowrap}\n"
        ".vbtns{display:flex;gap:3px}\n"
        ".vbtn{background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r);"
        "color:var(--mu);padding:4px 9px;font-size:14px;cursor:pointer}\n"
        ".vbtn.on{background:var(--ac);border-color:var(--ac);color:#fff}\n"

        ".bulkbar{display:none;background:rgba(91,138,240,.1);"
        "border-bottom:1px solid rgba(91,138,240,.3);padding:7px 16px;"
        "align-items:center;gap:10px;font-size:12px;flex-shrink:0}\n"
        ".bulkbar.show{display:flex}\n"
        ".bulkbar span{flex:1}\n"
        ".bb{background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r);"
        "color:var(--tx);padding:4px 11px;font-size:11px;cursor:pointer}\n"
        ".bb:hover{border-color:var(--ac)}\n"
        ".bb.red{border-color:rgba(224,84,104,.3);color:var(--red)}\n"
        ".bb.red:hover{background:var(--red);border-color:var(--red);color:#fff}\n"

        ".mscroll{overflow-y:auto;flex:1}\n"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));"
        "gap:6px;padding:10px}\n"
        ".card{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);"
        "overflow:hidden;cursor:pointer;transition:border-color .12s,transform .1s;position:relative}\n"
        ".card:hover{border-color:var(--ac);transform:translateY(-1px)}\n"
        ".card.sel{border-color:var(--ac);outline:2px solid var(--ac);outline-offset:-1px}\n"
        ".thumb{width:100%;aspect-ratio:1;background:var(--sf2);display:flex;"
        "align-items:center;justify-content:center;overflow:hidden;position:relative}\n"
        ".thumb img{width:100%;height:100%;object-fit:cover;display:block}\n"
        ".thumb .ico{font-size:30px;color:var(--mu);display:none}\n"
        ".thumb.thumbfail .ico{display:flex}\n"
        ".thumb.thumbfail img{display:none}\n"
        ".lthumb .ico{font-size:14px;color:var(--mu);display:none}\n"
        ".lthumb.thumbfail .ico{display:flex;align-items:center;justify-content:center;width:100%;height:100%}\n"
        ".lthumb.thumbfail img{display:none}\n"
        ".pill{position:absolute;border-radius:3px;padding:1px 5px;font-size:9px}\n"
        ".tpill{top:4px;right:4px;background:rgba(0,0,0,.65);font-family:var(--mo);color:var(--ac2)}\n"
        ".dpill{top:4px;left:4px;background:var(--red);font-weight:700;color:#fff}\n"
        ".vrpill{bottom:4px;right:4px;background:rgba(0,0,0,.65);color:#fff}\n"
        ".favbtn{border:none;cursor:pointer;line-height:1}\n"
        ".favbtn.cardfav{position:absolute;bottom:4px;left:4px;background:rgba(0,0,0,.55);"
        "color:#fff;font-size:14px;padding:2px 5px;border-radius:3px;opacity:.85}\n"
        ".favbtn.cardfav:hover{opacity:1}\n"
        ".favbtn.cardfav.on{color:#ffce45;opacity:1}\n"
        ".favbtn.listfav{background:none;color:var(--mu);font-size:15px;padding:2px}\n"
        ".favbtn.listfav.on{color:#ffce45}\n"
        ".cfoot{padding:5px 7px}\n"
        ".cname{font-size:10px;font-weight:500;white-space:nowrap;overflow:hidden;"
        "text-overflow:ellipsis;margin-bottom:1px}\n"
        ".csub{font-size:9px;color:var(--mu)}\n"
        ".ctags{display:flex;flex-wrap:wrap;gap:2px;margin-top:2px}\n"

        ".list{padding:8px 12px;display:flex;flex-direction:column;gap:4px}\n"
        ".lrow{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);"
        "padding:7px 10px;cursor:pointer;display:grid;"
        "grid-template-columns:34px 1fr 70px 100px 75px 22px;"
        "align-items:center;gap:9px;transition:border-color .12s}\n"
        ".lrow:hover,.lrow.sel{border-color:var(--ac)}\n"
        ".lthumb{width:30px;height:30px;border-radius:4px;overflow:hidden;"
        "background:var(--sf2);display:flex;align-items:center;justify-content:center;font-size:15px}\n"
        ".lthumb img{width:100%;height:100%;object-fit:cover}\n"
        ".lname{font-size:11px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n"
        ".lsub{font-size:10px;color:var(--mu);margin-top:1px}\n"
        ".lcell{font-size:10px;color:var(--mu)}\n"

        ".pager{padding:8px 16px;display:flex;align-items:center;justify-content:center;"
        "gap:5px;background:var(--sf);border-top:1px solid var(--bd);flex-shrink:0;flex-wrap:wrap}\n"
        ".pager button{background:var(--sf2);border:1px solid var(--bd);border-radius:var(--r);"
        "color:var(--tx);padding:4px 10px;font-size:11px;cursor:pointer}\n"
        ".pager button.on{background:var(--ac);border-color:var(--ac);color:#fff}\n"
        ".pager button:hover:not(.on){border-color:var(--ac)}\n"
        ".pinfo{font-size:10px;color:var(--mu)}\n"

        ".empty{text-align:center;padding:60px 20px;color:var(--mu)}\n"
        ".eico{font-size:44px;margin-bottom:10px}\n"

        ".lbox{position:fixed;inset:0;background:rgba(0,0,0,.95);z-index:200;"
        "display:none;flex-direction:column}\n"
        ".lbox.open{display:flex}\n"
        ".lbtop{position:relative;z-index:10;display:flex;align-items:center;padding:9px 14px;gap:10px;"
        "background:rgba(0,0,0,.5);flex-shrink:0}\n"
        ".lbtitle{font-size:13px;font-weight:600;flex:1;overflow:hidden;"
        "text-overflow:ellipsis;white-space:nowrap}\n"
        ".lbacts{display:flex;gap:6px}\n"
        ".lbtn{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.15);"
        "border-radius:var(--r);color:#fff;padding:5px 11px;font-size:12px;cursor:pointer}\n"
        ".lbtn:hover{background:rgba(255,255,255,.2)}\n"
        ".lbtn.active{color:#ffce45;border-color:#ffce45}\n"
        ".lbmain{flex:1;display:flex;align-items:stretch;justify-content:center;"
        "overflow:hidden;position:relative}\n"
        ".lbmain img{width:calc(100% - 100px);height:100%;object-fit:contain;display:block;"
        "cursor:zoom-in;transition:transform .05s linear;user-select:none;-webkit-user-drag:none}\n"
        ".lbmain video{max-width:calc(100% - 100px);max-height:100%;display:block;outline:none;z-index:1}\n"
        ".lbnav{position:absolute;top:50%;transform:translateY(-50%);"
        "background:rgba(0,0,0,.45);border:none;color:#fff;font-size:24px;"
        "padding:14px 8px;cursor:pointer;border-radius:var(--r);opacity:.6;z-index:10}\n"
        ".lbnav.p{left:10px}\n"
        ".lbnav.n{right:10px}\n"
        ".lbzoom{display:flex;align-items:center;gap:4px}\n"
        ".lbzpct{min-width:46px;text-align:center}\n"

        ".lbinfo{position:absolute;right:0;top:47px;bottom:0;width:310px;z-index:20;"
        "background:var(--sf);border-left:1px solid var(--bd);overflow-y:auto;"
        "transform:translateX(100%);transition:transform .2s ease;display:flex;flex-direction:column}\n"
        ".lbinfo.open{transform:translateX(0)}\n"
        ".ihead{padding:12px 14px;border-bottom:1px solid var(--bd);"
        "display:flex;align-items:center;justify-content:space-between;flex-shrink:0}\n"
        ".ihead h3{font-size:13px;font-weight:600}\n"
        ".iclose{background:none;border:none;color:var(--mu);font-size:17px;cursor:pointer}\n"
        ".ibody{padding:12px 14px;display:flex;flex-direction:column;gap:14px}\n"
        ".isec h4{font-size:10px;color:var(--mu);text-transform:uppercase;letter-spacing:.5px;margin-bottom:7px}\n"
        ".mrow{display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid var(--bd)}\n"
        ".mrow span:first-child{color:var(--mu)}\n"
        ".mrow span:last-child{font-family:var(--mo);font-size:10px;max-width:170px;"
        "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right}\n"
        ".taglist{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}\n"
        ".tpill2{background:var(--tagbg);color:var(--tagtx);border-radius:3px;"
        "padding:2px 7px;font-size:11px;display:flex;align-items:center;gap:3px}\n"
        ".tpill2 button{background:none;border:none;color:var(--tagtx);"
        "cursor:pointer;font-size:12px;line-height:1;padding:0}\n"
        ".tpill2 button:hover{color:var(--red)}\n"
        ".tadd{display:flex;gap:5px;margin-top:6px}\n"
        ".tadd input{flex:1;background:var(--sf2);border:1px solid var(--bd);"
        "border-radius:var(--r);color:var(--tx);padding:5px 7px;font-size:11px;outline:none}\n"
        ".tadd input:focus{border-color:var(--ac)}\n"
        ".tadd button{background:var(--ac);border:none;border-radius:var(--r);"
        "color:#fff;padding:5px 9px;font-size:11px;cursor:pointer}\n"
        ".darea{width:100%;background:var(--sf2);border:1px solid var(--bd);"
        "border-radius:var(--r);color:var(--tx);padding:6px;font-size:11px;"
        "font-family:var(--fn);resize:vertical;min-height:55px;outline:none}\n"
        ".darea:focus{border-color:var(--ac)}\n"
        ".dsk{border:1px solid rgba(224,84,104,.2);border-radius:var(--r);padding:9px}\n"
        ".dsk h4{color:var(--red)}\n"
        ".dbtn{width:100%;background:var(--sf2);border:1px solid var(--bd);"
        "border-radius:var(--r);color:var(--tx);padding:5px 9px;font-size:11px;"
        "cursor:pointer;text-align:left;margin-top:4px}\n"
        ".dbtn:hover{border-color:var(--ac)}\n"
        ".dbtn.red{border-color:rgba(224,84,104,.3);color:var(--red)}\n"
        ".dbtn.red:hover{background:var(--red);border-color:var(--red);color:#fff}\n"
        ".fcheck label{display:flex;align-items:center;gap:7px;font-size:11px;"
        "padding:3px 0;cursor:pointer}\n"

        ".dlgbg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:420;"
        "display:none;align-items:center;justify-content:center;padding:20px}\n"
        ".dlgbg.open{display:flex}\n"
        ".dlg{background:var(--sf);border:1px solid var(--bd);border-radius:10px;"
        "padding:22px;width:min(440px,100%);display:flex;flex-direction:column;gap:12px}\n"
        ".dlg h3{font-size:14px;font-weight:700}\n"
        ".dlg p{font-size:12px;color:var(--mu);line-height:1.5}\n"
        ".dlgpath{font-size:10px;color:var(--mu);background:var(--sf2);"
        "border-radius:3px;padding:5px 7px;font-family:var(--mo);word-break:break-all}\n"
        ".dlg input{width:100%;background:var(--sf2);border:1px solid var(--bd);"
        "border-radius:var(--r);color:var(--tx);padding:7px 9px;font-size:12px;outline:none}\n"
        ".dlg input:focus{border-color:var(--ac)}\n"
        ".dlgact{display:flex;gap:8px;justify-content:flex-end}\n"
        ".dlgact button{background:var(--sf2);border:1px solid var(--bd);"
        "border-radius:var(--r);color:var(--tx);padding:6px 14px;font-size:12px;cursor:pointer}\n"
        ".dlgact .ok{background:var(--ac);border-color:var(--ac);color:#fff}\n"
        ".dlgact .danger{background:var(--red);border-color:var(--red);color:#fff}\n"

        ".dupbg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:400;"
        "display:none;align-items:center;justify-content:center;padding:20px}\n"
        ".dupbg.open{display:flex}\n"
        ".dupdlg{background:var(--sf);border:1px solid var(--bd);border-radius:10px;"
        "width:min(760px,100%);max-height:85vh;display:flex;flex-direction:column;overflow:hidden}\n"
        ".duphead{padding:14px 18px;border-bottom:1px solid var(--bd);display:flex;"
        "align-items:center;justify-content:space-between;flex-shrink:0}\n"
        ".duphead h3{font-size:14px;font-weight:700}\n"
        ".dupbody{overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:16px}\n"
        ".dupgroup{border:1px solid var(--bd);border-radius:var(--r);padding:10px}\n"
        ".dupgtitle{font-size:11px;color:var(--mu);margin-bottom:8px}\n"
        ".dupitems{display:flex;flex-wrap:wrap;gap:8px}\n"
        ".dupitem{width:130px;background:var(--sf2);border:2px solid transparent;"
        "border-radius:var(--r);overflow:hidden;font-size:10px;flex-shrink:0}\n"
        ".dupitem img{width:100%;height:80px;object-fit:cover;display:block;background:var(--bg)}\n"
        ".dupitem .di-meta{padding:6px 7px}\n"
        ".dupitem .di-path{color:var(--tx);word-break:break-all;font-size:9px;"
        "margin-bottom:2px;max-height:24px;overflow:hidden}\n"
        ".dupitem .di-size{color:var(--mu);margin-bottom:4px}\n"
        ".dupitem button{width:100%;font-size:10px;padding:3px;border-radius:4px;"
        "border:1px solid var(--bd);background:var(--sf);color:var(--tx);"
        "cursor:pointer;margin-top:3px}\n"
        ".dupitem button.keepbtn{border-color:var(--green);color:var(--green)}\n"
        ".dupitem button.keepbtn:hover{background:var(--green);color:#0a0a0a}\n"
        ".dupitem button.delbtn{border-color:var(--red);color:var(--red)}\n"
        ".dupitem button.delbtn:hover{background:var(--red);color:#fff}\n"

        ".toastwrap{position:fixed;bottom:18px;right:18px;display:flex;"
        "flex-direction:column;gap:5px;z-index:500}\n"
        ".toast{background:var(--sf);border:1px solid var(--bd);border-radius:var(--r);"
        "padding:8px 13px;font-size:12px;animation:tin .18s ease}\n"
        "@keyframes tin{from{transform:translateY(6px);opacity:0}}\n"
        ".toast.ok{border-color:var(--green);color:var(--green)}\n"
        ".toast.err{border-color:var(--red);color:var(--red)}\n"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        "<div class='topbar'>\n"
        "  <div class='logo'>&#128193; Media Library <small>v4</small></div>\n"
        "  <input class='srch' id='q' placeholder='Search filenames and descriptions&#8230;' />\n"
        "  <label style='font-size:11px;color:var(--mu)'>Sort</label>\n"
        "  <select class='sel' id='sortBy' onchange='goPage(0)'>\n"
        "    <option value='date_taken'>Date Taken</option>\n"
        "    <option value='date_added'>Date Added</option>\n"
        "    <option value='file_size'>File Size</option>\n"
        "    <option value='file_name'>File Name</option>\n"
        "  </select>\n"
        "  <button class='icoBtn' id='dirBtn' onclick='toggleDir()' title='Toggle direction'>&#8595;</button>\n"
        "  <div style='position:relative'>\n"
        "    <button class='icoBtn' id='filterBtn' onclick='toggleFilterMenu(event)'>&#9660; Filters</button>\n"
        "    <div class='filtmenu' id='filtmenu' style='display:none'>\n"
        "      <div class='fg'><label>Media type</label>\n"
        "        <select id='fType' onchange='goPage(0)'>\n"
        "          <option value=''>All</option>\n"
        "          <option value='image'>Images</option>\n"
        "          <option value='video'>Videos</option>\n"
        "        </select></div>\n"
        "      <div class='fg'><label>Date taken from</label><input type='date' id='fFrom'/></div>\n"
        "      <div class='fg'><label>Date taken to</label><input type='date' id='fTo'/></div>\n"
        "      <div class='fg'><label>Extension</label><input type='text' id='fExt' placeholder='jpg, mp4&#8230;'/></div>\n"
        "      <div class='fg'><label>Tags (space-separated)</label><input type='text' id='fTags' placeholder='vacation beach'/></div>\n"
        "      <div class='fg'><label class='chk'><input type='checkbox' id='fDupes'/> Duplicates only</label></div>\n"
        "      <div class='fg'><label class='chk'><input type='checkbox' id='fFav'/> Favorites only</label></div>\n"
        "      <button class='fbtn' onclick='goPage(0);closeFilterMenu()'>Apply</button>\n"
        "      <button class='fbtn ghost' onclick='clearFilters()'>Clear</button>\n"
        "    </div>\n"
        "  </div>\n"
        "  <label style='font-size:11px;color:var(--mu);margin-left:4px'>Show</label>\n"
        "  <select class='sel' id='perPage' onchange='goPage(0)'>\n"
        "    <option value='25'>25</option>\n"
        "    <option value='50' selected>50</option>\n"
        "    <option value='75'>75</option>\n"
        "    <option value='100'>100</option>\n"
        "  </select>\n"
        "</div>\n"
        "<div class='shell'>\n"
        "<div class='sidebar'>\n"
        "  <div class='stabs'>\n"
        "    <div class='stab on' onclick=\"showSB('folders')\">Folders</div>\n"
        "    <div class='stab' onclick=\"showSB('tags')\">Tags</div>\n"
        "    <div class='stab' onclick=\"showSB('stats')\">Stats</div>\n"
        "  </div>\n"
        "  <div class='sbody'>\n"
        "    <div id='sb-folders'>\n"
        "      <div id='ftree'></div>\n"
        "      <button class='fnew' onclick='newFolder(null)'>+ New root folder</button>\n"
        "    </div>\n"
        "    <div id='sb-tags' style='display:none'><div id='tagcloud'></div></div>\n"
        "    <div id='sb-stats' style='display:none'><div id='statpanel'></div></div>\n"
        "  </div>\n"
        "</div>\n"
        "<div class='content'>\n"
        "  <div class='toolbar'>\n"
        "    <span class='tcnt' id='tcnt'>&#8211;</span>\n"
        "    <span class='ingpill' id='ingpill' style='display:none'></span>\n"
        "    <button class='icoBtn' onclick='rescanIngest()' "
        "title='Scan a folder on disk for new or changed media'>&#8635; Rescan / Ingest&#8230;</button>\n"
        "    <div class='vbtns'>\n"
        "      <button class='vbtn on' id='vg' onclick=\"setView('grid')\">&#8862;</button>\n"
        "      <button class='vbtn' id='vl' onclick=\"setView('list')\">&#9776;</button>\n"
        "    </div>\n"
        "  </div>\n"
        "  <div class='bulkbar' id='bulkbar'>\n"
        "    <span id='bkcnt'>0 selected</span>\n"
        "    <button class='bb' onclick='bulkTag()'>&#127991;&#65039; Tag&#8230;</button>\n"
        "    <button class='bb' onclick='bulkMove()'>&#128193; Move&#8230;</button>\n"
        "    <button class='bb red' onclick='bulkDelete()'>&#128465; Delete</button>\n"
        "    <button class='bb' onclick='clearSel()'>&#x2715; Clear</button>\n"
        "  </div>\n"
        "  <div class='mscroll' id='mscroll'><div class='grid' id='mgrid'></div></div>\n"
        "  <div class='pager' id='pager'></div>\n"
        "</div>\n"
        "</div>\n"
        "<!-- LIGHTBOX -->\n"
        "<div class='lbox' id='lbox'>\n"
        "  <div class='lbtop'>\n"
        "    <span class='lbtitle' id='lbtitle'>&nbsp;</span>\n"
        "    <div class='lbzoom' id='lbzoom' style='display:none'>\n"
        "      <button class='lbtn' onclick='lbZoomBy(-0.25)' title='Zoom out'>&#8722;</button>\n"
        "      <button class='lbtn lbzpct' id='lbzoompct' onclick='lbZoomReset()' title='Reset zoom'>100%</button>\n"
        "      <button class='lbtn' onclick='lbZoomBy(0.25)' title='Zoom in'>&#43;</button>\n"
        "    </div>\n"
        "    <div class='lbacts'>\n"
        "      <button class='lbtn' id='lbfav' onclick='toggleFavCurrent()'>&#9734; Favorite</button>\n"
        "      <button class='lbtn' id='lbslide' onclick='toggleSlideshow()'>&#9654; Slideshow</button>\n"
        "      <button class='lbtn' onclick='toggleInfo()'>&#9432; Info</button>\n"
        "      <button class='lbtn' onclick='lbNav(-1)'>&#9664; Prev</button>\n"
        "      <button class='lbtn' onclick='lbNav(1)'>Next &#9654;</button>\n"
        "      <button class='lbtn' onclick='closeLb()'>&#x2715; Close</button>\n"
        "    </div>\n"
        "  </div>\n"
        "  <div class='lbmain' id='lbmain' onclick='lbMainClick(event)'>\n"
        "    <button class='lbnav p' onclick='lbNav(-1)'>&#8249;</button>\n"
        "    <div id='lbmedia' style='display:flex;align-items:center;justify-content:center;flex:1;max-height:100%;overflow:hidden'></div>\n"
        "    <button class='lbnav n' onclick='lbNav(1)'>&#8250;</button>\n"
        "  </div>\n"
        "  <div class='lbinfo' id='lbinfo'>\n"
        "    <div class='ihead'>\n"
        "      <h3>File Info</h3>\n"
        "      <button class='iclose' onclick='toggleInfo()'>&#x2715;</button>\n"
        "    </div>\n"
        "    <div class='ibody' id='ibody'></div>\n"
        "  </div>\n"
        "</div>\n"
        "<!-- DIALOG -->\n"
        "<div class='dlgbg' id='dlgbg'>\n"
        "  <div class='dlg'>\n"
        "    <h3 id='dlgtitle'></h3>\n"
        "    <p id='dlgmsg'></p>\n"
        "    <div id='dlgpath' class='dlgpath' style='display:none'></div>\n"
        "    <div id='dlgextra'></div>\n"
        "    <div class='dlgact'>\n"
        "      <button onclick='dlgCancel()'>Cancel</button>\n"
        "      <button id='dlgok'>OK</button>\n"
        "    </div>\n"
        "  </div>\n"
        "</div>\n"
        "<!-- DUPLICATES -->\n"
        "<div class='dupbg' id='dupbg'>\n"
        "  <div class='dupdlg'>\n"
        "    <div class='duphead'>\n"
        "      <h3>Duplicate Files</h3>\n"
        "      <button class='iclose' onclick='closeDupes()'>&#x2715;</button>\n"
        "    </div>\n"
        "    <div class='dupbody' id='dupbody'></div>\n"
        "  </div>\n"
        "</div>\n"
        "<div class='toastwrap' id='tw'></div>\n"
        "<script>\n"
        "var page=0,sortDir='desc',view='grid',activeFolder=null;\n"
        "var results=[],totalCount=0,lbIdx=0,sel=new Set(),dlgCb=null;\n"
        "var lbZoom=1,lbPanX=0,lbPanY=0,lbDragging=false,lbDragSX=0,lbDragSY=0,lbJustDragged=false;\n"
        "var collapsed={};\n"
        "function g(id){return document.getElementById(id);}\n"
        "function dbt(fn){var t;return function(){clearTimeout(t);t=setTimeout(fn,280);}}\n"
        "async function api(path,method,body){\n"
        "  method=method||'GET';\n"
        "  var opts={method:method,headers:{'Content-Type':'application/json'}};\n"
        "  if(body)opts.body=JSON.stringify(body);\n"
        "  var r=await fetch('/api'+path,opts);\n"
        "  return r.json();\n"
        "}\n"
        "function thumbErr(el){\n"
        "  el.parentElement.classList.add('thumbfail');\n"
        "}\n"
        "function esc(s){\n"
        "  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')\n"
        "    .replace(/>/g,'&gt;').replace(/\"/g,'&quot;').replace(/'/g,'&#39;');\n"
        "}\n"
        "function fmtSz(n){\n"
        "  if(!n)return'&#8211;';\n"
        "  var u=['B','KB','MB','GB'],i=0;\n"
        "  while(n>=1024&&i<3){n/=1024;i++;}\n"
        "  return n.toFixed(1)+' '+u[i];\n"
        "}\n"
        "function fmtDur(s){\n"
        "  if(!s)return'';\n"
        "  var m=Math.floor(s/60),h=Math.floor(m/60);\n"
        "  return h?h+':'+String(m%60).padStart(2,'0')+':'+String(Math.floor(s%60)).padStart(2,'0')\n"
        "          :m+':'+String(Math.floor(s%60)).padStart(2,'0');\n"
        "}\n"
        "function toast(msg,type){\n"
        "  var el=document.createElement('div');\n"
        "  el.className='toast '+(type||'ok');el.textContent=msg;\n"
        "  g('tw').appendChild(el);\n"
        "  setTimeout(function(){el.remove();},2800);\n"
        "}\n"
        "function openFilterMenu(){g('filtmenu').style.display='';}\n"
        "function closeFilterMenu(){g('filtmenu').style.display='none';}\n"
        "function toggleFilterMenu(e){\n"
        "  if(e)e.stopPropagation();\n"
        "  var m=g('filtmenu');\n"
        "  m.style.display=m.style.display==='none'?'':'none';\n"
        "}\n"
        "document.addEventListener('click',function(e){\n"
        "  var m=g('filtmenu');\n"
        "  if(m.style.display!=='none'&&!m.contains(e.target)&&e.target.id!=='filterBtn'){\n"
        "    closeFilterMenu();\n"
        "  }\n"
        "});\n"

        "function showSB(name){\n"
        "  ['folders','tags','stats'].forEach(function(n){\n"
        "    g('sb-'+n).style.display=n===name?'':'none';\n"
        "  });\n"
        "  document.querySelectorAll('.stab').forEach(function(el,i){\n"
        "    el.classList.toggle('on',['folders','tags','stats'][i]===name);\n"
        "  });\n"
        "  if(name==='folders')loadTree();\n"
        "  if(name==='tags')loadTags();\n"
        "  if(name==='stats')loadStats();\n"
        "}\n"

        "async function loadMedia(){\n"
        "  var pp=parseInt(g('perPage').value);\n"
        "  var params=new URLSearchParams({\n"
        "    limit:pp,offset:page*pp,\n"
        "    sort_by:g('sortBy').value,sort_dir:sortDir\n"
        "  });\n"
        "  var q=g('q').value.trim();if(q)params.set('query',q);\n"
        "  var ft=g('fType').value;if(ft)params.set('media_type',ft);\n"
        "  var ff=g('fFrom').value;if(ff)params.set('date_from',ff);\n"
        "  var ft2=g('fTo').value;if(ft2)params.set('date_to',ft2);\n"
        "  var fe=g('fExt').value.trim();if(fe)params.set('ext',fe);\n"
        "  var tg=g('fTags').value.trim();if(tg)params.set('tags',tg);\n"
        "  if(g('fDupes').checked)params.set('dupes_only','1');\n"
        "  if(g('fFav').checked)params.set('favorites_only','1');\n"
        "  if(activeFolder!==null)params.set('folder_id',activeFolder);\n"
        "  var data=await api('/search?'+params);\n"
        "  results=data.results;\n"
        "  totalCount=data.total;\n"
        "  g('tcnt').textContent=totalCount+' file'+(totalCount!==1?'s':'');\n"
        "  renderMedia();\n"
        "  renderPager();\n"
        "  saveUIState();\n"
        "}\n"
        "function saveUIState(){\n"
        "  try{\n"
        "    var st={\n"
        "      sortBy:g('sortBy').value,sortDir:sortDir,view:view,perPage:g('perPage').value,\n"
        "      q:g('q').value,\n"
        "      fType:g('fType').value,fFrom:g('fFrom').value,fTo:g('fTo').value,\n"
        "      fExt:g('fExt').value,fTags:g('fTags').value,fDupes:g('fDupes').checked,\n"
        "      fFav:g('fFav').checked,activeFolder:activeFolder\n"
        "    };\n"
        "    localStorage.setItem('mlv4_state',JSON.stringify(st));\n"
        "  }catch(e){}\n"
        "}\n"
        "function restoreUIState(){\n"
        "  try{\n"
        "    var raw=localStorage.getItem('mlv4_state');\n"
        "    if(!raw)return;\n"
        "    var st=JSON.parse(raw);\n"
        "    if(st.sortBy)g('sortBy').value=st.sortBy;\n"
        "    if(st.sortDir){sortDir=st.sortDir;g('dirBtn').innerHTML=sortDir==='desc'?'&#8595;':'&#8593;';}\n"
        "    if(st.view){view=st.view;g('vg').classList.toggle('on',view==='grid');"
        "g('vl').classList.toggle('on',view==='list');}\n"
        "    if(st.perPage)g('perPage').value=st.perPage;\n"
        "    if(st.q)g('q').value=st.q;\n"
        "    if(st.fType!==undefined)g('fType').value=st.fType;\n"
        "    if(st.fFrom)g('fFrom').value=st.fFrom;\n"
        "    if(st.fTo)g('fTo').value=st.fTo;\n"
        "    if(st.fExt)g('fExt').value=st.fExt;\n"
        "    if(st.fTags)g('fTags').value=st.fTags;\n"
        "    if(st.fDupes)g('fDupes').checked=true;\n"
        "    if(st.fFav)g('fFav').checked=true;\n"
        "    if(st.activeFolder!=null)activeFolder=st.activeFolder;\n"
        "  }catch(e){}\n"
        "}\n"
        "function renderMedia(){\n"
        "  var scr=g('mscroll');\n"
        "  if(!results.length){\n"
        "    scr.innerHTML='<div class=\"empty\"><div class=\"eico\">&#128269;</div><p>No files found.</p></div>';\n"
        "    return;\n"
        "  }\n"
        "  if(view==='grid'){\n"
        "    var grid=g('mgrid');\n"
        "    if(!grid){grid=document.createElement('div');grid.id='mgrid';}\n"
        "    grid.className='grid';\n"
        "    grid.innerHTML=results.map(function(m,i){return cardHTML(m,i);}).join('');\n"
        "    scr.innerHTML='';\n"
        "    scr.appendChild(grid);\n"
        "  } else {\n"
        "    scr.innerHTML=listHTML();\n"
        "  }\n"
        "}\n"
        "function favBtnHTML(m,i,cls){\n"
        "  var on=m.is_favorite?' on':'';\n"
        "  return '<button class=\"favbtn '+cls+on+'\" onclick=\"toggleFav(event,'+m.id+','+i+')\" "
        "title=\"Favorite\">'+(m.is_favorite?'&#9733;':'&#9734;')+'</button>';\n"
        "}\n"
        "function cardHTML(m,i){\n"
        "  var date=(m.date_taken||m.date_added||'').slice(0,10);\n"
        "  var dup=m.is_dup?'<div class=\"pill dpill\">DUP</div>':'';\n"
        "  var dur=m.duration?'<div class=\"pill vrpill\">'+fmtDur(m.duration)+'</div>':'';\n"
        "  var tags=m.tags?m.tags.split(', ').map(function(t){return'<span class=\"tchip\">'+esc(t)+'</span>';}).join(''):'';\n"
        "  var sc=sel.has(m.id)?' sel':'';\n"
        "  var ico=m.media_type==='image'?'&#128444;':'&#127909;';\n"
        "  var th='<img src=\"/thumb/'+m.id+'\" loading=\"lazy\" onerror=\"thumbErr(this)\" />"
        "<div class=\"ico\">'+ico+'</div>';\n"
        "  return '<div class=\"card'+sc+'\" onclick=\"cc(event,'+i+')\" data-id=\"'+m.id+'\">'\n"
        "    +'<div class=\"thumb\">'+th\n"
        "    +'<div class=\"pill tpill\">.'+esc(m.extension)+'</div>'\n"
        "    +dup+dur+favBtnHTML(m,i,'cardfav')+'</div>'\n"
        "    +'<div class=\"cfoot\">'\n"
        "    +'<div class=\"cname\" title=\"'+esc(m.file_name)+'\">'+esc(m.file_name)+'</div>'\n"
        "    +'<div class=\"csub\">'+fmtSz(m.file_size)+(date?' &middot; '+date:'')+'</div>'\n"
        "    +(tags?'<div class=\"ctags\">'+tags+'</div>':'')\n"
        "    +'</div></div>';\n"
        "}\n"
        "function listHTML(){\n"
        "  var hdr='<div class=\"lrow\" style=\"cursor:default;pointer-events:none;opacity:.4;font-size:10px\">'\n"
        "    +'<div></div><div>Name</div><div>Type</div><div>Size</div><div>Date</div><div></div></div>';\n"
        "  var rows=results.map(function(m,i){\n"
        "    var date=(m.date_taken||m.date_added||'').slice(0,10);\n"
        "    var ico=m.media_type==='image'?'&#128444;':'&#127909;';\n"
        "    var th='<img src=\"/thumb/'+m.id+'\" loading=\"lazy\" onerror=\"thumbErr(this)\" />"
        "<div class=\"ico\">'+ico+'</div>';\n"
        "    var sc=sel.has(m.id)?' sel':'';\n"
        "    return '<div class=\"lrow'+sc+'\" onclick=\"cc(event,'+i+')\" data-id=\"'+m.id+'\">'\n"
        "      +'<div class=\"lthumb\">'+th+'</div>'\n"
        "      +'<div><div class=\"lname\" title=\"'+esc(m.file_path)+'\">'+esc(m.file_name)+'</div>'\n"
        "      +'<div class=\"lsub\">'+(m.tags||'')+'</div></div>'\n"
        "      +'<div class=\"lcell\">'+m.media_type+'</div>'\n"
        "      +'<div class=\"lcell\">'+fmtSz(m.file_size)+'</div>'\n"
        "      +'<div class=\"lcell\">'+date+'</div>'\n"
        "      +favBtnHTML(m,i,'listfav')\n"
        "      +'</div>';\n"
        "  }).join('');\n"
        "  return '<div class=\"list\">'+hdr+rows+'</div>';\n"
        "}\n"
        "async function toggleFav(e,mid,i){\n"
        "  e.stopPropagation();\n"
        "  var m=results[i];if(!m)return;\n"
        "  var newVal=!m.is_favorite;\n"
        "  m.is_favorite=newVal?1:0;\n"
        "  renderMedia();\n"
        "  await api('/media/'+mid+'/favorite','PUT',{favorite:newVal});\n"
        "}\n"
        "function cc(e,i){\n"
        "  if(e.shiftKey||e.ctrlKey||e.metaKey){\n"
        "    var id=results[i].id;\n"
        "    if(sel.has(id))sel.delete(id);else sel.add(id);\n"
        "    updBulk();renderMedia();\n"
        "  } else {\n"
        "    openLb(i);\n"
        "  }\n"
        "}\n"
        "function renderPager(){\n"
        "  var pp=parseInt(g('perPage').value);\n"
        "  var pages=Math.ceil(totalCount/pp);\n"
        "  if(pages<=1){g('pager').innerHTML='';return;}\n"
        "  var html='<span class=\"pinfo\">Page '+(page+1)+' of '+pages+'</span>';\n"
        "  if(page>0)html+='<button onclick=\"goPage('+(page-1)+')\">&#8592;</button>';\n"
        "  var s=Math.max(0,page-2),e=Math.min(pages-1,page+2);\n"
        "  for(var p=s;p<=e;p++){\n"
        "    html+='<button class=\"'+(p===page?'on':'')+'\""
        " onclick=\"goPage('+p+')\">'+(p+1)+'</button>';\n"
        "  }\n"
        "  if(page<pages-1)html+='<button onclick=\"goPage('+(page+1)+')\">&#8594;</button>';\n"
        "  g('pager').innerHTML=html;\n"
        "}\n"
        "function goPage(p){page=p;loadMedia();}\n"
        "function toggleDir(){\n"
        "  sortDir=sortDir==='desc'?'asc':'desc';\n"
        "  g('dirBtn').innerHTML=sortDir==='desc'?'&#8595;':'&#8593;';\n"
        "  goPage(0);\n"
        "}\n"
        "function setView(v){\n"
        "  view=v;\n"
        "  g('vg').classList.toggle('on',v==='grid');\n"
        "  g('vl').classList.toggle('on',v==='list');\n"
        "  renderMedia();\n"
        "  saveUIState();\n"
        "}\n"
        "function clearFilters(){\n"
        "  ['fType','fFrom','fTo','fExt','fTags'].forEach(function(id){g(id).value='';});\n"
        "  g('fDupes').checked=false;\n"
        "  g('fFav').checked=false;\n"
        "  g('q').value='';\n"
        "  activeFolder=null;\n"
        "  document.querySelectorAll('.ftrow').forEach(function(el){el.classList.remove('on');});\n"
        "  goPage(0);\n"
        "}\n"

        "function updBulk(){\n"
        "  var bar=g('bulkbar');\n"
        "  if(sel.size>0){bar.classList.add('show');g('bkcnt').textContent=sel.size+' selected';}\n"
        "  else bar.classList.remove('show');\n"
        "}\n"
        "function clearSel(){sel.clear();updBulk();renderMedia();}\n"
        "function bulkTag(){\n"
        "  showDlg('Tag '+sel.size+' files',\n"
        "    'Enter tags to add (space or comma separated).','','',\n"
        "    '<input id=\"di1\" placeholder=\"tag1, tag2\" style=\"margin-bottom:9px\" />'\n"
        "    +'<label style=\"display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer\">'\n"
        "    +'<input type=\"checkbox\" id=\"di_rm\" /> Remove these tags instead of adding</label>',\n"
        "    'Apply',false,\n"
        "    async function(){\n"
        "      var raw=g('di1').value.trim();if(!raw)return;\n"
        "      var tags=raw.split(/[,\\s]+/).filter(Boolean);\n"
        "      var action=g('di_rm').checked?'remove':'add';\n"
        "      var r=await api('/bulk/tags','POST',{media_ids:Array.from(sel),tags:tags,action:action});\n"
        "      toast((action==='add'?'Tagged ':'Untagged ')+(r.count||sel.size)+' file'+(sel.size!==1?'s':''),'ok');\n"
        "      loadMedia();loadTags();\n"
        "    });\n"
        "}\n"
        "function bulkMove(){\n"
        "  showDlg('Move '+sel.size+' files','Enter destination folder path.','','',\n"
        "    '<input id=\"di1\" placeholder=\"Destination path\" />','Move',false,\n"
        "    async function(){\n"
        "      var dest=g('di1').value.trim();if(!dest)return;\n"
        "      var ok=0,fail=0;\n"
        "      for(var id of sel){\n"
        "        var r=await api('/disk/move','POST',{media_id:id,dest_folder:dest});\n"
        "        r.ok?ok++:fail++;\n"
        "      }\n"
        "      toast('Moved '+ok+' file'+(ok!==1?'s':'')+(fail?', '+fail+' failed':''),fail?'err':'ok');\n"
        "      clearSel();loadMedia();\n"
        "    });\n"
        "}\n"
        "function bulkDelete(){\n"
        "  showDlg('Delete '+sel.size+' files',\n"
        "    'Files will be sent to the Recycle Bin and removed from the database.','','',\n"
        "    '<label style=\"display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer\">'\n"
        "    +'<input type=\"checkbox\" id=\"di_perm\" /> Delete permanently instead</label>',\n"
        "    'Delete',true,\n"
        "    async function(){\n"
        "      var perm=g('di_perm')&&g('di_perm').checked;\n"
        "      var ok=0,fail=0;\n"
        "      for(var id of sel){\n"
        "        var r=await api('/disk/delete','POST',{media_id:id,permanent:perm});\n"
        "        r.ok?ok++:fail++;\n"
        "      }\n"
        "      toast('Deleted '+ok+(fail?', '+fail+' failed':''),fail?'err':'ok');\n"
        "      clearSel();loadMedia();loadStats();\n"
        "    });\n"
        "}\n"

        "function openLb(i){\n"
        "  lbIdx=i;\n"
        "  g('lbox').classList.add('open');\n"
        "  renderLb();\n"
        "}\n"
        "function renderLb(){\n"
        "  var m=results[lbIdx];if(!m)return;\n"
        "  g('lbtitle').textContent=m.file_name;\n"
        "  var main=g('lbmedia');\n"
        "  lbZoom=1;lbPanX=0;lbPanY=0;lbDragging=false;lbJustDragged=false;\n"
        "  if(m.media_type==='image'){\n"
        "    main.innerHTML='<img src=\"/file/'+m.id+'\" alt=\"'+esc(m.file_name)+'\" "
        "onmousedown=\"lbDragStart(event)\" ondblclick=\"lbZoomReset()\" />';\n"
        "    g('lbzoom').style.display='flex';\n"
        "    updateLbZoom();\n"
        "  } else {\n"
        "    var mimes={mp4:'video/mp4',mov:'video/quicktime',avi:'video/x-msvideo',\n"
        "      mkv:'video/x-matroska',webm:'video/webm',m4v:'video/mp4',wmv:'video/x-ms-wmv'};\n"
        "    var mime=mimes[m.extension.toLowerCase()]||'video/mp4';\n"
        "    main.innerHTML='<video controls preload=\"metadata\" poster=\"/thumb/'+m.id+'\">'+'<source src=\"/file/'+m.id+'\" type=\"'+mime+'\">'+'</video>';\n"
        "    g('lbzoom').style.display='none';\n"
        "  }\n"
        "  if(g('lbinfo').classList.contains('open'))renderInfo();\n"
        "  updateLbFavBtn();\n"
        "}\n"
        "function updateLbFavBtn(){\n"
        "  var m=results[lbIdx];var btn=g('lbfav');\n"
        "  if(!m||!btn)return;\n"
        "  btn.innerHTML=(m.is_favorite?'&#9733;':'&#9734;')+' Favorite';\n"
        "  btn.classList.toggle('active',!!m.is_favorite);\n"
        "}\n"
        "async function toggleFavCurrent(){\n"
        "  var m=results[lbIdx];if(!m)return;\n"
        "  var newVal=!m.is_favorite;\n"
        "  m.is_favorite=newVal?1:0;\n"
        "  updateLbFavBtn();\n"
        "  await api('/media/'+m.id+'/favorite','PUT',{favorite:newVal});\n"
        "}\n"
        "var slideTimer=null;\n"
        "function toggleSlideshow(){\n"
        "  if(slideTimer)stopSlideshow();else startSlideshow();\n"
        "}\n"
        "function startSlideshow(){\n"
        "  if(slideTimer)clearInterval(slideTimer);\n"
        "  slideTimer=setInterval(function(){\n"
        "    var m=results[lbIdx];\n"
        "    if(m&&m.media_type==='video')return;\n"
        "    lbNav(1);\n"
        "  },4000);\n"
        "  var b=g('lbslide');\n"
        "  if(b){b.innerHTML='&#10074;&#10074; Slideshow';b.classList.add('active');}\n"
        "}\n"
        "function stopSlideshow(){\n"
        "  if(slideTimer){clearInterval(slideTimer);slideTimer=null;}\n"
        "  var b=g('lbslide');\n"
        "  if(b){b.innerHTML='&#9654; Slideshow';b.classList.remove('active');}\n"
        "}\n"
        "function updateLbZoom(){\n"
        "  var img=g('lbmedia').querySelector('img');\n"
        "  if(!img)return;\n"
        "  img.style.transform='translate('+lbPanX+'px,'+lbPanY+'px) scale('+lbZoom+')';\n"
        "  img.style.cursor=lbZoom>1?'grab':'zoom-in';\n"
        "  var pct=g('lbzoompct');if(pct)pct.textContent=Math.round(lbZoom*100)+'%';\n"
        "}\n"
        "function lbZoomBy(delta){\n"
        "  lbZoom=Math.min(6,Math.max(1,+(lbZoom+delta).toFixed(2)));\n"
        "  if(lbZoom<=1){lbZoom=1;lbPanX=0;lbPanY=0;}\n"
        "  updateLbZoom();\n"
        "}\n"
        "function lbZoomReset(){lbZoom=1;lbPanX=0;lbPanY=0;updateLbZoom();}\n"
        # The <img> element's own box fills its container (so object-fit:contain can
        # letterbox/pillarbox it correctly), which means the box is usually bigger
        # than the actual drawn picture. These two helpers figure out where the
        # picture pixels REALLY are on screen (post zoom/pan transform) so clicks
        # and wheel-zoom in the empty letterbox margin behave as "outside the photo".
        "function lbPicRect(img){\n"
        "  var r=img.getBoundingClientRect();\n"
        "  var nw=img.naturalWidth,nh=img.naturalHeight;\n"
        "  if(!nw||!nh)return r;\n"
        "  var boxRatio=r.width/r.height,picRatio=nw/nh,w,h;\n"
        "  if(picRatio>boxRatio){w=r.width;h=r.width/picRatio;}\n"
        "  else{h=r.height;w=r.height*picRatio;}\n"
        "  var x=r.left+(r.width-w)/2,y=r.top+(r.height-h)/2;\n"
        "  return {left:x,top:y,right:x+w,bottom:y+h};\n"
        "}\n"
        "function lbOnPic(img,x,y){\n"
        "  var pr=lbPicRect(img);\n"
        "  return x>=pr.left&&x<=pr.right&&y>=pr.top&&y<=pr.bottom;\n"
        "}\n"
        "function lbWheel(e){\n"
        "  var img=g('lbmedia').querySelector('img');\n"
        "  if(!img||!lbOnPic(img,e.clientX,e.clientY))return;\n"
        "  e.preventDefault();\n"
        "  var factor=e.deltaY<0?1.15:1/1.15;\n"
        "  lbZoom=Math.min(6,Math.max(1,lbZoom*factor));\n"
        "  if(lbZoom<=1.001){lbZoom=1;lbPanX=0;lbPanY=0;}\n"
        "  updateLbZoom();\n"
        "}\n"
        "function lbDragStart(e){\n"
        "  if(lbZoom<=1||!lbOnPic(e.target,e.clientX,e.clientY))return;\n"
        "  e.preventDefault();\n"
        "  lbDragging=true;\n"
        "  lbDragSX=e.clientX-lbPanX;\n"
        "  lbDragSY=e.clientY-lbPanY;\n"
        "}\n"
        "function lbMainClick(e){\n"
        "  if(lbJustDragged){lbJustDragged=false;return;}\n"
        "  var img=g('lbmedia').querySelector('img');\n"
        "  if(img){if(!lbOnPic(img,e.clientX,e.clientY))closeLb();return;}\n"
        "  if(e.target.id==='lbmain'||e.target.id==='lbmedia')closeLb();\n"
        "}\n"
        "function lbNav(dir){\n"
        "  var vid=g('lbmedia').querySelector('video');\n"
        "  if(vid){vid.pause();vid.src='';vid.load();vid.remove();}\n"
        "  g('lbmedia').innerHTML='';\n"
        "  lbIdx=(lbIdx+dir+results.length)%results.length;\n"
        "  renderLb();\n"
        "}\n"
        "function closeLb(){\n"
        "  stopSlideshow();\n"
        "  var vid=g('lbmedia').querySelector('video');\n"
        "  if(vid){vid.pause();vid.src='';vid.load();vid.remove();}\n"
        "  g('lbmedia').innerHTML='';\n"
        "  g('lbox').classList.remove('open');\n"
        "  g('lbinfo').classList.remove('open');\n"
        "  lbZoom=1;lbPanX=0;lbPanY=0;lbDragging=false;lbJustDragged=false;\n"
        "}\n"
        "function toggleInfo(){\n"
        "  var p=g('lbinfo');\n"
        "  p.classList.toggle('open');\n"
        "  if(p.classList.contains('open'))renderInfo();\n"
        "}\n"
        "async function renderInfo(){\n"
        "  var m=results[lbIdx];if(!m)return;\n"
        "  var data=await api('/media/'+m.id);\n"
        "  var af=await api('/folders?tree=1');\n"
        "  var aset=new Set((data.folders||[]).map(function(f){return f.id;}));\n"
        "  var meta=[\n"
        "    ['Name',data.file_name],['Type',data.media_type+' (.'+data.extension+')'],\n"
        "    ['Size',fmtSz(data.file_size)],\n"
        "    ['Dimensions',data.width?data.width+' x '+data.height+' px':'&#8211;'],\n"
        "    ['Date taken',data.date_taken?data.date_taken.slice(0,10):'&#8211;'],\n"
        "    ['Date added',data.date_added?data.date_added.slice(0,10):'&#8211;'],\n"
        "    ['Duration',data.duration?fmtDur(data.duration):'&#8211;'],\n"
        "    ['Path',data.file_path]\n"
        "  ];\n"
        "  var mrows=meta.map(function(kv){\n"
        "    return '<div class=\"mrow\"><span>'+kv[0]+'</span><span title=\"'+esc(String(kv[1]))+'\">'+esc(String(kv[1]))+'</span></div>';\n"
        "  }).join('');\n"
        "  var extra={};try{extra=JSON.parse(data.extra_meta||'{}');}catch(e){}\n"
        "  var gpsRow='';\n"
        "  if(extra.gps){\n"
        "    var glat=extra.gps.lat,glon=extra.gps.lon;\n"
        "    var mapUrl='https://www.openstreetmap.org/?mlat='+glat+'&mlon='+glon+'#map=16/'+glat+'/'+glon;\n"
        "    gpsRow='<div class=\"mrow\"><span>Location</span><span>'\n"
        "      +'<a href=\"'+mapUrl+'\" target=\"_blank\" rel=\"noopener\" style=\"color:var(--ac2)\">'\n"
        "      +glat.toFixed(4)+', '+glon.toFixed(4)+'</a></span></div>';\n"
        "  }\n"
        "  var tpills=(data.tags||[]).map(function(t){\n"
        "    return '<div class=\"tpill2\">'+esc(t)\n"
        "      +'<button onclick=\"rmTag('+data.id+',\\''+esc(t)+'\\')\" title=\"Remove\">&times;</button></div>';\n"
        "  }).join('');\n"
        "  var fchecks=af.map(function(f){\n"
        "    var ind='padding-left:'+(f.depth*12+2)+'px';\n"
        "    var chk=aset.has(f.id)?'checked':'';\n"
        "    return '<label style=\"'+ind+';display:flex;align-items:center;gap:6px;font-size:11px;padding:3px 0;cursor:pointer\">'\n"
        "      +'<input type=\"checkbox\" '+chk+' onchange=\"togFolder('+data.id+','+f.id+',this.checked)\" />'\n"
        "      +'&#128193; '+esc(f.name)+'</label>';\n"
        "  }).join('');\n"
        "  g('ibody').innerHTML=\n"
        "    '<div class=\"isec\"><h4>Metadata</h4>'+mrows+gpsRow+'</div>'\n"
        "    +'<div class=\"isec\"><h4>Tags</h4>'\n"
        "    +'<div class=\"taglist\">'+(tpills||'<span style=\"color:var(--mu);font-size:11px\">No tags</span>')+'</div>'\n"
        "    +'<div class=\"tadd\"><input id=\"tinp\" placeholder=\"Add tag&#8230;\"'\n"
        "    +' onkeydown=\"if(event.key===\\'Enter\\')addTag('+data.id+')\" />'\n"
        "    +'<button onclick=\"addTag('+data.id+')\">Add</button></div></div>'\n"
        "    +'<div class=\"isec\"><h4>Description</h4>'\n"
        "    +'<textarea class=\"darea\" id=\"darea\" rows=\"3\">'+esc(data.description||'')+'</textarea>'\n"
        "    +'<button class=\"dbtn\" style=\"margin-top:5px\" onclick=\"saveDesc('+data.id+')\">Save</button></div>'\n"
        "    +'<div class=\"isec\"><h4>Folders</h4><div class=\"fcheck\">'\n"
        "    +(af.length?fchecks:'<span style=\"color:var(--mu);font-size:11px\">No folders yet</span>')\n"
        "    +'</div></div>'\n"
        "    +'<div class=\"isec dsk\"><h4>&#9888; Disk Operations</h4>'\n"
        "    +'<button class=\"dbtn\" onclick=\"dskRename('+data.id+',\\''+esc(data.file_name)+'\\')\">✎ Rename file on disk</button>'\n"
        "    +'<button class=\"dbtn\" onclick=\"dskMove('+data.id+',\\''+esc(data.file_path)+'\\')\">📁 Move file on disk</button>'\n"
        "    +'<button class=\"dbtn red\" onclick=\"dskDel('+data.id+',false,\\''+esc(data.file_path)+'\\')\">🗑 Recycle Bin</button>'\n"
        "    +'<button class=\"dbtn red\" onclick=\"dskDel('+data.id+',true,\\''+esc(data.file_path)+'\\')\">🗑 Delete permanently</button>'\n"
        "    +'</div>';\n"
        "}\n"

        "async function addTag(mid){\n"
        "  var inp=g('tinp');var t=inp.value.trim().toLowerCase();if(!t)return;\n"
        "  await api('/media/'+mid+'/tags','POST',{tags:[t]});\n"
        "  inp.value='';toast('Tag added','ok');renderInfo();\n"
        "}\n"
        "async function rmTag(mid,tag){\n"
        "  await api('/media/'+mid+'/tags','DELETE',{tags:[tag]});\n"
        "  toast('Tag removed','ok');renderInfo();\n"
        "}\n"
        "async function saveDesc(mid){\n"
        "  await api('/media/'+mid+'/description','PUT',{description:g('darea').value});\n"
        "  toast('Saved','ok');\n"
        "}\n"
        "async function togFolder(mid,fid,add){\n"
        "  if(add)await api('/folders/'+fid+'/media','POST',{media_ids:[mid]});\n"
        "  else await api('/folders/'+fid+'/media','DELETE',{media_ids:[mid]});\n"
        "  toast(add?'Added to folder':'Removed from folder','ok');\n"
        "}\n"

        "async function loadTree(){\n"
        "  var flat=await api('/folders?tree=1');\n"
        "  flat.forEach(function(f){\n"
        "    if(f.child_count>0&&!(f.id in collapsed))collapsed[f.id]=true;\n"
        "  });\n"
        "  renderTree(flat);\n"
        "}\n"
        "function renderTree(flat){\n"
        "  var el=g('ftree');\n"
        "  if(!flat.length){\n"
        "    el.innerHTML='<div style=\"color:var(--mu);font-size:11px;padding:6px\">No folders yet.</div>';\n"
        "    return;\n"
        "  }\n"
        "  el.innerHTML=flat.map(function(f){\n"
        "    var hide=f.depth>0&&isAncCollapsed(f,flat)?'display:none;':'';\n"
        "    var ind=f.depth*14;\n"
        "    var has=f.child_count>0;\n"
        "    var col=collapsed[f.id];\n"
        "    var tgl=has\n"
        "      ?'<span class=\"tgl\" onclick=\"togCol('+f.id+',event)\">'+(col?'&#9654;':'&#9660;')+'</span>'\n"
        "      :'<span class=\"tgl\"></span>';\n"
        "    var on=activeFolder===f.id?' on':'';\n"
        "    return '<div class=\"ftrow'+on+'\" data-fid=\"'+f.id+'\" data-depth=\"'+f.depth+'\" style=\"padding-left:'+(ind+6)+'px;'+hide+'\">'\n"
        "      +tgl\n"
        "      +'<span style=\"flex-shrink:0\">&#128193;</span>'\n"
        "      +'<span class=\"fn\" onclick=\"selFolder('+f.id+')\" title=\"'+esc(f.name)+'\">'+esc(f.name)+'</span>'\n"
        "      +'<span class=\"fc\">'+f.media_count+'</span>'\n"
        "      +'<span class=\"tgl\" onclick=\"renFolder('+f.id+',\\''+esc(f.name)+'\\')\">&#9998;</span>'\n"
        "      +'<span class=\"tgl\" onclick=\"newFolder('+f.id+')\">+</span>'\n"
        "      +'<span class=\"tgl\" onclick=\"delFolder('+f.id+',\\''+esc(f.name)+'\\')\">&#128465;</span>'\n"
        "      +'</div>';\n"
        "  }).join('');\n"
        "}\n"
        "function isAncCollapsed(f,flat){\n"
        "  var byId={};\n"
        "  flat.forEach(function(x){byId[x.id]=x;});\n"
        "  var cur=f;\n"
        "  while(cur.parent_id!=null){\n"
        "    var p=byId[cur.parent_id];\n"
        "    if(!p)break;\n"
        "    if(collapsed[p.id])return true;\n"
        "    cur=p;\n"
        "  }\n"
        "  return false;\n"
        "}\n"
        "function togCol(fid,e){\n"
        "  e.stopPropagation();\n"
        "  collapsed[fid]=!collapsed[fid];\n"
        "  loadTree();\n"
        "}\n"
        "function selFolder(fid){\n"
        "  activeFolder=activeFolder===fid?null:fid;\n"
        "  loadTree();goPage(0);\n"
        "}\n"
        "function newFolder(pid){\n"
        "  showDlg('New folder','Enter a name for the new folder.','','',\n"
        "    '<input id=\"di1\" placeholder=\"Folder name\" />','Create',false,\n"
        "    async function(){\n"
        "      var name=g('di1').value.trim();if(!name)return;\n"
        "      await api('/folders','POST',{name:name,parent_id:pid});\n"
        "      toast('Folder created','ok');loadTree();\n"
        "    });\n"
        "}\n"
        "function renFolder(fid,cur){\n"
        "  showDlg('Rename folder','Enter a new name.','','',\n"
        "    '<input id=\"di1\" value=\"'+esc(cur)+'\" />','Rename',false,\n"
        "    async function(){\n"
        "      var name=g('di1').value.trim();if(!name||name===cur)return;\n"
        "      await api('/folders/'+fid,'PUT',{name:name});\n"
        "      toast('Renamed','ok');loadTree();\n"
        "    });\n"
        "}\n"
        "function delFolder(fid,name){\n"
        "  showDlg('Delete folder','Delete \"'+esc(name)+'\" from the database? Files on disk are not affected.','','','','Delete',true,\n"
        "    async function(){\n"
        "      await api('/folders/'+fid,'DELETE',{});\n"
        "      toast('Deleted','ok');\n"
        "      if(activeFolder===fid){activeFolder=null;goPage(0);}\n"
        "      loadTree();\n"
        "    });\n"
        "}\n"

        "function rescanIngest(){\n"
        "  showDlg('Rescan / Ingest folder',\n"
        "    'Scan a folder on disk and import new or changed media into the library.','','',\n"
        "    '<input id=\"ing_path\" placeholder=\"Folder path, e.g. C:\\\\Users\\\\Desmond\\\\Desktop\\\\Apps\\\\Things\\\\Content\" '\n"
        "    +'style=\"width:100%;margin-bottom:9px\" />'\n"
        "    +'<label style=\"display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer;margin-bottom:5px\">'\n"
        "    +'<input type=\"checkbox\" id=\"ing_mirror\" /> Mirror folder structure into virtual folders</label>'\n"
        "    +'<label style=\"display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer;margin-bottom:5px\">'\n"
        "    +'<input type=\"checkbox\" id=\"ing_force\" /> Force re-scan files already in the library</label>'\n"
        "    +'<label style=\"display:flex;align-items:center;gap:7px;font-size:12px;cursor:pointer\">'\n"
        "    +'<input type=\"checkbox\" id=\"ing_norecursive\" /> This folder only (skip subfolders)</label>',\n"
        "    'Ingest',false,\n"
        "    async function(){\n"
        "      var folder=g('ing_path').value.trim();if(!folder)return;\n"
        "      var r=await api('/ingest','POST',{folder:folder,\n"
        "        mirror:g('ing_mirror').checked, force:g('ing_force').checked,\n"
        "        recursive:!g('ing_norecursive').checked});\n"
        "      if(r.error){toast(r.error,'err');return;}\n"
        "      pollIngest();\n"
        "    });\n"
        "}\n"
        "var ingPollTimer=null;\n"
        "function pollIngest(){\n"
        "  var pill=g('ingpill');\n"
        "  pill.style.display='';\n"
        "  pill.textContent='Ingesting\\u2026';\n"
        "  if(ingPollTimer)clearInterval(ingPollTimer);\n"
        "  ingPollTimer=setInterval(async function(){\n"
        "    var s=await api('/ingest_status');\n"
        "    if(s.running){\n"
        "      var c=s.counts||{};\n"
        "      pill.textContent='Ingesting '+s.current+'/'+(s.total||'?')\n"
        "        +' (added '+(c.added||0)+', updated '+(c.updated||0)+', skipped '+(c.skipped||0)\n"
        "        +(c.errors?', '+c.errors+' errors':'')+')';\n"
        "    } else {\n"
        "      clearInterval(ingPollTimer);ingPollTimer=null;\n"
        "      pill.style.display='none';\n"
        "      if(s.error){\n"
        "        toast('Ingest failed: '+s.error,'err');\n"
        "      } else {\n"
        "        var c=s.counts||{};\n"
        "        toast('Ingest done: '+(c.added||0)+' added, '+(c.updated||0)+' updated, '\n"
        "          +(c.skipped||0)+' skipped'+(c.errors?', '+c.errors+' errors':''),\n"
        "          c.errors?'err':'ok');\n"
        "      }\n"
        "      loadMedia();loadStats();loadTree();\n"
        "    }\n"
        "  },700);\n"
        "}\n"

        "async function loadTags(){\n"
        "  var tags=await api('/tags');\n"
        "  g('tagcloud').innerHTML=tags.map(function(t){\n"
        "    return '<span class=\"tchip\" onclick=\"ftag(\\''+esc(t.tag)+'\\')\">'+esc(t.tag)\n"
        "      +' <small style=\"opacity:.6\">'+t.cnt+'</small></span>';\n"
        "  }).join('')||\n"
        "  '<span style=\"color:var(--mu);font-size:11px\">No tags yet.</span>';\n"
        "}\n"
        "function ftag(tag){\n"
        "  g('fTags').value=tag;openFilterMenu();goPage(0);\n"
        "}\n"

        "async function loadStats(){\n"
        "  var s=await api('/stats');\n"
        "  g('statpanel').innerHTML=\n"
        "    '<div class=\"srow\"><span>Total files</span><span>'+s.total+'</span></div>'\n"
        "    +'<div class=\"srow\"><span>Images</span><span>'+s.images+'</span></div>'\n"
        "    +'<div class=\"srow\"><span>Videos</span><span>'+s.videos+'</span></div>'\n"
        "    +'<div class=\"srow\"><span>Total size</span><span>'+fmtSz(s.total_size)+'</span></div>'\n"
        "    +'<div class=\"srow\"><span>Tags</span><span>'+s.tags+'</span></div>'\n"
        "    +'<div class=\"srow\"><span>Folders</span><span>'+s.folders+'</span></div>'\n"
        "    +'<div class=\"srow\"><span>Duplicate groups</span><span>'+s.dup_groups+'</span></div>'\n"
        "    +'<button class=\"fbtn\" style=\"margin-top:10px\" onclick=\"openDupes()\">Review Duplicates'\n"
        "    +(s.dup_groups?' ('+s.dup_groups+')':'')+'</button>';\n"
        "}\n"

        "async function openDupes(){\n"
        "  g('dupbg').classList.add('open');\n"
        "  await loadDupes();\n"
        "}\n"
        "function closeDupes(){g('dupbg').classList.remove('open');}\n"
        "async function loadDupes(){\n"
        "  var data=await api('/duplicates');\n"
        "  var groups=data.groups||[];\n"
        "  window._dupGroups=groups;\n"
        "  if(!groups.length){\n"
        "    g('dupbody').innerHTML='<div class=\"empty\"><div class=\"eico\">&#10003;</div>"
        "<p>No duplicates found.</p></div>';\n"
        "    return;\n"
        "  }\n"
        "  g('dupbody').innerHTML=groups.map(function(grp,gi){\n"
        "    var items=grp.items.map(function(it){\n"
        "      return '<div class=\"dupitem\">'\n"
        "        +'<img src=\"/thumb/'+it.id+'\" loading=\"lazy\" onerror=\"this.style.opacity=0.2\" />'\n"
        "        +'<div class=\"di-meta\">'\n"
        "        +'<div class=\"di-path\" title=\"'+esc(it.file_path)+'\">'+esc(it.file_name)+'</div>'\n"
        "        +'<div class=\"di-size\">'+fmtSz(it.file_size)+'</div>'\n"
        "        +'<button class=\"keepbtn\" onclick=\"keepDup('+gi+','+it.id+')\">Keep this</button>'\n"
        "        +'<button class=\"delbtn\" onclick=\"delDupItem('+it.id+')\">Delete</button>'\n"
        "        +'</div></div>';\n"
        "    }).join('');\n"
        "    return '<div class=\"dupgroup\"><div class=\"dupgtitle\">'+grp.items.length\n"
        "      +' copies &middot; '+fmtSz(grp.items[0].file_size)+' each</div>'\n"
        "      +'<div class=\"dupitems\">'+items+'</div></div>';\n"
        "  }).join('');\n"
        "}\n"
        "function keepDup(gi,keepId){\n"
        "  var grp=window._dupGroups[gi];\n"
        "  var others=grp.items.filter(function(it){return it.id!==keepId;});\n"
        "  showDlg('Keep 1, delete '+others.length,\n"
        "    'The other '+others.length+' cop'+(others.length!==1?'ies':'y')\n"
        "    +' will be sent to the Recycle Bin.','','','',\n"
        "    'Keep & Delete Rest',true,\n"
        "    async function(){\n"
        "      var ok=0,fail=0;\n"
        "      for(var it of others){\n"
        "        var r=await api('/disk/delete','POST',{media_id:it.id,permanent:false});\n"
        "        r.ok?ok++:fail++;\n"
        "      }\n"
        "      toast('Kept 1, removed '+ok+(fail?', '+fail+' failed':''),fail?'err':'ok');\n"
        "      loadDupes();loadMedia();loadStats();\n"
        "    });\n"
        "}\n"
        "function delDupItem(mid){\n"
        "  showDlg('Delete file','Send this file to the Recycle Bin?','','','','Delete',true,\n"
        "    async function(){\n"
        "      var r=await api('/disk/delete','POST',{media_id:mid,permanent:false});\n"
        "      if(r.error){toast(r.error,'err');return;}\n"
        "      toast('Sent to Recycle Bin','ok');\n"
        "      loadDupes();loadMedia();loadStats();\n"
        "    });\n"
        "}\n"

        "function dskRename(mid,cur){\n"
        "  showDlg('Rename file on disk','Enter a new filename.',cur,'',\n"
        "    '<input id=\"di1\" value=\"'+esc(cur)+'\" />','Rename',false,\n"
        "    async function(){\n"
        "      var n=g('di1').value.trim();if(!n||n===cur)return;\n"
        "      var r=await api('/disk/rename','POST',{media_id:mid,new_name:n});\n"
        "      if(r.error){toast(r.error,'err');return;}\n"
        "      toast('Renamed','ok');loadMedia();renderInfo();\n"
        "    });\n"
        "}\n"
        "function dskMove(mid,cur){\n"
        "  showDlg('Move file on disk','Enter destination folder path.',cur,'',\n"
        "    '<input id=\"di1\" placeholder=\"Destination path\" />','Move',false,\n"
        "    async function(){\n"
        "      var dest=g('di1').value.trim();if(!dest)return;\n"
        "      var r=await api('/disk/move','POST',{media_id:mid,dest_folder:dest});\n"
        "      if(r.error){toast(r.error,'err');return;}\n"
        "      toast('Moved','ok');loadMedia();renderInfo();\n"
        "    });\n"
        "}\n"
        "function dskDel(mid,permanent,cur){\n"
        "  var lbl=permanent?'Delete permanently':'Send to Recycle Bin';\n"
        "  var msg=permanent\n"
        "    ?'This PERMANENTLY deletes the file from your hard drive. This cannot be undone.'\n"
        "    :'The file will be sent to your Recycle Bin. You can recover it from there.';\n"
        "  showDlg(lbl,msg,cur,'','',lbl,true,\n"
        "    async function(){\n"
        "      var r=await api('/disk/delete','POST',{media_id:mid,permanent:permanent});\n"
        "      if(r.error){toast(r.error,'err');return;}\n"
        "      toast(permanent?'Permanently deleted':'Sent to Recycle Bin','ok');\n"
        "      closeLb();loadMedia();loadStats();\n"
        "    });\n"
        "}\n"

        "function showDlg(title,msg,path,placeholder,extra,okLabel,danger,cb){\n"
        "  g('dlgtitle').textContent=title;\n"
        "  g('dlgmsg').textContent=msg;\n"
        "  var pe=g('dlgpath');\n"
        "  if(path){pe.textContent=path;pe.style.display='';}else{pe.style.display='none';}\n"
        "  g('dlgextra').innerHTML=extra;\n"
        "  var ok=g('dlgok');\n"
        "  ok.textContent=okLabel||'OK';\n"
        "  ok.className=danger?'danger':'ok';\n"
        "  dlgCb=cb;\n"
        "  g('dlgbg').classList.add('open');\n"
        "  var inp=g('dlgextra').querySelector('input');\n"
        "  if(inp){inp.focus();inp.select();}\n"
        "}\n"
        "function dlgCancel(){g('dlgbg').classList.remove('open');dlgCb=null;}\n"
        "g('dlgok').onclick=async function(){\n"
        "  g('dlgbg').classList.remove('open');\n"
        "  if(dlgCb){await dlgCb();dlgCb=null;}\n"
        "};\n"

        "document.addEventListener('keydown',function(e){\n"
        "  if(g('dlgbg').classList.contains('open')&&e.key==='Escape'){dlgCancel();return;}\n"
        "  if(g('lbox').classList.contains('open')){\n"
        "    if(e.key==='ArrowRight')lbNav(1);\n"
        "    else if(e.key==='ArrowLeft')lbNav(-1);\n"
        "    else if(e.key==='Escape')closeLb();\n"
        "    return;\n"
        "  }\n"
        "  var at=document.activeElement;\n"
        "  var typing=at&&(at.tagName==='INPUT'||at.tagName==='TEXTAREA'||at.isContentEditable);\n"
        "  if(e.key==='/'&&!typing){\n"
        "    e.preventDefault();g('q').focus();return;\n"
        "  }\n"
        "  if(typing)return;\n"
        "  if(e.key==='g'||e.key==='G')setView('grid');\n"
        "  else if(e.key==='l'||e.key==='L')setView('list');\n"
        "});\n"
        "g('lbmedia').addEventListener('wheel',lbWheel,{passive:false});\n"
        "document.addEventListener('mousemove',function(e){\n"
        "  if(!lbDragging)return;\n"
        "  lbJustDragged=true;\n"
        "  lbPanX=e.clientX-lbDragSX;\n"
        "  lbPanY=e.clientY-lbDragSY;\n"
        "  updateLbZoom();\n"
        "});\n"
        "document.addEventListener('mouseup',function(){\n"
        "  lbDragging=false;\n"
        "  if(lbJustDragged)setTimeout(function(){lbJustDragged=false;},200);\n"
        "});\n"

        "g('q').addEventListener('input',dbt(function(){goPage(0);}));\n"
        "(async function(){var s=await api('/ingest_status');if(s.running)pollIngest();})();\n"
        "restoreUIState();\n"
        "loadTree();\n"
        "loadMedia();\n"
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_json(self, data, status=200):
            body = json.dumps(data, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def read_body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qsp    = urllib.parse.parse_qs(parsed.query)
            gp     = lambda k: qsp.get(k, [None])[0]
            p      = parsed.path

            if p in ("/", ""):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html;charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
                return

            if p.startswith("/file/"):
                try:
                    mid = int(p.split("/")[-1])
                except ValueError:
                    self.send_json({"error": "bad id"}, 400); return
                row = conn.execute(
                    "SELECT file_path, extension FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error": "not found"}, 404); return
                fpath = row["file_path"]
                if not os.path.isfile(fpath):
                    parent = os.path.dirname(fpath)
                    fname  = os.path.basename(fpath)
                    if os.path.isdir(parent):
                        for f in os.listdir(parent):
                            if f.lower() == fname.lower():
                                fpath = os.path.join(parent, f)
                                break
                if not os.path.isfile(fpath):
                    self.send_json({"error": "file missing"}, 404); return
                mime_map = {
                    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif",  "bmp":  "image/bmp",  "webp": "image/webp",
                    "tiff":"image/tiff", "heic": "image/heif",
                    "mp4": "video/mp4",  "mov":  "video/quicktime",
                    "avi": "video/x-msvideo", "mkv": "video/x-matroska",
                    "webm":"video/webm", "m4v":  "video/mp4",
                    "wmv": "video/x-ms-wmv",  "flv":  "video/x-flv",
                }
                mime  = mime_map.get(row["extension"].lower(), "application/octet-stream")
                fsize = os.path.getsize(fpath)
                mtime = os.path.getmtime(fpath)
                last_modified = email.utils.formatdate(mtime, usegmt=True)
                # These files essentially never change in place once ingested,
                # so let the browser cache them for a week and skip
                # re-downloading on repeat lightbox views (e.g. paging back
                # and forth) whenever its cached copy is still fresh.
                if _not_modified(self.headers, mtime):
                    self.send_response(304)
                    self.send_header("Cache-Control", "private, max-age=604800")
                    self.send_header("Last-Modified", last_modified)
                    self.end_headers()
                    return
                rng   = self.headers.get("Range", "")
                if rng.startswith("bytes="):
                    try:
                        parts  = rng[6:].split("-")
                        start  = int(parts[0]) if parts[0] else 0
                        end    = int(parts[1]) if parts[1] else fsize - 1
                        end    = min(end, fsize - 1)
                        length = end - start + 1
                        self.send_response(206)
                        self.send_header("Content-Type", mime)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                        self.send_header("Content-Length", length)
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Cache-Control", "private, max-age=604800")
                        self.send_header("Last-Modified", last_modified)
                        self.end_headers()
                        with open(fpath, "rb") as f:
                            f.seek(start)
                            rem = length
                            while rem:
                                chunk = f.read(min(65536, rem))
                                if not chunk: break
                                self.wfile.write(chunk)
                                rem -= len(chunk)
                        return
                    except Exception:
                        pass
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", fsize)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, max-age=604800")
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                with open(fpath, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
                return

            if p.startswith("/thumb/"):
                try:
                    mid = int(p.split("/")[-1])
                except ValueError:
                    self.send_json({"error": "bad id"}, 400); return
                row = conn.execute(
                    "SELECT file_path, media_type FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error": "no thumbnail"}, 404); return
                thumb_path = get_thumbnail_path(db_path, mid, row["file_path"], row["media_type"])
                if not thumb_path:
                    self.send_json({"error": "thumbnail unavailable"}, 404); return
                tmtime = os.path.getmtime(thumb_path)
                if _not_modified(self.headers, tmtime):
                    self.send_response(304)
                    self.send_header("Cache-Control", "private, max-age=86400")
                    self.send_header("Last-Modified", email.utils.formatdate(tmtime, usegmt=True))
                    self.end_headers()
                    return
                data = open(thumb_path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", len(data))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("Last-Modified", email.utils.formatdate(tmtime, usegmt=True))
                self.end_headers()
                self.wfile.write(data)
                return

            if not p.startswith("/api/"):
                self.send_json({"error": "not found"}, 404); return
            ap = p[4:]

            if ap == "/ingest_status":
                with ingest_lock:
                    self.send_json(dict(ingest_state))
                return

            if ap == "/stats":
                total  = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
                images = conn.execute("SELECT COUNT(*) FROM media WHERE media_type='image'").fetchone()[0]
                videos = conn.execute("SELECT COUNT(*) FROM media WHERE media_type='video'").fetchone()[0]
                tsize  = conn.execute("SELECT SUM(file_size) FROM media").fetchone()[0] or 0
                tags   = conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
                folders= conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
                dups   = conn.execute(
                    "SELECT COUNT(*) FROM (SELECT sha256 FROM media "
                    "WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1)").fetchone()[0]
                self.send_json({"total": total, "images": images, "videos": videos,
                                "total_size": tsize, "tags": tags,
                                "folders": folders, "dup_groups": dups})
                return

            if ap == "/tags":
                rows = conn.execute(
                    "SELECT tag, COUNT(*) AS cnt FROM tags "
                    "GROUP BY tag ORDER BY cnt DESC LIMIT 200").fetchall()
                self.send_json([dict(r) for r in rows])
                return

            if ap == "/search":
                tags_val = gp("tags")
                tag_list = [t.strip() for t in tags_val.split()] if tags_val else None
                total, rows = search(conn,
                    query      = gp("query") or None,
                    media_type = gp("media_type") or None,
                    tags       = tag_list,
                    date_from  = gp("date_from") or None,
                    date_to    = gp("date_to") or None,
                    ext        = gp("ext") or None,
                    folder_id  = int(gp("folder_id")) if gp("folder_id") else None,
                    dupes_only = bool(gp("dupes_only")),
                    favorites_only = bool(gp("favorites_only")),
                    sort_by    = gp("sort_by") or "date_taken",
                    sort_dir   = gp("sort_dir") or "desc",
                    limit      = int(gp("limit") or 50),
                    offset     = int(gp("offset") or 0),
                )
                dup_hashes = set(r[0] for r in conn.execute(
                    "SELECT sha256 FROM media WHERE sha256 IS NOT NULL "
                    "GROUP BY sha256 HAVING COUNT(*)>1").fetchall())
                for r in rows:
                    r["is_dup"] = bool(r.get("sha256") and r["sha256"] in dup_hashes)
                self.send_json({"total": total, "results": rows})
                return

            if ap == "/duplicates":
                hashes = [r[0] for r in conn.execute(
                    "SELECT sha256 FROM media WHERE sha256 IS NOT NULL "
                    "GROUP BY sha256 HAVING COUNT(*)>1").fetchall()]
                groups = []
                for h in hashes:
                    rows = conn.execute(
                        "SELECT id, file_path, file_name, file_size, media_type, "
                        "extension, date_added, date_taken FROM media "
                        "WHERE sha256=? ORDER BY date_added ASC", (h,)).fetchall()
                    groups.append({"sha256": h, "items": [dict(r) for r in rows]})
                self.send_json({"groups": groups})
                return

            if ap.startswith("/media/") and ap.count("/") == 2:
                mid = int(ap.split("/")[-1])
                row = conn.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error": "not found"}, 404); return
                d = dict(row)
                d["tags"]    = get_tags(conn, mid)
                d["folders"] = [dict(r) for r in conn.execute(
                    "SELECT f.* FROM folders f "
                    "JOIN folder_media fm ON fm.folder_id=f.id "
                    "WHERE fm.media_id=? ORDER BY f.name", (mid,)).fetchall()]
                self.send_json(d)
                return

            if ap == "/folders":
                if gp("tree"):
                    self.send_json(folder_tree_flat(conn))
                else:
                    pid = gp("parent_id")
                    self.send_json(get_folders(conn, int(pid) if pid else None))
                return

            if ap.startswith("/folders/") and ap.count("/") == 2:
                fid = int(ap.split("/")[-1])
                row = conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
                if not row:
                    self.send_json({"error": "not found"}, 404); return
                d = dict(row)
                d["media"] = [dict(r) for r in conn.execute(
                    "SELECT m.* FROM media m "
                    "JOIN folder_media fm ON fm.media_id=m.id "
                    "WHERE fm.folder_id=? ORDER BY m.file_name", (fid,)).fetchall()]
                self.send_json(d)
                return

            self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            body = self.read_body()
            p    = self.path

            if p.startswith("/api/media/") and p.endswith("/tags"):
                mid = int(p.split("/")[-2])
                add_tags(conn, mid, body.get("tags", []))
                self.send_json({"ok": True}); return

            if p.startswith("/api/folders/") and p.endswith("/media"):
                fid = int(p.split("/")[-2])
                now = datetime.datetime.now().isoformat()
                for mid in body.get("media_ids", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO folder_media(folder_id,media_id,added_at) VALUES(?,?,?)",
                        (fid, mid, now))
                conn.commit()
                self.send_json({"ok": True}); return

            if p == "/api/bulk/tags":
                ids    = body.get("media_ids", [])
                tags   = body.get("tags", [])
                action = body.get("action", "add")
                for mid in ids:
                    if action == "remove":
                        remove_tags(conn, mid, tags)
                    else:
                        add_tags(conn, mid, tags)
                self.send_json({"ok": True, "count": len(ids)}); return

            if p == "/api/folders":
                pid = body.get("parent_id")
                fid = create_folder(conn, body.get("name", "New Folder"), parent_id=pid)
                self.send_json({"id": fid, "ok": True}); return

            if p == "/api/ingest":
                folder = (body.get("folder") or "").strip().strip('"')
                if not folder:
                    self.send_json({"error": "Folder path is required"}, 400); return
                if not os.path.isdir(folder):
                    self.send_json({"error": f"Not a folder: {folder}"}, 400); return
                with ingest_lock:
                    if ingest_state["running"]:
                        self.send_json({"error": "An ingest is already running"}, 409); return
                    ingest_state.update({
                        "running": True, "folder": folder, "current": 0, "total": 0,
                        "counts": {}, "done": False, "error": None,
                        "started_at": datetime.datetime.now().isoformat(),
                        "finished_at": None,
                    })
                threading.Thread(
                    target=_run_ingest_bg,
                    args=(folder, bool(body.get("mirror")), bool(body.get("force")),
                          bool(body.get("recursive", True))),
                    daemon=True,
                ).start()
                self.send_json({"ok": True, "started": True})
                return

            if p == "/api/disk/rename":
                try:
                    new_path = disk_rename(conn, body["media_id"], body["new_name"])
                    self.send_json({"ok": True, "new_path": new_path})
                except Exception as e:
                    self.send_json({"error": str(e)}, 400)
                return

            if p == "/api/disk/move":
                try:
                    new_path = disk_move(conn, body["media_id"], body["dest_folder"])
                    self.send_json({"ok": True, "new_path": new_path})
                except Exception as e:
                    self.send_json({"error": str(e)}, 400)
                return

            if p == "/api/disk/delete":
                try:
                    disk_delete(conn, body["media_id"], permanent=body.get("permanent", False))
                    self.send_json({"ok": True})
                except Exception as e:
                    self.send_json({"error": str(e)}, 400)
                return

            self.send_json({"error": "not found"}, 404)

        def do_PUT(self):
            body = self.read_body()
            p    = self.path

            if p.startswith("/api/folders/") and p.count("/") == 3:
                fid = int(p.split("/")[-1])
                if "name" in body:
                    conn.execute("UPDATE folders SET name=? WHERE id=?",
                                 (body["name"].strip(), fid))
                    conn.commit()
                self.send_json({"ok": True}); return

            if p.startswith("/api/media/") and p.endswith("/description"):
                mid = int(p.split("/")[-2])
                conn.execute("UPDATE media SET description=? WHERE id=?",
                             (body.get("description", ""), mid))
                conn.commit()
                self.send_json({"ok": True}); return

            if p.startswith("/api/media/") and p.endswith("/favorite"):
                mid = int(p.split("/")[-2])
                conn.execute("UPDATE media SET is_favorite=? WHERE id=?",
                             (1 if body.get("favorite") else 0, mid))
                conn.commit()
                self.send_json({"ok": True}); return

            self.send_json({"error": "not found"}, 404)

        def do_DELETE(self):
            n    = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            p    = self.path

            if p.startswith("/api/media/") and p.endswith("/tags"):
                mid = int(p.split("/")[-2])
                remove_tags(conn, mid, body.get("tags", []))
                self.send_json({"ok": True}); return

            if p.startswith("/api/folders/") and p.endswith("/media"):
                fid = int(p.split("/")[-2])
                for mid in body.get("media_ids", []):
                    conn.execute("DELETE FROM folder_media WHERE folder_id=? AND media_id=?",
                                 (fid, mid))
                conn.commit()
                self.send_json({"ok": True}); return

            if p.startswith("/api/folders/") and p.count("/") == 3:
                fid = int(p.split("/")[-1])
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("DELETE FROM folders WHERE id=?", (fid,))
                conn.commit()
                self.send_json({"ok": True}); return

            self.send_json({"error": "not found"}, 404)

    server = http.server.HTTPServer((host, port), Handler)
    return server, conn

def run_ui(db_path, host="127.0.0.1", port=DEFAULT_PORT):
    """CLI `ui` command: build the server and block in the foreground,
    exactly as before - unchanged behavior for existing terminal usage."""
    server, conn = _build_server(db_path, host, port)
    print(f"\n  Media Library v4 running at http://{host}:{port}")
    print(f"  Database : {os.path.abspath(db_path)}")
    print(f"  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
    finally:
        conn.close()

def run_desktop_app(db_path, host="127.0.0.1", port=DEFAULT_PORT):
    """CLI `app` command: run the exact same server on a background thread,
    inside a native window via pywebview, instead of the OS browser. This is
    the entry point the packaged .exe uses (see BUILD_DESKTOP_APP.md)."""
    try:
        import webview
    except ImportError:
        print("\n  The desktop app window requires pywebview, which isn't installed.")
        print("  Install it with:  pip install pywebview\n")
        sys.exit(1)

    server, conn = _build_server(db_path, host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    window = webview.create_window(
        "Media Library",
        f"http://{host}:{port}",
        width=1360, height=860, min_size=(900, 600),
    )

    def _on_closed():
        # Mirrors run_ui's shutdown/close pair so the DB is closed cleanly
        # and the background server thread is told to stop, instead of being
        # killed mid-request when the process exits. shutdown() alone stops
        # serve_forever()'s loop but leaves the listening socket open; also
        # closing it means a relaunch (or a second window) isn't left
        # fighting over the same port.
        server.shutdown()
        server.server_close()
        conn.close()

    window.events.closed += _on_closed
    webview.start()

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        prog="media_database_v4",
        description="Media Library v4")
    ap.add_argument("--db", default=None,
                    help=f"Database file (default: {DEFAULT_DB}, next to this "
                         f"script/exe - not the current directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ui = sub.add_parser("ui", help="Launch the web UI in your browser")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=DEFAULT_PORT)

    app_p = sub.add_parser("app", help="Launch as a native desktop window (requires pywebview)")
    app_p.add_argument("--host", default="127.0.0.1")
    app_p.add_argument("--port", type=int, default=DEFAULT_PORT)

    ing = sub.add_parser("ingest", help="Import media files")
    ing.add_argument("paths", nargs="+")
    ing.add_argument("--no-recursive", action="store_true")
    ing.add_argument("--force", action="store_true")
    ing.add_argument("--mirror-folders", action="store_true",
                     help="Auto-create virtual folders matching disk structure")
    ing.add_argument("--workers", type=int, default=None,
                     help="Parallel hashing/metadata threads (default: auto, based on CPU count)")

    sub.add_parser("stats", help="Show library statistics")

    args = ap.parse_args()
    if args.db is None:
        args.db = os.path.join(app_base_dir(), DEFAULT_DB)
    conn = get_db(args.db)

    if args.cmd == "ui":
        conn.close()
        run_ui(args.db, args.host, args.port)

    elif args.cmd == "app":
        conn.close()
        run_desktop_app(args.db, args.host, args.port)

    elif args.cmd == "ingest":
        for path in args.paths:
            if os.path.isfile(path):
                print(f"  {ingest_file(conn, path, args.force)}: {path}")
            elif os.path.isdir(path):
                print(f"Scanning {path} ...")
                counts = ingest_dir(conn, path,
                                    recursive=not args.no_recursive,
                                    force=args.force,
                                    mirror=args.mirror_folders,
                                    workers=args.workers)
                print(f"Done: {counts}")
            else:
                print(f"  Not found: {path}")
        conn.close()

    elif args.cmd == "stats":
        total  = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        images = conn.execute("SELECT COUNT(*) FROM media WHERE media_type='image'").fetchone()[0]
        videos = conn.execute("SELECT COUNT(*) FROM media WHERE media_type='video'").fetchone()[0]
        tsize  = conn.execute("SELECT SUM(file_size) FROM media").fetchone()[0] or 0
        tags   = conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
        folders= conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
        dups   = conn.execute(
            "SELECT COUNT(*) FROM (SELECT sha256 FROM media "
            "WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1)").fetchone()[0]
        print(f"\n  Files   : {total} ({images} images, {videos} videos)")
        print(f"  Size    : {fmt_size(tsize)}")
        print(f"  Tags    : {tags}   Folders: {folders}   Dup groups: {dups}\n")
        conn.close()


if __name__ == "__main__":
    main()
