#!/usr/bin/env python3
"""
Media Database - Organize and search images/videos by metadata.
Supports: JPG, PNG, GIF, BMP, WEBP, MP4, MOV, AVI, MKV, WEBM

Optional dependencies:
  pip install Pillow opencv-python send2trash
"""

import os
import sys
import json
import sqlite3
import hashlib
import argparse
import datetime
from pathlib import Path

# ── optional deps (graceful degradation) ──────────────────────────────────────
try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

try:
    import send2trash
    TRASH_AVAILABLE = True
except ImportError:
    TRASH_AVAILABLE = False

# ── constants ──────────────────────────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
DB_FILE    = "media_library.db"


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE SETUP
# ══════════════════════════════════════════════════════════════════════════════

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path   TEXT    NOT NULL UNIQUE,
            file_name   TEXT    NOT NULL,
            media_type  TEXT    NOT NULL,
            extension   TEXT    NOT NULL,
            file_size   INTEGER,
            width       INTEGER,
            height      INTEGER,
            duration    REAL,
            date_taken  TEXT,
            date_added  TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            sha256      TEXT,
            extra_meta  TEXT    DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS tags (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            tag      TEXT    NOT NULL,
            UNIQUE(media_id, tag)
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS media_fts
            USING fts5(file_name, description, content=media, content_rowid=id);

        CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media BEGIN
            INSERT INTO media_fts(rowid, file_name, description)
            VALUES (new.id, new.file_name, new.description);
        END;
        CREATE TRIGGER IF NOT EXISTS media_ad AFTER DELETE ON media BEGIN
            INSERT INTO media_fts(media_fts, rowid, file_name, description)
            VALUES ('delete', old.id, old.file_name, old.description);
        END;
        CREATE TRIGGER IF NOT EXISTS media_au AFTER UPDATE ON media BEGIN
            INSERT INTO media_fts(media_fts, rowid, file_name, description)
            VALUES ('delete', old.id, old.file_name, old.description);
            INSERT INTO media_fts(rowid, file_name, description)
            VALUES (new.id, new.file_name, new.description);
        END;

        CREATE TABLE IF NOT EXISTS folders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            parent_id   INTEGER REFERENCES folders(id) ON DELETE CASCADE,
            description TEXT    DEFAULT '',
            created_at  TEXT    NOT NULL,
            UNIQUE(name, parent_id)
        );

        CREATE TABLE IF NOT EXISTS folder_media (
            folder_id INTEGER NOT NULL REFERENCES folders(id)  ON DELETE CASCADE,
            media_id  INTEGER NOT NULL REFERENCES media(id)    ON DELETE CASCADE,
            added_at  TEXT    NOT NULL,
            PRIMARY KEY (folder_id, media_id)
        );

        CREATE INDEX IF NOT EXISTS idx_media_type    ON media(media_type);
        CREATE INDEX IF NOT EXISTS idx_date_taken    ON media(date_taken);
        CREATE INDEX IF NOT EXISTS idx_tags_tag      ON tags(tag);
        CREATE INDEX IF NOT EXISTS idx_sha256        ON media(sha256);
        CREATE INDEX IF NOT EXISTS idx_folder_parent ON folders(parent_id);
        CREATE INDEX IF NOT EXISTS idx_fm_folder     ON folder_media(folder_id);
        CREATE INDEX IF NOT EXISTS idx_fm_media      ON folder_media(media_id);
    """)
    conn.commit()
    return conn


# ══════════════════════════════════════════════════════════════════════════════
# METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def sha256_file(path: str, chunk=1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


def extract_image_meta(path: str) -> dict:
    meta = {"width": None, "height": None, "date_taken": None, "extra": {}}
    if not PIL_AVAILABLE:
        return meta
    try:
        with Image.open(path) as img:
            meta["width"], meta["height"] = img.size
            exif_data = img._getexif() if hasattr(img, "_getexif") else None
            if exif_data:
                for tag_id, value in exif_data.items():
                    tag = TAGS.get(tag_id, tag_id)
                    if tag == "DateTimeOriginal":
                        try:
                            dt = datetime.datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
                            meta["date_taken"] = dt.isoformat()
                        except ValueError:
                            pass
                    elif tag in ("Make", "Model", "Software", "Flash", "FocalLength"):
                        meta["extra"][tag] = str(value)
    except Exception as e:
        meta["extra"]["read_error"] = str(e)
    return meta


def extract_video_meta(path: str) -> dict:
    meta = {"width": None, "height": None, "duration": None, "date_taken": None, "extra": {}}
    if not CV2_AVAILABLE:
        try:
            mtime = os.path.getmtime(path)
            meta["date_taken"] = datetime.datetime.fromtimestamp(mtime).isoformat()
        except Exception:
            pass
        return meta
    try:
        cap = cv2.VideoCapture(path)
        if cap.isOpened():
            meta["width"]    = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            meta["height"]   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps              = cap.get(cv2.CAP_PROP_FPS)
            frame_count      = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            if fps and fps > 0:
                meta["duration"] = round(frame_count / fps, 2)
            meta["extra"]["fps"] = round(fps, 2) if fps else None
        cap.release()
    except Exception as e:
        meta["extra"]["read_error"] = str(e)
    try:
        mtime = os.path.getmtime(path)
        meta["date_taken"] = datetime.datetime.fromtimestamp(mtime).isoformat()
    except Exception:
        pass
    return meta


# ══════════════════════════════════════════════════════════════════════════════
# INGESTION
# ══════════════════════════════════════════════════════════════════════════════

def ingest_file(conn: sqlite3.Connection, path: str, force: bool = False) -> str:
    path = str(Path(path).resolve())
    ext  = Path(path).suffix.lower()

    if ext in IMAGE_EXTS:
        media_type = "image"
        meta = extract_image_meta(path)
    elif ext in VIDEO_EXTS:
        media_type = "video"
        meta = extract_video_meta(path)
    else:
        return "skipped"

    stat       = os.stat(path)
    file_size  = stat.st_size
    file_name  = Path(path).name
    date_added = datetime.datetime.now().isoformat()
    sha        = sha256_file(path)

    existing = conn.execute("SELECT id, sha256 FROM media WHERE file_path=?", (path,)).fetchone()
    if existing and not force:
        return "skipped"

    row = {
        "file_path":  path,
        "file_name":  file_name,
        "media_type": media_type,
        "extension":  ext.lstrip("."),
        "file_size":  file_size,
        "width":      meta.get("width"),
        "height":     meta.get("height"),
        "duration":   meta.get("duration"),
        "date_taken": meta.get("date_taken"),
        "date_added": date_added,
        "sha256":     sha,
        "extra_meta": json.dumps(meta.get("extra", {})),
    }

    if existing:
        conn.execute("""
            UPDATE media SET file_name=:file_name, file_size=:file_size,
            width=:width, height=:height, duration=:duration,
            date_taken=:date_taken, sha256=:sha256, extra_meta=:extra_meta
            WHERE file_path=:file_path
        """, row)
        conn.commit()
        return "updated"
    else:
        conn.execute("""
            INSERT INTO media
            (file_path, file_name, media_type, extension, file_size,
             width, height, duration, date_taken, date_added, sha256, extra_meta)
            VALUES
            (:file_path, :file_name, :media_type, :extension, :file_size,
             :width, :height, :duration, :date_taken, :date_added, :sha256, :extra_meta)
        """, row)
        conn.commit()
        return "added"


def _get_or_create_folder_path(conn, parts: list) -> int:
    parent_id = None
    for part in parts:
        row = conn.execute(
            "SELECT id FROM folders WHERE name=? AND parent_id IS ?", (part, parent_id)
        ).fetchone()
        if row:
            parent_id = row["id"]
        else:
            parent_id = create_folder(conn, part, parent_id=parent_id)
    return parent_id


def ingest_directory(conn: sqlite3.Connection, directory: str,
                     recursive: bool = True, force: bool = False,
                     mirror_folders: bool = False) -> dict:
    counts = {"added": 0, "skipped": 0, "updated": 0, "errors": 0}
    root = Path(directory).resolve()
    pattern = "**/*" if recursive else "*"
    files = [f for f in root.glob(pattern) if f.is_file()]
    total = len(files)
    _folder_cache = {}

    for i, fp in enumerate(files, 1):
        try:
            result = ingest_file(conn, str(fp), force=force)
            counts[result] = counts.get(result, 0) + 1

            if mirror_folders and result in ("added", "updated"):
                try:
                    rel_parts = list(fp.parent.relative_to(root).parts)
                except ValueError:
                    rel_parts = []
                if rel_parts:
                    cache_key = tuple(rel_parts)
                    if cache_key not in _folder_cache:
                        _folder_cache[cache_key] = _get_or_create_folder_path(conn, list(rel_parts))
                    folder_id = _folder_cache[cache_key]
                    row = conn.execute("SELECT id FROM media WHERE file_path=?",
                                       (str(fp.resolve()),)).fetchone()
                    if row:
                        add_to_folder(conn, folder_id, [row["id"]])
        except Exception as e:
            counts["errors"] += 1
            print(f"  ERROR {fp.name}: {e}")
        if i % 50 == 0 or i == total:
            print(f"  [{i}/{total}] added={counts['added']} skipped={counts['skipped']} "
                  f"updated={counts['updated']} errors={counts['errors']}", end="\r")
    print()
    return counts


# ══════════════════════════════════════════════════════════════════════════════
# DUPLICATE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def find_duplicates(conn: sqlite3.Connection, by: str = "hash") -> list[list[dict]]:
    """
    Find duplicate files in the library.

    by='hash'  – exact byte-for-byte duplicates (same SHA-256)
    by='size'  – same file size (fast pre-filter; may have false positives)
    by='name'  – same filename regardless of location
    """
    if by == "hash":
        sql = """
            SELECT sha256, COUNT(*) AS cnt
            FROM media
            WHERE sha256 IS NOT NULL
            GROUP BY sha256
            HAVING cnt > 1
            ORDER BY cnt DESC, sha256
        """
        key_col = "sha256"
    elif by == "size":
        sql = """
            SELECT file_size, COUNT(*) AS cnt
            FROM media
            WHERE file_size IS NOT NULL AND file_size > 0
            GROUP BY file_size
            HAVING cnt > 1
            ORDER BY cnt DESC, file_size DESC
        """
        key_col = "file_size"
    elif by == "name":
        sql = """
            SELECT file_name, COUNT(*) AS cnt
            FROM media
            GROUP BY file_name
            HAVING cnt > 1
            ORDER BY cnt DESC, file_name
        """
        key_col = "file_name"
    else:
        raise ValueError(f"Unknown dedup mode: {by}")

    groups = []
    for row in conn.execute(sql).fetchall():
        key_val = row[key_col]
        members = conn.execute(
            f"SELECT * FROM media WHERE {key_col}=? ORDER BY date_added ASC",
            (key_val,)
        ).fetchall()
        groups.append([dict(m) for m in members])
    return groups


def print_duplicates(groups: list[list[dict]], mode: str):
    if not groups:
        print("  No duplicates found.")
        return
    total_waste = 0
    for g in groups:
        sizes = [m.get("file_size") or 0 for m in g]
        wasted = sum(sizes[1:])          # keep first, rest are "waste"
        total_waste += wasted
        print(f"\n  ── {len(g)} copies  ({fmt_size(wasted)} wasted) ──")
        for i, m in enumerate(g):
            marker = " [KEEP]" if i == 0 else " [DUP] "
            print(f"  {marker} #{m['id']:>5}  {m['file_name']:<40}  "
                  f"{fmt_size(m.get('file_size'))}  {(m.get('date_taken') or m.get('date_added') or '')[:10]}")
            print(f"           {m['file_path']}")
    dup_files = sum(len(g) - 1 for g in groups)
    print(f"\n  Total: {len(groups)} duplicate groups, "
          f"{dup_files} redundant files, {fmt_size(total_waste)} reclaimable\n")


def delete_duplicates(conn: sqlite3.Connection, groups: list[list[dict]],
                      keep: str = "oldest", dry_run: bool = True) -> int:
    """
    Remove duplicate entries from the DB (not from disk).
    keep='oldest'  – keep the file added to the library first
    keep='newest'  – keep the most recently added file
    keep='largest' – keep the highest-resolution / largest file
    Returns count of deleted DB rows.
    """
    deleted = 0
    for g in groups:
        if keep == "oldest":
            sorted_g = sorted(g, key=lambda m: m.get("date_added") or "")
        elif keep == "newest":
            sorted_g = sorted(g, key=lambda m: m.get("date_added") or "", reverse=True)
        elif keep == "largest":
            sorted_g = sorted(g, key=lambda m: m.get("file_size") or 0, reverse=True)
        else:
            sorted_g = g

        to_remove = sorted_g[1:]   # everything after the keeper
        for m in to_remove:
            if dry_run:
                print(f"  [DRY RUN] would remove #{m['id']}  {m['file_path']}")
            else:
                conn.execute("DELETE FROM media WHERE id=?", (m["id"],))
                deleted += 1

    if not dry_run:
        conn.commit()
    return deleted



# ══════════════════════════════════════════════════════════════════════════════
# FOLDER MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def create_folder(conn, name: str, parent_id=None, description: str = "") -> int:
    name = name.strip()
    if not name:
        raise ValueError("Folder name cannot be empty")
    now = datetime.datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO folders(name, parent_id, description, created_at) VALUES (?,?,?,?)",
        (name, parent_id, description, now)
    )
    conn.commit()
    return cur.lastrowid


def rename_folder(conn, folder_id: int, new_name: str):
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Folder name cannot be empty")
    conn.execute("UPDATE folders SET name=? WHERE id=?", (new_name, folder_id))
    conn.commit()


def delete_folder(conn, folder_id: int, recursive: bool = False):
    children = conn.execute("SELECT id FROM folders WHERE parent_id=?", (folder_id,)).fetchall()
    if children and not recursive:
        raise ValueError(f"Folder has {len(children)} sub-folder(s). Use recursive=True.")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
    conn.commit()


def move_folder(conn, folder_id: int, new_parent_id=None):
    if new_parent_id is not None:
        anc = new_parent_id
        while anc is not None:
            if anc == folder_id:
                raise ValueError("Cannot move a folder into one of its own descendants.")
            row = conn.execute("SELECT parent_id FROM folders WHERE id=?", (anc,)).fetchone()
            anc = row["parent_id"] if row else None
    conn.execute("UPDATE folders SET parent_id=? WHERE id=?", (new_parent_id, folder_id))
    conn.commit()


def get_folder(conn, folder_id: int) -> dict:
    row = conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
    return dict(row) if row else None


def list_folders(conn, parent_id=None) -> list:
    if parent_id is None:
        rows = conn.execute("SELECT * FROM folders WHERE parent_id IS NULL ORDER BY name").fetchall()
    else:
        rows = conn.execute("SELECT * FROM folders WHERE parent_id=? ORDER BY name", (parent_id,)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["child_count"] = conn.execute("SELECT COUNT(*) FROM folders WHERE parent_id=?", (r["id"],)).fetchone()[0]
        d["media_count"] = conn.execute("SELECT COUNT(*) FROM folder_media WHERE folder_id=?", (r["id"],)).fetchone()[0]
        result.append(d)
    return result


def folder_tree(conn, parent_id=None, depth: int = 0) -> list:
    rows = list_folders(conn, parent_id)
    result = []
    for f in rows:
        f["depth"] = depth
        result.append(f)
        result.extend(folder_tree(conn, f["id"], depth + 1))
    return result


def add_to_folder(conn, folder_id: int, media_ids: list):
    now = datetime.datetime.now().isoformat()
    for mid in media_ids:
        conn.execute(
            "INSERT OR IGNORE INTO folder_media(folder_id, media_id, added_at) VALUES (?,?,?)",
            (folder_id, mid, now)
        )
    conn.commit()


def remove_from_folder(conn, folder_id: int, media_ids: list):
    for mid in media_ids:
        conn.execute("DELETE FROM folder_media WHERE folder_id=? AND media_id=?", (folder_id, mid))
    conn.commit()


def get_folder_media(conn, folder_id: int, sort_by="date_taken", sort_dir="desc",
                     limit: int = 200, offset: int = 0) -> list:
    order_col = SORT_COLUMNS.get(sort_by, SORT_COLUMNS["date_taken"])
    order_dir = "DESC" if sort_dir.lower() == "desc" else "ASC"
    rows = conn.execute(f"""
        SELECT m.*, GROUP_CONCAT(t.tag, ', ') AS tags
        FROM media m
        JOIN folder_media fm ON fm.media_id = m.id
        LEFT JOIN tags t ON t.media_id = m.id
        WHERE fm.folder_id = ?
        GROUP BY m.id
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
    """, (folder_id, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def get_media_folders(conn, media_id: int) -> list:
    rows = conn.execute("""
        SELECT f.*, COUNT(fm2.media_id) AS media_count
        FROM folders f
        JOIN folder_media fm ON fm.folder_id = f.id
        LEFT JOIN folder_media fm2 ON fm2.folder_id = f.id
        WHERE fm.media_id = ?
        GROUP BY f.id ORDER BY f.name
    """, (media_id,)).fetchall()
    return [dict(r) for r in rows]


def folder_breadcrumb(conn, folder_id: int) -> list:
    crumbs = []
    fid = folder_id
    while fid is not None:
        row = conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
        if not row:
            break
        crumbs.insert(0, dict(row))
        fid = row["parent_id"]
    return crumbs


def print_folder_tree(conn):
    tree = folder_tree(conn)
    if not tree:
        print("  (no folders yet)")
        return
    for f in tree:
        indent = "  " + "     " * f["depth"]
        icon   = "📁"
        print(f"{indent}{icon} [{f['id']:>4}]  {f['name']:<28}  "
              f"{f['media_count']} files,  {f['child_count']} sub-folders")

# ══════════════════════════════════════════════════════════════════════════════
# TAG MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def add_tags(conn: sqlite3.Connection, media_id: int, tags: list):
    for tag in tags:
        tag = tag.strip().lower()
        if tag:
            conn.execute("INSERT OR IGNORE INTO tags(media_id, tag) VALUES (?,?)", (media_id, tag))
    conn.commit()


def remove_tags(conn: sqlite3.Connection, media_id: int, tags: list):
    for tag in tags:
        conn.execute("DELETE FROM tags WHERE media_id=? AND tag=?", (media_id, tag.strip().lower()))
    conn.commit()


def get_tags(conn: sqlite3.Connection, media_id: int) -> list:
    rows = conn.execute("SELECT tag FROM tags WHERE media_id=? ORDER BY tag", (media_id,)).fetchall()
    return [r["tag"] for r in rows]


def set_description(conn: sqlite3.Connection, media_id: int, description: str):
    conn.execute("UPDATE media SET description=? WHERE id=?", (description, media_id))
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ══════════════════════════════════════════════════════════════════════════════

SORT_COLUMNS = {
    "date_taken":  "COALESCE(m.date_taken, m.date_added)",
    "date_added":  "m.date_added",
    "file_size":   "m.file_size",
    "file_name":   "m.file_name COLLATE NOCASE",
}

def search(conn: sqlite3.Connection,
           query:      str  = None,
           media_type: str  = None,
           tags:       list = None,
           date_from:  str  = None,
           date_to:    str  = None,
           min_width:  int  = None,
           min_height: int  = None,
           ext:        str  = None,
           folder_id:  int  = None,
           sort_by:    str  = "date_taken",
           sort_dir:   str  = "desc",
           limit:      int  = 50,
           offset:     int  = 0) -> list:

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
        for tag in tags:
            wheres.append("m.id IN (SELECT media_id FROM tags WHERE tag=?)")
            params.append(tag.strip().lower())

    if date_from:
        wheres.append("m.date_taken >= ?")
        params.append(date_from)

    if date_to:
        wheres.append("m.date_taken <= ?")
        params.append(date_to)

    if min_width:
        wheres.append("m.width >= ?")
        params.append(min_width)

    if min_height:
        wheres.append("m.height >= ?")
        params.append(min_height)

    if ext:
        wheres.append("m.extension=?")
        params.append(ext.lstrip(".").lower())

    where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""

    order_col = SORT_COLUMNS.get(sort_by, SORT_COLUMNS["date_taken"])
    order_dir = "DESC" if sort_dir.lower() == "desc" else "ASC"

    sql = f"""
        SELECT m.*, GROUP_CONCAT(t.tag, ', ') AS tags
        FROM media m
        LEFT JOIN tags t ON t.media_id = m.id
        {where_clause}
        GROUP BY m.id
        ORDER BY {order_col} {order_dir}
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fmt_size(n) -> str:
    if n is None: return "?"
    n = int(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(secs) -> str:
    if secs is None: return ""
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def print_results(results: list, verbose: bool = False):
    if not results:
        print("  (no results)")
        return
    for r in results:
        dims = f"{r['width']}×{r['height']}" if r['width'] else "?"
        dur  = f" [{fmt_duration(r['duration'])}]" if r['media_type'] == 'video' else ""
        date = (r['date_taken'] or r['date_added'] or "")[:10]
        tags = r.get('tags') or ""
        print(f"  [{r['id']:>5}] {r['file_name']:<40} {r['media_type']:<6} "
              f"{dims:<12} {fmt_size(r['file_size']):<10} {date}")
        if verbose:
            print(f"         Path : {r['file_path']}")
            if tags:
                print(f"         Tags : {tags}")
            if r.get('description'):
                print(f"         Desc : {r['description']}")
        elif tags:
            print(f"         tags: {tags}")


def stats(conn: sqlite3.Connection):
    rows = conn.execute("""
        SELECT media_type, COUNT(*) AS cnt,
               SUM(file_size) AS total_size,
               AVG(width) AS avg_w, AVG(height) AS avg_h
        FROM media GROUP BY media_type
    """).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    tag_count    = conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
    folder_count = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
    dup_count = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT sha256 FROM media WHERE sha256 IS NOT NULL
            GROUP BY sha256 HAVING COUNT(*) > 1
        )
    """).fetchone()[0]
    print(f"\n{'─'*50}")
    print(f"  Total files     : {total}   Unique tags: {tag_count}   Folders: {folder_count}")
    print(f"  Duplicate groups: {dup_count}")
    for r in rows:
        print(f"  {r['media_type'].capitalize()+'s':<8}: {r['cnt']} files, "
              f"{fmt_size(r['total_size'])} total, "
              f"avg {int(r['avg_w'] or 0)}×{int(r['avg_h'] or 0)}")
    print(f"{'─'*50}\n")




# ══════════════════════════════════════════════════════════════════════════════
# DISK OPERATIONS  (real file/folder edits with Recycle Bin safety)
# ══════════════════════════════════════════════════════════════════════════════

def _check_trash():
    if not TRASH_AVAILABLE:
        raise RuntimeError(
            "send2trash is not installed. Run: pip install send2trash\n"
            "This is required for safe deletion via the Recycle Bin."
        )

def disk_rename_file(old_path: str, new_name: str) -> str:
    """Rename a file on disk. Returns the new absolute path."""
    import shutil
    old = Path(old_path)
    if not old.exists():
        raise FileNotFoundError(f"File not found: {old_path}")
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("New name cannot be empty")
    if "." not in new_name:
        new_name += old.suffix        # keep original extension if omitted
    new = old.parent / new_name
    if new.exists() and new.resolve() != old.resolve():
        raise FileExistsError(f"A file named \'{new_name}\' already exists in this folder")
    old.rename(new)
    return str(new)


def disk_move_file(file_path: str, dest_dir: str) -> str:
    """Move a file into dest_dir on disk. Creates dest_dir if needed. Returns new path."""
    import shutil
    src = Path(file_path)
    dst_dir = Path(dest_dir)
    if not src.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        raise FileExistsError(f"\'{src.name}\' already exists in the destination folder")
    shutil.move(str(src), str(dst))
    return str(dst)


def disk_delete_file(file_path: str, permanent: bool = False):
    """Send file to Recycle Bin (default) or delete permanently."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if permanent:
        p.unlink()
    else:
        _check_trash()
        send2trash.send2trash(str(p.resolve()))


def disk_rename_folder(old_path: str, new_name: str) -> str:
    """Rename a real folder on disk. Returns new path."""
    old = Path(old_path)
    if not old.exists():
        raise FileNotFoundError(f"Folder not found: {old_path}")
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Folder name cannot be empty")
    new = old.parent / new_name
    if new.exists() and new.resolve() != old.resolve():
        raise FileExistsError(f"A folder named \'{new_name}\' already exists here")
    old.rename(new)
    return str(new)


def disk_delete_folder(folder_path: str, permanent: bool = False):
    """Send entire folder to Recycle Bin or delete permanently."""
    import shutil
    p = Path(folder_path)
    if not p.exists():
        raise FileNotFoundError(f"Folder not found: {folder_path}")
    if permanent:
        shutil.rmtree(str(p))
    else:
        _check_trash()
        send2trash.send2trash(str(p.resolve()))


def disk_create_folder(parent_path: str, name: str) -> str:
    """Create a new real folder inside parent_path. Returns new path."""
    parent = Path(parent_path)
    new_dir = parent / name.strip()
    if new_dir.exists():
        raise FileExistsError(f"Folder \'{name}\' already exists")
    new_dir.mkdir(parents=True)
    return str(new_dir)


def db_update_paths(conn, old_prefix: str, new_prefix: str) -> int:
    """After a rename/move on disk, sync all matching paths in the database."""
    rows = conn.execute(
        "SELECT id, file_path FROM media WHERE file_path LIKE ?",
        (old_prefix.rstrip(os.sep) + "%",)
    ).fetchall()
    updated = 0
    for row in rows:
        old_p = row["file_path"]
        new_p = new_prefix + old_p[len(old_prefix.rstrip(os.sep)):]
        conn.execute(
            "UPDATE media SET file_path=?, file_name=? WHERE id=?",
            (new_p, Path(new_p).name, row["id"])
        )
        updated += 1
    conn.commit()
    return updated

# ══════════════════════════════════════════════════════════════════════════════
# WEB UI SERVER
# ══════════════════════════════════════════════════════════════════════════════

def run_web_ui(db_path: str, host: str = "127.0.0.1", port: int = 7432):
    """Launch a local web server for the media database UI."""
    import http.server
    import urllib.parse
    import mimetypes
    import base64
    from pathlib import Path as _Path

    # Inline the HTML so it's self-contained in this one Python file
    HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Media Library</title>
<style>
  :root{--bg:#0f0f11;--surface:#18181c;--surface2:#222228;--border:#2e2e38;--accent:#7c6ef7;--accent2:#a89df9;--text:#e8e6f0;--muted:#8a87a0;--danger:#e05c6e;--success:#4ecb8d;--tag-bg:#2a2444;--tag-text:#a89df9;--radius:8px;--font:'Inter',system-ui,sans-serif;--mono:'JetBrains Mono','Fira Code',monospace}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px;min-height:100vh}
  .app{display:grid;grid-template-rows:56px 1fr;height:100vh}
  .topbar{background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 20px;gap:12px}
  .topbar-logo{font-weight:700;font-size:16px;color:var(--accent2);white-space:nowrap}
  .topbar-logo span{color:var(--muted);font-weight:400}
  .search-bar{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:7px 12px;font-size:14px;outline:none}
  .search-bar:focus{border-color:var(--accent)}
  .main{display:grid;grid-template-columns:260px 1fr;overflow:hidden}
  .sidebar{background:var(--surface);border-right:1px solid var(--border);overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:18px}
  .sidebar h3{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:6px}
  .filter-group{display:flex;flex-direction:column;gap:5px}
  .filter-group label{font-size:12px;color:var(--muted)}
  .filter-group input,.filter-group select,.sel{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:7px 10px;font-size:13px;width:100%;outline:none;transition:border-color .15s}
  .filter-group input:focus,.filter-group select:focus,.sel:focus{border-color:var(--accent)}
  .btn{background:var(--accent);color:#fff;border:none;border-radius:var(--radius);padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;transition:opacity .15s;width:100%}
  .btn:hover{opacity:.85}
  .btn-ghost{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
  .btn-danger{background:var(--danger)}
  .btn-sm{padding:5px 10px;font-size:12px;width:auto}
  .stat-chips{display:flex;flex-wrap:wrap;gap:6px}
  .chip{background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:3px 10px;font-size:12px;color:var(--muted)}
  .chip b{color:var(--text)}
  .content{overflow:hidden;display:flex;flex-direction:column}
  .tabs{display:flex;gap:2px;padding:0 20px;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
  .tab{padding:12px 16px;cursor:pointer;font-size:13px;font-weight:500;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}
  .tab.active{color:var(--accent2);border-bottom-color:var(--accent)}
  .toolbar{padding:10px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;background:var(--surface);flex-wrap:wrap;flex-shrink:0}
  .toolbar-count{color:var(--muted);font-size:13px;flex:1}
  .view-toggle{display:flex;gap:4px}
  .view-btn{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:5px 10px;cursor:pointer;color:var(--muted);font-size:16px}
  .view-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
  .sort-row{display:flex;align-items:center;gap:8px;padding:8px 20px;background:var(--surface2);border-bottom:1px solid var(--border);flex-shrink:0}
  .sort-row label{font-size:12px;color:var(--muted);white-space:nowrap}
  .sort-row select{background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:5px 8px;font-size:12px;outline:none;cursor:pointer}
  .sort-row select:focus{border-color:var(--accent)}
  .dir-btn{background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:5px 10px;font-size:14px;cursor:pointer}
  .dir-btn:hover{border-color:var(--accent)}
  .grid-view{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;padding:20px}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;cursor:pointer;transition:border-color .15s,transform .1s}
  .card:hover{border-color:var(--accent);transform:translateY(-1px)}
  .card-thumb{width:100%;aspect-ratio:1;background:var(--surface2);position:relative;display:flex;align-items:center;justify-content:center;overflow:hidden}
  .card-thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .card-thumb .icon{font-size:40px;color:var(--muted)}
  .card-type-badge{position:absolute;top:6px;right:6px;background:rgba(0,0,0,.7);border-radius:4px;padding:2px 6px;font-size:10px;font-family:var(--mono);color:var(--accent2)}
  .dup-badge{position:absolute;top:6px;left:6px;background:var(--danger);border-radius:4px;padding:2px 6px;font-size:10px;font-weight:700;color:#fff}
  .vid-dur{position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.65);border-radius:4px;padding:2px 6px;font-size:10px;color:#fff}
  .card-body{padding:8px 10px}
  .card-name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
  .card-meta{font-size:11px;color:var(--muted)}
  .card-tags{margin-top:5px;display:flex;flex-wrap:wrap;gap:3px}
  .tag{background:var(--tag-bg);color:var(--tag-text);border-radius:4px;padding:1px 6px;font-size:10px;font-weight:500}
  .list-view{padding:16px 20px;display:flex;flex-direction:column;gap:6px}
  .list-row{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:8px 12px;cursor:pointer;display:grid;grid-template-columns:44px 1fr 80px 110px 90px 80px;align-items:center;gap:10px;transition:border-color .15s}
  .list-row:hover{border-color:var(--accent)}
  .list-thumb{width:40px;height:40px;border-radius:6px;overflow:hidden;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:20px;flex-shrink:0}
  .list-thumb img{width:100%;height:100%;object-fit:cover}
  .list-name{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .list-sub{font-size:11px;color:var(--muted)}
  .list-cell{font-size:12px;color:var(--muted)}
  .modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:center;z-index:100;padding:16px}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:12px;width:min(760px,100%);max-height:94vh;overflow-y:auto;display:flex;flex-direction:column}
  .modal-header{padding:16px 20px 0;display:flex;justify-content:space-between;align-items:flex-start;flex-shrink:0}
  .modal-title{font-size:16px;font-weight:700;margin-bottom:2px}
  .modal-sub{font-size:12px;color:var(--muted);word-break:break-all}
  .close-btn{background:none;border:none;color:var(--muted);font-size:22px;cursor:pointer;padding:0 4px;line-height:1}
  .close-btn:hover{color:var(--text)}
  .preview-area{background:#000;display:flex;align-items:center;justify-content:center;min-height:180px;max-height:440px;overflow:hidden;flex-shrink:0;position:relative}
  .preview-area img{max-width:100%;max-height:440px;object-fit:contain;display:block}
  .preview-area video{max-width:100%;max-height:440px;display:block;outline:none;background:#000}
  .preview-msg{color:var(--muted);font-size:13px;padding:32px;text-align:center}
  .modal-body{padding:14px 20px 20px;display:flex;flex-direction:column;gap:14px}
  .meta-grid{display:grid;grid-template-columns:130px 1fr;gap:5px 12px}
  .meta-label{font-size:12px;color:var(--muted)}
  .meta-value{font-size:13px;word-break:break-all;font-family:var(--mono)}
  .tag-input-row{display:flex;gap:8px}
  .tag-input{flex:1;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:7px 10px;font-size:13px;outline:none}
  .tag-input:focus{border-color:var(--accent)}
  .tag-list{display:flex;flex-wrap:wrap;gap:6px;min-height:24px}
  .tag-rm{background:var(--tag-bg);color:var(--tag-text);border-radius:4px;padding:3px 8px;font-size:12px;cursor:pointer;border:none;display:flex;align-items:center;gap:4px}
  .tag-rm:hover{background:var(--danger);color:#fff}
  textarea.desc-input{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:8px 10px;font-size:13px;width:100%;resize:vertical;min-height:60px;outline:none;font-family:var(--font)}
  textarea.desc-input:focus{border-color:var(--accent)}
  .section-title{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  .dup-group{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:12px;margin-bottom:10px}
  .dup-group-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .dup-group-info{font-size:13px;font-weight:600}
  .dup-waste{color:var(--danger);font-size:12px}
  .dup-item{display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--border)}
  .dup-item:first-of-type{border-top:none}
  .dup-keep{font-size:10px;background:var(--success);color:#000;border-radius:4px;padding:2px 6px;font-weight:700}
  .dup-copy{font-size:10px;background:var(--danger);color:#fff;border-radius:4px;padding:2px 6px;font-weight:700}
  .dup-path{font-size:11px;color:var(--muted);font-family:var(--mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .dup-size{font-size:11px;color:var(--muted);white-space:nowrap}
  .empty{text-align:center;padding:60px 20px;color:var(--muted)}
  .empty .icon{font-size:48px;margin-bottom:12px}
  .loading{text-align:center;padding:40px;color:var(--muted)}
  @keyframes spin{to{transform:rotate(360deg)}}
  .spinner{display:inline-block;width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite}
  .pagination{display:flex;gap:6px;justify-content:center;padding:16px;flex-wrap:wrap;flex-shrink:0}
  .page-btn{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:6px 12px;cursor:pointer;color:var(--text);font-size:13px}
  .page-btn.active{background:var(--accent);border-color:var(--accent)}
  .page-btn:hover:not(.active){border-color:var(--accent)}
  .toast{position:fixed;bottom:24px;right:24px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:10px 16px;font-size:13px;z-index:200;animation:slidein .2s ease}
  @keyframes slidein{from{transform:translateY(12px);opacity:0}}
  .toast.success{border-color:var(--success);color:var(--success)}
  .toast.error{border-color:var(--danger);color:var(--danger)}
  /* DISK OPS */
  .disk-btn{background:none;border:1px solid var(--border);border-radius:5px;color:var(--muted);font-size:11px;padding:3px 8px;cursor:pointer;transition:all .15s}
  .disk-btn:hover{border-color:var(--accent);color:var(--text)}
  .disk-btn.danger:hover{border-color:var(--danger);color:var(--danger)}
  .disk-ops-bar{display:flex;gap:6px;padding:10px 20px;background:var(--surface2);border-bottom:1px solid var(--border);align-items:center;flex-shrink:0}
  .disk-ops-bar span{font-size:11px;color:var(--muted);margin-right:4px}
  .warning-badge{display:inline-block;background:rgba(224,92,110,.15);border:1px solid var(--danger);border-radius:4px;padding:2px 8px;font-size:11px;color:var(--danger);margin-left:auto}
  .confirm-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;z-index:200}
  .confirm-box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:24px;width:min(420px,90%);display:flex;flex-direction:column;gap:14px}
  .confirm-box h3{font-size:15px;font-weight:700}
  .confirm-box p{font-size:13px;color:var(--muted);line-height:1.5}
  .confirm-box .path{font-family:var(--mono);font-size:11px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:6px 10px;word-break:break-all}
  .confirm-actions{display:flex;gap:8px;justify-content:flex-end}
  .inp{background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);color:var(--text);padding:7px 10px;font-size:13px;width:100%;outline:none}
  .inp:focus{border-color:var(--accent)}
</style>
</head>
<body>
<div class="app">
  <div class="topbar">
    <div class="topbar-logo">&#128193; Media Library <span>v3</span></div>
    <input class="search-bar" id="globalSearch" placeholder="Search filenames &amp; descriptions&hellip;" />
  </div>
  <div class="main">
    <div class="sidebar">
      <div>
        <h3>Stats</h3>
        <div class="stat-chips" id="statChips"><div class="chip">Loading&hellip;</div></div>
      </div>
      <div>
        <h3>Filters</h3>
        <div class="filter-group"><label>Type</label>
          <select id="fType"><option value="">All</option><option value="image">Images</option><option value="video">Videos</option></select>
        </div>
        <div class="filter-group"><label>Date from</label><input type="date" id="fFrom" /></div>
        <div class="filter-group"><label>Date to</label><input type="date" id="fTo" /></div>
        <div class="filter-group"><label>Min width (px)</label><input type="number" id="fMinW" placeholder="e.g. 1920" /></div>
        <div class="filter-group"><label>Min height (px)</label><input type="number" id="fMinH" placeholder="e.g. 1080" /></div>
        <div class="filter-group"><label>Extension</label><input type="text" id="fExt" placeholder="jpg, mp4&hellip;" /></div>
        <div class="filter-group"><label>Tags (space-separated)</label><input type="text" id="fTags" placeholder="vacation beach" /></div>
        <br>
        <button class="btn" onclick="applyFilters()">Apply Filters</button>
        <br><br>
        <button class="btn btn-ghost" onclick="clearFilters()">Clear</button>
      </div>
      <div>
        <h3>Popular Tags</h3>
        <div class="tag-list" id="popularTags"></div>
      </div>
    </div>
    <div class="content">
      <div class="tabs">
        <div class="tab active" onclick="switchTab('library')">Library</div>
        <div class="tab" onclick="switchTab('folders')">Folders</div>
        <div class="tab" onclick="switchTab('duplicates')">Duplicates</div>
      </div>
      <div id="tabLibrary" style="display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden">
        <div class="toolbar">
          <span class="toolbar-count" id="resultCount">&ndash;</span>
          <div class="view-toggle">
            <button class="view-btn active" id="btnGrid" onclick="setView('grid')">&#8862;</button>
            <button class="view-btn" id="btnList" onclick="setView('list')">&#9776;</button>
          </div>
        </div>
        <div class="sort-row">
          <label>Sort by</label>
          <select id="sortBy" onchange="sortChanged()">
            <option value="date_taken">Date Taken</option>
            <option value="date_added">Date Uploaded</option>
            <option value="file_size">File Size</option>
            <option value="file_name">File Name</option>
          </select>
          <button class="dir-btn" id="dirBtn" onclick="toggleDir()" title="Toggle sort direction">&#8595; Desc</button>
        </div>
        <div class="disk-ops-bar" id="diskOpsBar" style="display:none">
          <span>Selected:</span>
          <span id="selCount" style="font-size:12px;color:var(--text);font-weight:600">0 files</span>
          <button class="disk-btn" onclick="bulkMovePrompt()">&#128193; Move to folder&hellip;</button>
          <button class="disk-btn danger" onclick="bulkDeletePrompt()">&#128465; Delete&hellip;</button>
          <button class="disk-btn" onclick="clearSelection()" style="margin-left:4px">&#x2715; Clear</button>
          <span class="warning-badge">&#9888; Affects real files on disk</span>
        </div>
        <div id="mediaContainer" style="overflow-y:auto;flex:1"></div>
        <div class="pagination" id="pagination"></div>
      </div>
      <div id="tabFolders" style="display:none;flex:1;min-height:0;overflow:hidden;display:none">
        <div style="display:grid;grid-template-columns:240px 1fr;height:100%;overflow:hidden">
          <!-- Folder tree panel -->
          <div style="border-right:1px solid var(--border);overflow-y:auto;padding:12px;background:var(--surface);display:flex;flex-direction:column;gap:8px">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
              <span style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)">Folders</span>
              <button class="btn btn-sm" onclick="promptNewFolder(null)" title="New root folder">+ New</button>
            </div>
            <div id="folderTree"></div>
          </div>
          <!-- Folder content panel -->
          <div style="overflow:hidden;display:flex;flex-direction:column">
            <div style="padding:10px 16px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0">
              <div id="folderBreadcrumb" style="font-size:13px;color:var(--muted);flex:1">Select a folder</div>
              <button class="btn btn-ghost btn-sm" id="btnNewSubfolder" onclick="promptNewFolder(currentFolderId)" style="display:none">+ Sub-folder</button>
              <button class="btn btn-ghost btn-sm" id="btnRenameFolder" onclick="promptRenameFolder()" style="display:none">Rename</button>
              <button class="btn btn-danger btn-sm" id="btnDeleteFolder" onclick="confirmDeleteFolder()" style="display:none">Delete</button>
            </div>
            <div id="folderContent" style="overflow-y:auto;flex:1;padding:16px">
              <div class="empty"><div class="icon">&#128193;</div><p>Select a folder from the left panel.</p></div>
            </div>
          </div>
        </div>
      </div>

      <div id="tabDuplicates" style="display:none;padding:20px;overflow-y:auto">
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap">
          <select id="dupMode" class="sel" style="width:auto">
            <option value="hash">Exact duplicates (SHA-256)</option>
            <option value="size">Same file size</option>
            <option value="name">Same filename</option>
          </select>
          <button class="btn btn-sm" onclick="loadDuplicates()">Find Duplicates</button>
          <button class="btn btn-sm btn-danger" onclick="cleanDuplicates()" id="btnClean" disabled>Remove Duplicates from DB</button>
        </div>
        <div id="dupSummary" style="margin-bottom:12px;color:var(--muted);font-size:13px"></div>
        <div id="dupContainer"></div>
      </div>
    </div>
  </div>
</div>

<!-- CONFIRMATION DIALOG -->
<div class="confirm-overlay" id="confirmOverlay" style="display:none">
  <div class="confirm-box">
    <h3 id="confirmTitle">Are you sure?</h3>
    <p id="confirmMsg"></p>
    <div class="path" id="confirmPath" style="display:none"></div>
    <div id="confirmExtra"></div>
    <div class="confirm-actions">
      <button class="btn btn-ghost btn-sm" onclick="confirmCancel()">Cancel</button>
      <button class="btn btn-danger btn-sm" id="confirmOkBtn" onclick="confirmOk()">Confirm</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="modalOverlay" style="display:none" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <div class="modal-header">
      <div style="flex:1;min-width:0">
        <div class="modal-title" id="modalTitle">&ndash;</div>
        <div class="modal-sub" id="modalSub">&ndash;</div>
      </div>
      <div style="display:flex;gap:6px;align-items:center;margin-left:12px;flex-shrink:0">
        <button class="btn btn-ghost btn-sm" onclick="prevItem()" title="Previous">&#9664;</button>
        <button class="btn btn-ghost btn-sm" onclick="nextItem()" title="Next">&#9654;</button>
        <button class="close-btn" onclick="closeModal()">&#x2715;</button>
      </div>
    </div>
    <div class="preview-area" id="previewArea">
      <div class="preview-msg"><div class="spinner"></div></div>
    </div>
    <div class="modal-body">
      <div class="meta-grid" id="modalMeta"></div>
      <div>
        <div class="section-title" style="margin-bottom:8px">Tags</div>
        <div class="tag-list" id="modalTags"></div>
        <div class="tag-input-row" style="margin-top:8px">
          <input class="tag-input" id="newTagInput" placeholder="Add tag&hellip;" onkeydown="if(event.key==='Enter')addTag()" />
          <button class="btn btn-sm" onclick="addTag()">Add</button>
        </div>
      </div>
      <div>
        <div class="section-title" style="margin-bottom:8px">Folders</div>
        <div id="modalFolders" style="display:flex;flex-direction:column;gap:2px;max-height:120px;overflow-y:auto"><span style="color:var(--muted);font-size:12px">Loading&hellip;</span></div>
      </div>
      <div style="border:1px solid var(--danger);border-radius:var(--radius);padding:12px">
        <div class="section-title" style="margin-bottom:10px;color:var(--danger)">&#9888; Disk Operations</div>
        <p style="font-size:12px;color:var(--muted);margin-bottom:10px">These actions change real files on your computer.</p>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          <button class="disk-btn" onclick="renameFilePrompt()">&#9998; Rename file</button>
          <button class="disk-btn" onclick="moveFilePrompt()">&#128193; Move file&hellip;</button>
          <button class="disk-btn danger" onclick="deleteFilePrompt(false)">&#128465; Send to Recycle Bin</button>
          <button class="disk-btn danger" onclick="deleteFilePrompt(true)">&#128683; Delete permanently</button>
        </div>
      </div>
      <div>
        <div class="section-title" style="margin-bottom:8px">Description</div>
        <textarea class="desc-input" id="descInput" placeholder="Add a description&hellip;" rows="3"></textarea>
        <button class="btn btn-sm" style="margin-top:8px" onclick="saveDescription()">Save Description</button>
      </div>
    </div>
  </div>
</div>

<script>
const PAGE_SIZE = 48;
let currentPage = 0, currentView = 'grid', currentTab = 'library';
let currentItem = null, currentResults = [], currentIndex = 0;
let sortDir = 'desc', dupGroups = [], filters = {};

async function api(path, method='GET', body=null) {
  const opts = {method, headers:{'Content-Type':'application/json'}};
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch('/api' + path, opts);
  return r.json();
}
function debounce(fn, ms) { let t; return (...a) => {clearTimeout(t); t = setTimeout(()=>fn(...a), ms);} }

async function init() {
  loadStats(); loadTags(); loadMedia();
  document.getElementById('globalSearch').addEventListener('input', debounce(()=>{currentPage=0;loadMedia();}, 300));
}

async function loadStats() {
  const s = await api('/stats');
  document.getElementById('statChips').innerHTML =
    `<div class="chip"><b>${s.total}</b> files</div><div class="chip"><b>${s.images}</b> images</div><div class="chip"><b>${s.videos}</b> videos</div><div class="chip"><b>${s.tags}</b> tags</div><div class="chip"><b>${s.dup_groups}</b> dups</div>`;
}
async function loadTags() {
  const tags = await api('/tags');
  document.getElementById('popularTags').innerHTML = tags.slice(0,20).map(t=>
    `<button class="tag" style="cursor:pointer" onclick="filterByTag('${t.tag}')">${t.tag} <span style="opacity:.6">${t.cnt}</span></button>`).join('');
}

function sortChanged() { currentPage = 0; loadMedia(); }
function toggleDir() {
  sortDir = sortDir === 'desc' ? 'asc' : 'desc';
  document.getElementById('dirBtn').innerHTML = sortDir === 'desc' ? '&#8595; Desc' : '&#8593; Asc';
  currentPage = 0; loadMedia();
}

async function loadMedia() {
  const q = document.getElementById('globalSearch').value;
  const sortBy = document.getElementById('sortBy').value;
  const params = new URLSearchParams({limit:PAGE_SIZE, offset:currentPage*PAGE_SIZE, sort_by:sortBy, sort_dir:sortDir, ...(q&&{query:q}), ...filters});
  document.getElementById('mediaContainer').innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  const data = await api('/search?' + params);
  currentResults = data.results;
  renderMedia(data.results, data.total);
  renderPagination(data.total);
}

function renderMedia(items, total) {
  document.getElementById('resultCount').textContent = `${total} file${total!==1?'s':''} found`;
  const el = document.getElementById('mediaContainer');
  if (!items.length) { el.innerHTML = '<div class="empty"><div class="icon">&#128269;</div><p>No files match your search.</p></div>'; el.className=''; return; }
  if (currentView === 'grid') {
    el.className = 'grid-view';
    el.innerHTML = items.map((m,i) => cardHTML(m,i)).join('');
  } else {
    el.className = 'list-view';
    el.innerHTML = `<div class="list-row" style="cursor:default;pointer-events:none;opacity:.5;font-size:11px"><div></div><div>Name</div><div>Type</div><div>Dimensions</div><div>Size</div><div>Date</div></div>` + items.map((m,i)=>listRowHTML(m,i)).join('');
  }
}

function cardHTML(m, i) {
  const dims = m.width ? `${m.width}&times;${m.height}` : '';
  const date = (m.date_taken||m.date_added||'').slice(0,10);
  const tags = m.tags ? m.tags.split(', ').map(t=>`<span class="tag">${t}</span>`).join('') : '';
  const dup  = m.is_dup ? '<div class="dup-badge">DUP</div>' : '';
  const dur  = m.duration ? `<div class="vid-dur">${fmtDuration(m.duration)}</div>` : '';
  const thumb = m.media_type==='image'
    ? `<img src="/api/file/${m.id}" loading="lazy" onerror="this.style.display='none';this.nextSibling.style.display='block'" /><div class="icon" style="display:none">&#128444;</div>`
    : `<div class="icon">&#127909;</div>${dur}`;
  return `<div class="card" onclick="event.ctrlKey||event.metaKey?toggleSelect(${m.id},this):openModal(${m.id},${i})" title="Click to open · Ctrl+Click to select">
    <div class="card-thumb">${thumb}<div class="card-type-badge">.${m.extension}</div>${dup}</div>
    <div class="card-body">
      <div class="card-name" title="${m.file_name}">${m.file_name}</div>
      <div class="card-meta">${dims}${dims&&date?' &middot; ':''}${date}</div>
      ${tags?`<div class="card-tags">${tags}</div>`:''}
    </div>
  </div>`;
}

function listRowHTML(m, i) {
  const dims = m.width ? `${m.width}&times;${m.height}` : '&ndash;';
  const date = (m.date_taken||m.date_added||'').slice(0,10);
  const thumb = m.media_type==='image'
    ? `<img src="/api/file/${m.id}" loading="lazy" onerror="this.style.display='none';this.parentElement.textContent='&#128444;'" />`
    : '&#127909;';
  return `<div class="list-row" onclick="event.ctrlKey||event.metaKey?toggleSelect(${m.id},this):openModal(${m.id},${i})" title="Click to open · Ctrl+Click to select">
    <div class="list-thumb">${thumb}</div>
    <div><div class="list-name" title="${m.file_path}">${m.file_name}</div><div class="list-sub">${m.tags||''}</div></div>
    <div class="list-cell">${m.media_type}</div>
    <div class="list-cell">${dims}</div>
    <div class="list-cell">${fmtSize(m.file_size)}</div>
    <div class="list-cell">${date}</div>
  </div>`;
}

function renderPagination(total) {
  const pages = Math.ceil(total/PAGE_SIZE);
  const el = document.getElementById('pagination');
  if (pages<=1){el.innerHTML='';return;}
  el.innerHTML = Array.from({length:pages},(_,i)=>
    `<button class="page-btn ${i===currentPage?'active':''}" onclick="goPage(${i})">${i+1}</button>`).join('');
}
function goPage(n){currentPage=n;loadMedia();}

function applyFilters() {
  filters={};
  const v=id=>document.getElementById(id).value.trim();
  if(v('fType')) filters.media_type=v('fType');
  if(v('fFrom')) filters.date_from=v('fFrom');
  if(v('fTo'))   filters.date_to=v('fTo');
  if(v('fMinW')) filters.min_width=v('fMinW');
  if(v('fMinH')) filters.min_height=v('fMinH');
  if(v('fExt'))  filters.ext=v('fExt');
  if(v('fTags')) filters.tags=v('fTags');
  currentPage=0; loadMedia();
}
function clearFilters(){
  ['fType','fFrom','fTo','fMinW','fMinH','fExt','fTags'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('globalSearch').value='';
  filters={}; currentPage=0; loadMedia();
}
function filterByTag(tag){document.getElementById('fTags').value=tag; applyFilters();}
function setView(v){
  currentView=v;
  document.getElementById('btnGrid').classList.toggle('active',v==='grid');
  document.getElementById('btnList').classList.toggle('active',v==='list');
  loadMedia();
}
function switchTab(tab){
  currentTab=tab;
  document.getElementById('tabLibrary').style.display=tab==='library'?'flex':'none';
  document.getElementById('tabFolders').style.display=tab==='folders'?'grid':'none';
  document.getElementById('tabDuplicates').style.display=tab==='duplicates'?'block':'none';
  document.querySelectorAll('.tab').forEach((el,i)=>el.classList.toggle('active',
    (i===0&&tab==='library')||(i===1&&tab==='folders')||(i===2&&tab==='duplicates')));
  if(tab==='duplicates') loadDuplicates();
  if(tab==='folders') loadFolderTree();
}

// ── FOLDERS ───────────────────────────────────────────────────────────────────
let currentFolderId = null;

async function loadFolderTree() {
  const data = await api('/folders?tree=1');
  const el = document.getElementById('folderTree');
  if (!data.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:12px;padding:8px">No folders yet. Create one above.</div>';
    return;
  }
  el.innerHTML = data.map(f => {
    const indent = f.depth * 14;
    const active = f.id === currentFolderId ? 'border-color:var(--accent);background:var(--surface2)' : '';
    return `<div onclick="openFolder(${f.id})" style="padding:6px 8px 6px ${8+indent}px;cursor:pointer;border-radius:6px;border:1px solid transparent;margin-bottom:3px;font-size:13px;display:flex;align-items:center;gap:6px;${active}">
      <span style="color:var(--accent2)">&#128193;</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${f.name}</span>
      <span style="font-size:10px;color:var(--muted)">${f.media_count}</span>
    </div>`;
  }).join('');
}

async function openFolder(id) {
  currentFolderId = id;
  loadFolderTree(); // refresh to show active state
  const data = await api(`/folders/${id}`);
  // breadcrumb
  const crumb = data.breadcrumb.map((f,i) =>
    i < data.breadcrumb.length-1
      ? `<span onclick="openFolder(${f.id})" style="cursor:pointer;color:var(--accent2)">${f.name}</span> / `
      : `<b>${f.name}</b>`).join('');
  document.getElementById('folderBreadcrumb').innerHTML = crumb || data.name;
  ['btnNewSubfolder','btnRenameFolder','btnDeleteFolder'].forEach(id=>document.getElementById(id).style.display='');
  // render contents
  const el = document.getElementById('folderContent');
  if (!data.media.length && !data.children.length) {
    el.innerHTML = '<div class="empty"><div class="icon">&#128193;</div><p>This folder is empty. Assign files to it from the Library tab.</p></div>';
    return;
  }
  let html = '';
  if (data.children.length) {
    html += '<div style="margin-bottom:16px"><div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px">Sub-folders</div>';
    html += '<div style="display:flex;flex-wrap:wrap;gap:8px">';
    html += data.children.map(c => `<div onclick="openFolder(${c.id})" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 14px;cursor:pointer;display:flex;align-items:center;gap:8px;font-size:13px;transition:border-color .15s" onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--border)'">
      <span>&#128193;</span><span>${c.name}</span><span style="color:var(--muted);font-size:11px">${c.media_count}</span>
    </div>`).join('');
    html += '</div></div>';
  }
  if (data.media.length) {
    html += '<div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px">Files (' + data.media_count + ')</div>';
    html += '<div class="grid-view" style="padding:0;margin:0">';
    html += data.media.map((m,i) => {
      const dims = m.width ? `${m.width}&times;${m.height}` : '';
      const date = (m.date_taken||m.date_added||'').slice(0,10);
      const dup  = m.is_dup ? '<div class="dup-badge">DUP</div>' : '';
      const dur  = m.duration ? `<div class="vid-dur">${fmtDuration(m.duration)}</div>` : '';
      const thumb = m.media_type==='image'
        ? `<img src="/api/file/${m.id}" loading="lazy" onerror="this.style.display='none';this.nextSibling.style.display='block'" /><div class="icon" style="display:none">&#128444;</div>`
        : `<div class="icon">&#127909;</div>${dur}`;
      return `<div class="card" onclick="openModal(${m.id},-1)">
        <div class="card-thumb">${thumb}<div class="card-type-badge">.${m.extension}</div>${dup}
          <button onclick="event.stopPropagation();removeFromCurrentFolder(${m.id})" title="Remove from folder" style="position:absolute;bottom:4px;right:4px;background:rgba(0,0,0,.6);border:none;color:#fff;border-radius:4px;padding:2px 6px;font-size:10px;cursor:pointer">&#x2715;</button>
        </div>
        <div class="card-body">
          <div class="card-name" title="${m.file_name}">${m.file_name}</div>
          <div class="card-meta">${dims}${dims&&date?' &middot; ':''}${date}</div>
        </div>
      </div>`;
    }).join('');
    html += '</div>';
  }
  el.innerHTML = html;
}

async function removeFromCurrentFolder(mediaId) {
  if (!currentFolderId) return;
  await api(`/folders/${currentFolderId}/media`, 'DELETE', {media_ids:[mediaId]});
  toast('Removed from folder', 'success');
  openFolder(currentFolderId);
}

async function promptNewFolder(parentId) {
  const name = prompt(parentId ? 'Sub-folder name:' : 'New folder name:');
  if (!name) return;
  await api('/folders', 'POST', {name, parent_id: parentId});
  toast('Folder created', 'success');
  loadFolderTree();
  if (parentId) openFolder(parentId);
}

async function promptRenameFolder() {
  if (!currentFolderId) return;
  const f = await api(`/folders/${currentFolderId}`);
  const name = prompt('New name:', f.name);
  if (!name || name === f.name) return;
  await api(`/folders/${currentFolderId}`, 'PUT', {name});
  toast('Folder renamed', 'success');
  loadFolderTree();
  openFolder(currentFolderId);
}

async function confirmDeleteFolder() {
  if (!currentFolderId) return;
  const f = await api(`/folders/${currentFolderId}`);
  const msg = f.children && f.children.length
    ? `Delete "${f.name}" and all ${f.children.length} sub-folder(s)? Files will NOT be deleted.`
    : `Delete folder "${f.name}"? Files will NOT be deleted.`;
  if (!confirm(msg)) return;
  const r = await api(`/folders/${currentFolderId}`, 'DELETE', {recursive: true});
  if (r.error) { toast(r.error, 'error'); return; }
  toast('Folder deleted', 'success');
  currentFolderId = null;
  document.getElementById('folderBreadcrumb').textContent = 'Select a folder';
  ['btnNewSubfolder','btnRenameFolder','btnDeleteFolder'].forEach(id=>document.getElementById(id).style.display='none');
  document.getElementById('folderContent').innerHTML = '<div class="empty"><div class="icon">&#128193;</div><p>Select a folder from the left panel.</p></div>';
  loadFolderTree();
}

async function openModal(id, idx) {
  currentIndex = (idx !== undefined) ? idx : currentResults.findIndex(r=>r.id===id);
  const data = await api('/media/' + id);
  currentItem = data;
  document.getElementById('modalOverlay').style.display='flex';
  renderModal(data);
}
function renderModal(data) {
  document.getElementById('modalTitle').textContent = data.file_name;
  document.getElementById('modalSub').textContent   = data.file_path;
  const pa = document.getElementById('previewArea');
  if (data.media_type === 'image') {
    pa.innerHTML = `<img src="/api/file/${data.id}" alt="${data.file_name}" style="max-width:100%;max-height:440px;object-fit:contain"
      onerror="this.outerHTML='<div class=preview-msg>&#9888; Could not display this image. The file may have been moved or is an unsupported format.</div>'" />`;
  } else if (data.media_type === 'video') {
    const mimeMap={mp4:'video/mp4',mov:'video/quicktime',avi:'video/x-msvideo',mkv:'video/x-matroska',webm:'video/webm',m4v:'video/mp4',wmv:'video/x-ms-wmv'};
    const mime = mimeMap[data.extension.toLowerCase()] || 'video/mp4';
    pa.innerHTML = `<video controls preload="metadata" style="max-width:100%;max-height:440px;background:#000">
      <source src="/api/file/${data.id}" type="${mime}">
      <div class="preview-msg">&#9888; Your browser cannot play this video format.<br>Try Chrome or open the file directly.</div>
    </video>`;
  } else {
    pa.innerHTML = '<div class="preview-msg">No preview available</div>';
  }
  const meta = [
    ['Type',       `${data.media_type} (.${data.extension})`],
    ['Dimensions', data.width ? `${data.width} × ${data.height} px` : '–'],
    ['File size',  fmtSize(data.file_size)],
    ['Date taken', data.date_taken || '–'],
    ['Date added', (data.date_added||'').slice(0,10)],
    ['SHA-256',    data.sha256 ? data.sha256.slice(0,20)+'&hellip;' : '–'],
    ...(data.duration ? [['Duration', fmtDuration(data.duration)]] : []),
  ];
  document.getElementById('modalMeta').innerHTML = meta.map(([l,v])=>
    `<div class="meta-label">${l}</div><div class="meta-value">${v}</div>`).join('');
  renderModalTags(data.tags||[]);
  document.getElementById('descInput').value = data.description||'';
  // load folder list for assignment checkboxes
  api('/folders?tree=1').then(allFolders => renderModalFolders(data.folders||[], allFolders));
}
function prevItem(){if(currentIndex>0){currentIndex--;openModal(currentResults[currentIndex].id,currentIndex);}}
function nextItem(){if(currentIndex<currentResults.length-1){currentIndex++;openModal(currentResults[currentIndex].id,currentIndex);}}
function closeModal(){
  const vid=document.querySelector('#previewArea video');
  if(vid) vid.pause();
  document.getElementById('modalOverlay').style.display='none';
  currentItem=null;
}
function renderModalTags(tags){
  document.getElementById('modalTags').innerHTML=(tags||[]).map(t=>
    `<button class="tag-rm" onclick="removeTag('${t}')">${t} &#x2715;</button>`).join('')||'<span style="color:var(--muted);font-size:12px">No tags yet</span>';
}

function renderModalFolders(folders, allFolders){
  const el = document.getElementById('modalFolders');
  if (!el) return;
  const assigned = new Set((folders||[]).map(f=>f.id));
  el.innerHTML = allFolders.map(f =>
    `<label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer;padding:3px 0">
      <input type="checkbox" ${assigned.has(f.id)?'checked':''} onchange="toggleFolderAssign(${f.id},this.checked)" />
      ${'&nbsp;&nbsp;'.repeat(f.depth||0)}&#128193; ${f.name}
     </label>`).join('') || '<span style="color:var(--muted);font-size:12px">No folders yet</span>';
}

async function toggleFolderAssign(folderId, add) {
  if (!currentItem) return;
  if (add) {
    await api(`/folders/${folderId}/media`, 'POST', {media_ids:[currentItem.id]});
    toast('Added to folder','success');
  } else {
    await api(`/folders/${folderId}/media`, 'DELETE', {media_ids:[currentItem.id]});
    toast('Removed from folder','success');
  }
}
async function addTag(){
  const input=document.getElementById('newTagInput');
  const tag=input.value.trim().toLowerCase();
  if(!tag||!currentItem) return;
  await api(`/media/${currentItem.id}/tags`,'POST',{tags:[tag]});
  input.value=''; currentItem.tags=[...(currentItem.tags||[]),tag];
  renderModalTags(currentItem.tags); toast('Tag added','success');
}
async function removeTag(tag){
  if(!currentItem) return;
  await api(`/media/${currentItem.id}/tags`,'DELETE',{tags:[tag]});
  currentItem.tags=(currentItem.tags||[]).filter(t=>t!==tag);
  renderModalTags(currentItem.tags); toast('Tag removed','success');
}
async function saveDescription(){
  if(!currentItem) return;
  await api(`/media/${currentItem.id}/description`,'PUT',{description:document.getElementById('descInput').value});
  toast('Description saved','success');
}

async function loadDuplicates(){
  const mode=document.getElementById('dupMode').value;
  document.getElementById('dupContainer').innerHTML='<div class="loading"><div class="spinner"></div></div>';
  const data=await api('/duplicates?by='+mode);
  dupGroups=data.groups;
  const waste=dupGroups.reduce((a,g)=>a+g.slice(1).reduce((s,m)=>s+(m.file_size||0),0),0);
  const dup_files=dupGroups.reduce((a,g)=>a+g.length-1,0);
  document.getElementById('dupSummary').textContent=dupGroups.length
    ?`${dupGroups.length} duplicate group(s) — ${dup_files} redundant file(s), ~${fmtSize(waste)} reclaimable`
    :'No duplicates found.';
  document.getElementById('btnClean').disabled=!dupGroups.length;
  document.getElementById('dupContainer').innerHTML=dupGroups.map(g=>{
    const w=g.slice(1).reduce((s,m)=>s+(m.file_size||0),0);
    return `<div class="dup-group"><div class="dup-group-header"><div class="dup-group-info">${g.length} copies of &ldquo;${g[0].file_name}&rdquo;</div><div class="dup-waste">&minus;${fmtSize(w)}</div></div>${g.map((m,i)=>`<div class="dup-item"><span class="${i===0?'dup-keep':'dup-copy'}">${i===0?'KEEP':'DUP'}</span><span class="dup-path" title="${m.file_path}">${m.file_path}</span><span class="dup-size">${fmtSize(m.file_size)}</span><span style="font-size:11px;color:var(--muted)">${(m.date_taken||m.date_added||'').slice(0,10)}</span></div>`).join('')}</div>`;
  }).join('');
}
async function cleanDuplicates(){
  if(!confirm(`Remove ${dupGroups.reduce((a,g)=>a+g.length-1,0)} duplicate entries from the database?\n\nFiles on disk are NOT deleted.`)) return;
  const data=await api('/duplicates/clean','POST',{groups:dupGroups.map(g=>g.map(m=>m.id))});
  toast(`Removed ${data.deleted} duplicate entries`,'success');
  loadDuplicates(); loadStats(); loadMedia();
}

function fmtSize(n){if(!n)return'–';const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++;}return n.toFixed(1)+' '+u[i];}
function fmtDuration(s){if(!s)return'';const m=Math.floor(s/60),h=Math.floor(m/60);return h?`${h}:${String(m%60).padStart(2,'0')}:${String(Math.floor(s%60)).padStart(2,'0')}`:`${m}:${String(Math.floor(s%60)).padStart(2,'0')}`;}
function toast(msg,type='success'){const el=document.createElement('div');el.className=`toast ${type}`;el.textContent=msg;document.body.appendChild(el);setTimeout(()=>el.remove(),2800);}

// ── SELECTION (bulk ops) ──────────────────────────────────────────────────────
let selectedIds = new Set();
function toggleSelect(id, el) {
  if (selectedIds.has(id)) { selectedIds.delete(id); el.style.outline = ''; }
  else { selectedIds.add(id); el.style.outline = '2px solid var(--accent)'; }
  const bar = document.getElementById('diskOpsBar');
  bar.style.display = selectedIds.size ? 'flex' : 'none';
  document.getElementById('selCount').textContent = selectedIds.size + ' file' + (selectedIds.size!==1?'s':'');
}
function clearSelection() {
  selectedIds.clear();
  document.querySelectorAll('.card,.list-row').forEach(el => el.style.outline = '');
  document.getElementById('diskOpsBar').style.display = 'none';
}

// ── CONFIRMATION DIALOG ───────────────────────────────────────────────────────
let _confirmCallback = null;
function showConfirm(title, msg, path, extra, okLabel, callback) {
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmMsg').textContent = msg;
  const pathEl = document.getElementById('confirmPath');
  if (path) { pathEl.textContent = path; pathEl.style.display = 'block'; }
  else pathEl.style.display = 'none';
  document.getElementById('confirmExtra').innerHTML = extra || '';
  document.getElementById('confirmOkBtn').textContent = okLabel || 'Confirm';
  _confirmCallback = callback;
  document.getElementById('confirmOverlay').style.display = 'flex';
}
function confirmOk() {
  document.getElementById('confirmOverlay').style.display = 'none';
  if (_confirmCallback) _confirmCallback();
  _confirmCallback = null;
}
function confirmCancel() {
  document.getElementById('confirmOverlay').style.display = 'none';
  _confirmCallback = null;
}

// ── DISK OPS — FILE ───────────────────────────────────────────────────────────
function renameFilePrompt() {
  if (!currentItem) return;
  const cur = currentItem.file_name;
  showConfirm('Rename file on disk',
    'Enter a new filename. The original extension is kept if you omit it.',
    currentItem.file_path,
    '<input class="inp" id="renameInp" value="' + cur + '" style="margin-top:6px" />',
    'Rename',
    async () => {
      const newName = (document.getElementById('renameInp').value || '').trim();
      if (!newName || newName === cur) return;
      const r = await api('/media/' + currentItem.id + '/disk-rename', 'POST', {new_name: newName});
      if (r.error) { toast(r.error, 'error'); return; }
      toast('Renamed to "' + r.new_name + '"', 'success');
      currentItem.file_name = r.new_name; currentItem.file_path = r.new_path;
      document.getElementById('modalTitle').textContent = r.new_name;
      document.getElementById('modalSub').textContent = r.new_path;
      loadMedia();
    }
  );
  setTimeout(() => { const el = document.getElementById('renameInp'); if(el){el.focus();el.select();} }, 60);
}

function moveFilePrompt() {
  if (!currentItem) return;
  showConfirm('Move file on disk',
    'Enter the full destination folder path. The folder will be created if needed.',
    currentItem.file_path,
    '<input class="inp" id="moveInp" placeholder="e.g. Destination folder path" style="margin-top:6px" />',
    'Move',
    async () => {
      const dest = (document.getElementById('moveInp').value || '').trim();
      if (!dest) return;
      const r = await api('/media/' + currentItem.id + '/disk-move', 'POST', {dest_dir: dest});
      if (r.error) { toast(r.error, 'error'); return; }
      toast('File moved successfully', 'success');
      currentItem.file_path = r.new_path;
      document.getElementById('modalSub').textContent = r.new_path;
      loadMedia();
    }
  );
}

function deleteFilePrompt(permanent) {
  if (!currentItem) return;
  const title = permanent ? 'Delete permanently' : 'Send to Recycle Bin';
  const msg = permanent
    ? 'This file will be permanently deleted and CANNOT be recovered.'
    : 'This file will be moved to your Recycle Bin. You can restore it from there if needed.';
  showConfirm(title, msg, currentItem.file_path, null,
    permanent ? 'Delete Forever' : 'Send to Bin',
    async () => {
      const r = await api('/media/' + currentItem.id + '/disk-delete', 'DELETE', {permanent});
      if (r.error) { toast(r.error, 'error'); return; }
      toast(permanent ? 'File permanently deleted' : 'File sent to Recycle Bin', 'success');
      closeModal(); loadMedia(); loadStats();
    }
  );
}

// ── BULK OPS ──────────────────────────────────────────────────────────────────




// ── DISK OPS — FOLDER ────────────────────────────────────────────────────────




// ── DISK FILE OPERATIONS ──────────────────────────────────────────────────────

function diskRenameFile() {
  if (!currentItem) return;
  const oldName = currentItem.file_name;
  showConfirm(
    'Rename file on disk',
    'Enter a new filename. The file will be renamed on your hard drive.',
    currentItem.file_path,
    '<input class="inp" id="renameInp" value="' + oldName + '" style="margin-top:6px" />',
    'Rename',
    async () => {
      const newName = (document.getElementById('renameInp').value || '').trim();
      if (!newName || newName === oldName) return;
      const r = await api('/api/disk/rename', 'POST', {media_id: currentItem.id, new_name: newName});
      if (r.error) { toast(r.error, 'error'); return; }
      toast('File renamed to ' + newName, 'success');
      currentItem.file_name = newName;
      currentItem.file_path = r.new_path || currentItem.file_path;
      document.getElementById('modalTitle').textContent = newName;
      document.getElementById('modalSub').textContent = currentItem.file_path;
      loadMedia();
    }
  );
}

function diskMoveFile() {
  if (!currentItem) return;
  showConfirm(
    'Move file on disk',
    'Enter the destination folder path. The file will be physically moved on your hard drive.',
    currentItem.file_path,
    '<input class="inp" id="moveInp" placeholder="e.g. Destination folder path" style="margin-top:6px" />',
    'Move',
    async () => {
      const dest = (document.getElementById('moveInp').value || '').trim();
      if (!dest) return;
      const r = await api('/api/disk/move', 'POST', {media_id: currentItem.id, dest_folder: dest});
      if (r.error) { toast(r.error, 'error'); return; }
      toast('File moved successfully', 'success');
      currentItem.file_path = r.new_path || currentItem.file_path;
      document.getElementById('modalSub').textContent = currentItem.file_path;
      loadMedia();
    }
  );
}

function diskDeleteFile(permanent) {
  if (!currentItem) return;
  const label  = permanent ? 'Delete permanently' : 'Send to Recycle Bin';
  const detail = permanent
    ? 'This file will be PERMANENTLY deleted from your hard drive. This cannot be undone.'
    : 'This file will be sent to your Recycle Bin. You can recover it from there if needed.';
  showConfirm(
    label,
    detail,
    currentItem.file_path,
    '',
    label,
    async () => {
      const r = await api('/api/disk/delete', 'POST', {media_id: currentItem.id, permanent});
      if (r.error) { toast(r.error, 'error'); return; }
      toast(permanent ? 'File permanently deleted' : 'File sent to Recycle Bin', 'success');
      closeModal();
      loadMedia();
      loadStats();
    }
  );
}

function diskRenameFolderPrompt(folderPath) {
  if (!folderPath) return;
  const current = folderPath.replace(/\\/g,'/').split('/').filter(Boolean).pop() || folderPath;
  showConfirm(
    'Rename folder on disk',
    'Enter a new name for this folder. All files inside will be updated in the database automatically.',
    folderPath,
    '<input class="inp" id="renFolderInp" value="' + current + '" style="margin-top:6px" />',
    'Rename',
    async () => {
      const newName = (document.getElementById('renFolderInp').value || '').trim();
      if (!newName || newName === current) return;
      const r = await api('/api/disk/folder-rename', 'POST', {folder_path: folderPath, new_name: newName});
      if (r.error) { toast(r.error, 'error'); return; }
      toast('Folder renamed — ' + (r.db_updated||0) + ' paths updated in DB', 'success');
      loadFolderTree();
      loadMedia();
    }
  );
}

function diskDeleteFolderPrompt(folderPath) {
  if (!folderPath) return;
  showConfirm(
    'Delete folder on disk',
    'The folder and ALL its contents will be sent to the Recycle Bin, and removed from the database. This affects real files on your hard drive.',
    folderPath,
    '<label style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;cursor:pointer"><input type="checkbox" id="permDelFolderChk" /> Delete permanently instead of Recycle Bin</label>',
    'Delete Folder',
    async () => {
      const permanent = document.getElementById('permDelFolderChk').checked;
      const r = await api('/api/disk/folder-delete', 'POST', {folder_path: folderPath, permanent});
      if (r.error) { toast(r.error, 'error'); return; }
      toast('Folder deleted — ' + r.db_removed + ' files removed from DB', 'success');
      currentFolderId = null; loadFolderTree(); loadMedia(); loadStats();
    }
  );
}

function diskCreateFolderPrompt(parentPath) {
  showConfirm(
    'Create folder on disk',
    'Enter a name for the new folder. It will be created inside: ' + parentPath,
    parentPath,
    '<input class="inp" id="newFolderNameInp" placeholder="New folder name" style="margin-top:6px" />',
    'Create Folder',
    async () => {
      const name = (document.getElementById('newFolderNameInp').value || '').trim();
      if (!name) return;
      const r = await api('/api/disk/folder-create', 'POST', {parent_path: parentPath, name});
      if (r.error) { toast(r.error, 'error'); return; }
      toast('Folder created: ' + r.new_path, 'success');
    }
  );
}

function bulkMovePrompt() {
  if (!selectedIds.size) return;
  showConfirm(
    'Move ' + selectedIds.size + ' files on disk',
    'Enter the destination folder. All selected files will be physically moved there.',
    '',
    '<input class="inp" id="bulkMoveInp" placeholder="e.g. Destination folder path" style="margin-top:6px" />',
    'Move All',
    async () => {
      const dest = (document.getElementById('bulkMoveInp').value || '').trim();
      if (!dest) return;
      let ok = 0, fail = 0;
      for (const id of selectedIds) {
        const r = await api('/api/disk/move', 'POST', {media_id: id, dest_folder: dest});
        r.error ? fail++ : ok++;
      }
      toast('Moved ' + ok + ' files' + (fail ? ', ' + fail + ' failed' : ''), ok ? 'success' : 'error');
      clearSelection();
      loadMedia();
    }
  );
}

function bulkDeletePrompt() {
  if (!selectedIds.size) return;
  showConfirm(
    'Delete ' + selectedIds.size + ' files',
    'Selected files will be sent to the Recycle Bin and removed from the database.',
    '',
    '<label style="display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;cursor:pointer"><input type="checkbox" id="permBulkChk" /> Delete permanently instead</label>',
    'Delete Selected',
    async () => {
      const permanent = document.getElementById('permBulkChk').checked;
      let ok = 0, fail = 0;
      for (const id of selectedIds) {
        const r = await api('/api/disk/delete', 'POST', {media_id: id, permanent});
        r.error ? fail++ : ok++;
      }
      toast('Deleted ' + ok + ' files' + (fail ? ', ' + fail + ' failed' : ''), ok ? 'success' : 'error');
      clearSelection();
      loadMedia();
      loadStats();
    }
  );
}


init();
</script>
</body>
</html>"""

    conn = init_db(db_path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # suppress default logging

        def send_json(self, data, status=200):
            body = json.dumps(data, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs     = urllib.parse.parse_qs(parsed.query)
            p      = parsed.path

            # Root → serve HTML
            if p in ("/", ""):
                body = HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
                return

            # ── /api/stats ────────────────────────────────────────────────────
            if p == "/api/stats":
                total  = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
                images = conn.execute("SELECT COUNT(*) FROM media WHERE media_type='image'").fetchone()[0]
                videos = conn.execute("SELECT COUNT(*) FROM media WHERE media_type='video'").fetchone()[0]
                tags   = conn.execute("SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
                dups   = conn.execute("""
                    SELECT COUNT(*) FROM (
                        SELECT sha256 FROM media WHERE sha256 IS NOT NULL
                        GROUP BY sha256 HAVING COUNT(*)>1)""").fetchone()[0]
                self.send_json({"total":total,"images":images,"videos":videos,"tags":tags,"dup_groups":dups})
                return

            # ── /api/tags ─────────────────────────────────────────────────────
            if p == "/api/tags":
                rows = conn.execute("""
                    SELECT tag, COUNT(*) AS cnt FROM tags
                    GROUP BY tag ORDER BY cnt DESC LIMIT 50""").fetchall()
                self.send_json([dict(r) for r in rows])
                return

            # ── /api/folders  (list tree) ─────────────────────────────────────
            if p == "/api/folders":
                pid = qs.get("parent_id", [None])[0]
                parent_id = int(pid) if pid and pid != "null" else None
                if qs.get("tree"):
                    self.send_json(folder_tree(conn))
                else:
                    self.send_json(list_folders(conn, parent_id))
                return

            # ── /api/folders/<id> ─────────────────────────────────────────────
            if p.startswith("/api/folders/") and p.count("/") == 3:
                fid = int(p.split("/")[-1])
                f = get_folder(conn, fid)
                if not f:
                    self.send_json({"error":"not found"}, 404); return
                f["children"]    = list_folders(conn, fid)
                f["breadcrumb"]  = folder_breadcrumb(conn, fid)
                g2 = lambda k: qs.get(k, [None])[0]
                f["media"]       = get_folder_media(conn, fid,
                    sort_by  = g2("sort_by") or "date_taken",
                    sort_dir = g2("sort_dir") or "desc",
                    limit    = int(g2("limit") or 200),
                    offset   = int(g2("offset") or 0))
                f["media_count"] = conn.execute("SELECT COUNT(*) FROM folder_media WHERE folder_id=?", (fid,)).fetchone()[0]
                # mark dups
                dup_hashes = set(r[0] for r in conn.execute("SELECT sha256 FROM media WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING COUNT(*)>1").fetchall())
                for m in f["media"]:
                    m["is_dup"] = bool(m.get("sha256") and m["sha256"] in dup_hashes)
                self.send_json(f)
                return

            # ── /api/search ───────────────────────────────────────────────────
            if p == "/api/search":
                g = lambda k: qs.get(k, [None])[0]
                tags_val = g("tags")
                tag_list = [t.strip() for t in tags_val.split()] if tags_val else None
                fid_val  = g("folder_id")
                results = search(
                    conn,
                    query      = g("query") or None,
                    media_type = g("media_type") or None,
                    tags       = tag_list,
                    date_from  = g("date_from") or None,
                    date_to    = g("date_to") or None,
                    min_width  = int(g("min_width")) if g("min_width") else None,
                    min_height = int(g("min_height")) if g("min_height") else None,
                    ext        = g("ext") or None,
                    folder_id  = int(fid_val) if fid_val else None,
                    sort_by    = g("sort_by") or "date_taken",
                    sort_dir   = g("sort_dir") or "desc",
                    limit      = int(g("limit") or 48),
                    offset     = int(g("offset") or 0),
                )
                # Mark duplicates
                dup_hashes = set(r[0] for r in conn.execute("""
                    SELECT sha256 FROM media WHERE sha256 IS NOT NULL
                    GROUP BY sha256 HAVING COUNT(*)>1""").fetchall())
                for r in results:
                    r["is_dup"] = bool(r.get("sha256") and r["sha256"] in dup_hashes)
                total = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
                self.send_json({"results": results, "total": total})
                return

            # ── /api/media/<id> ───────────────────────────────────────────────
            if p.startswith("/api/media/") and p.count("/") == 3:
                mid = int(p.split("/")[-1])
                row = conn.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error":"not found"}, 404); return
                data = dict(row)
                data["tags"]    = get_tags(conn, mid)
                data["folders"] = get_media_folders(conn, mid)
                self.send_json(data)
                return

            # ── /api/file/<id>  — stream actual file bytes ────────────────────
            if p.startswith("/api/file/"):
                try:
                    mid = int(p.split("/")[-1])
                except ValueError:
                    self.send_json({"error":"bad id"}, 400); return
                row = conn.execute("SELECT file_path, extension, media_type FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error":"not found"}, 404); return
                fpath = row["file_path"]
                if not os.path.isfile(fpath):
                    # Try case-insensitive match on Windows
                    parent = os.path.dirname(fpath)
                    fname  = os.path.basename(fpath)
                    found  = None
                    if os.path.isdir(parent):
                        for f in os.listdir(parent):
                            if f.lower() == fname.lower():
                                found = os.path.join(parent, f)
                                break
                    if found:
                        fpath = found
                    else:
                        self.send_json({"error": f"file not found on disk: {fpath}"}, 404); return
                ext_lower = row["extension"].lower()
                mime_map = {
                    "jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png",
                    "gif":"image/gif","bmp":"image/bmp","webp":"image/webp",
                    "tiff":"image/tiff","heic":"image/heif",
                    "mp4":"video/mp4","mov":"video/quicktime","avi":"video/x-msvideo",
                    "mkv":"video/x-matroska","webm":"video/webm","m4v":"video/mp4",
                    "flv":"video/x-flv","wmv":"video/x-ms-wmv",
                }
                mime = mime_map.get(ext_lower, "application/octet-stream")
                file_size = os.path.getsize(fpath)

                # Support Range requests for video seeking
                range_header = self.headers.get("Range")
                if range_header and range_header.startswith("bytes="):
                    try:
                        byte_range = range_header[6:].split("-")
                        start = int(byte_range[0]) if byte_range[0] else 0
                        end   = int(byte_range[1]) if byte_range[1] else file_size - 1
                        end   = min(end, file_size - 1)
                        length = end - start + 1
                        self.send_response(206)
                        self.send_header("Content-Type", mime)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                        self.send_header("Content-Length", length)
                        self.send_header("Accept-Ranges", "bytes")
                        self.end_headers()
                        with open(fpath, "rb") as f:
                            f.seek(start)
                            remaining = length
                            while remaining:
                                chunk = f.read(min(65536, remaining))
                                if not chunk: break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        return
                    except Exception:
                        pass

                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", file_size)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(fpath, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk: break
                        self.wfile.write(chunk)
                return

            # ── /api/duplicates ───────────────────────────────────────────────
            if p == "/api/duplicates":
                by = qs.get("by", ["hash"])[0]
                groups = find_duplicates(conn, by=by)
                self.send_json({"groups": groups})
                return

            self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            p      = self.path

            # ── /api/folders  (create) ───────────────────────────────────────
            if p == "/api/folders":
                pid = body.get("parent_id")
                fid = create_folder(conn, body.get("name","New Folder"), parent_id=pid, description=body.get("description",""))
                self.send_json({"id": fid, "ok": True})
                return

            # ── /api/folders/<id>/media  (add files to folder) ────────────────
            if p.startswith("/api/folders/") and p.endswith("/media"):
                fid = int(p.split("/")[-2])
                add_to_folder(conn, fid, body.get("media_ids", []))
                self.send_json({"ok": True})
                return

            # ── /api/media/<id>/tags ──────────────────────────────────────────
            if p.endswith("/tags"):
                mid = int(p.split("/")[-2])
                add_tags(conn, mid, body.get("tags", []))
                self.send_json({"ok": True})
                return

            # ── /api/duplicates/clean ─────────────────────────────────────────
            if p == "/api/duplicates/clean":
                id_groups = body.get("groups", [])
                deleted = 0
                for grp in id_groups:
                    for mid in grp[1:]:
                        conn.execute("DELETE FROM media WHERE id=?", (mid,))
                        deleted += 1
                conn.commit()
                self.send_json({"deleted": deleted})
                return

            # ── /api/media/<id>/disk-rename ───────────────────────────────────
            if p.endswith("/disk-rename"):
                mid = int(p.split("/")[-2])
                row = conn.execute("SELECT file_path FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error": "not found"}, 404); return
                new_name = body.get("new_name", "").strip()
                if not new_name:
                    self.send_json({"error": "new_name required"}, 400); return
                try:
                    old_path = row["file_path"]
                    new_path = disk_rename_file(old_path, new_name)
                    conn.execute("UPDATE media SET file_path=?, file_name=? WHERE id=?",
                                 (new_path, Path(new_path).name, mid))
                    conn.commit()
                    self.send_json({"ok": True, "new_path": new_path, "new_name": Path(new_path).name})
                except (FileNotFoundError, FileExistsError, ValueError) as e:
                    self.send_json({"error": str(e)}, 400)
                return

            # ── /api/media/<id>/disk-move ─────────────────────────────────────
            if p.endswith("/disk-move"):
                mid = int(p.split("/")[-2])
                row = conn.execute("SELECT file_path FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error": "not found"}, 404); return
                dest_dir = body.get("dest_dir", "").strip()
                if not dest_dir:
                    self.send_json({"error": "dest_dir required"}, 400); return
                try:
                    old_path = row["file_path"]
                    new_path = disk_move_file(old_path, dest_dir)
                    conn.execute("UPDATE media SET file_path=?, file_name=? WHERE id=?",
                                 (new_path, Path(new_path).name, mid))
                    conn.commit()
                    self.send_json({"ok": True, "new_path": new_path})
                except (FileNotFoundError, FileExistsError) as e:
                    self.send_json({"error": str(e)}, 400)
                return

            # ── /api/disk/folder-rename ───────────────────────────────────────
            if p == "/api/disk/folder-rename":
                old_path = body.get("old_path", "").strip()
                new_name = body.get("new_name", "").strip()
                if not old_path or not new_name:
                    self.send_json({"error": "old_path and new_name required"}, 400); return
                try:
                    new_path = disk_rename_folder(old_path, new_name)
                    updated  = db_update_paths(conn, old_path, new_path)
                    self.send_json({"ok": True, "new_path": new_path, "db_updated": updated})
                except (FileNotFoundError, FileExistsError, ValueError) as e:
                    self.send_json({"error": str(e)}, 400)
                return

            # ── /api/disk/folder-create ───────────────────────────────────────
            if p == "/api/disk/folder-create":
                parent_path = body.get("parent_path", "").strip()
                name        = body.get("name", "").strip()
                if not parent_path or not name:
                    self.send_json({"error": "parent_path and name required"}, 400); return
                try:
                    new_path = disk_create_folder(parent_path, name)
                    self.send_json({"ok": True, "new_path": new_path})
                except FileExistsError as e:
                    self.send_json({"error": str(e)}, 400)
                return

            # ── /api/disk/folder-delete ───────────────────────────────────────
            if p == "/api/disk/folder-delete":
                folder_path = body.get("folder_path", "").strip()
                permanent   = body.get("permanent", False)
                if not folder_path:
                    self.send_json({"error": "folder_path required"}, 400); return
                try:
                    disk_delete_folder(folder_path, permanent=permanent)
                    rows = conn.execute(
                        "SELECT id FROM media WHERE file_path LIKE ?",
                        (folder_path.rstrip(os.sep) + "%",)
                    ).fetchall()
                    for r in rows:
                        conn.execute("DELETE FROM media WHERE id=?", (r["id"],))
                    conn.commit()
                    self.send_json({"ok": True, "db_removed": len(rows)})
                except (FileNotFoundError, RuntimeError) as e:
                    self.send_json({"error": str(e)}, 400)
                return

            self.send_json({"error": "not found"}, 404)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            p      = self.path

            # ── /api/folders/<id>  (rename / move) ───────────────────────────
            if p.startswith("/api/folders/") and p.count("/") == 3:
                fid = int(p.split("/")[-1])
                if "name" in body:
                    rename_folder(conn, fid, body["name"])
                if "parent_id" in body:
                    new_pid = body["parent_id"]
                    move_folder(conn, fid, new_pid if new_pid != "" else None)
                self.send_json({"ok": True})
                return

            # ── /api/media/<id>/description ───────────────────────────────────
            if p.endswith("/description"):
                mid = int(p.split("/")[-2])
                set_description(conn, mid, body.get("description", ""))
                self.send_json({"ok": True})
                return

            self.send_json({"error": "not found"}, 404)

        def do_DELETE(self):
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            p      = self.path

            if p.endswith("/tags"):
                mid = int(p.split("/")[-2])
                remove_tags(conn, mid, body.get("tags", []))
                self.send_json({"ok": True})
                return

            # ── /api/folders/<id>  (delete folder) ───────────────────────────
            if p.startswith("/api/folders/") and p.count("/") == 3:
                fid = int(p.split("/")[-1])
                try:
                    delete_folder(conn, fid, recursive=body.get("recursive", False))
                    self.send_json({"ok": True})
                except ValueError as e:
                    self.send_json({"error": str(e)}, 400)
                return

            # ── /api/folders/<id>/media  (remove files from folder) ──────────
            if p.startswith("/api/folders/") and p.endswith("/media"):
                fid = int(p.split("/")[-2])
                remove_from_folder(conn, fid, body.get("media_ids", []))
                self.send_json({"ok": True})
                return

            # ── /api/media/<id>/disk-delete  (Recycle Bin or permanent) ───────
            if p.endswith("/disk-delete"):
                mid = int(p.split("/")[-2])
                row = conn.execute("SELECT file_path FROM media WHERE id=?", (mid,)).fetchone()
                if not row:
                    self.send_json({"error": "not found"}, 404); return
                permanent = body.get("permanent", False)
                try:
                    disk_delete_file(row["file_path"], permanent=permanent)
                    conn.execute("DELETE FROM media WHERE id=?", (mid,))
                    conn.commit()
                    self.send_json({"ok": True, "permanent": permanent})
                except (FileNotFoundError, RuntimeError) as e:
                    self.send_json({"error": str(e)}, 400)
                return

            self.send_json({"error": "not found"}, 404)

    server = http.server.HTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    if not TRASH_AVAILABLE:
        print("  WARNING: send2trash not installed. Run: pip install send2trash")
        print("           Recycle Bin deletion will not work without it.")
    print(f"\n  📁 Media Library UI running at {url}")
    print(f"  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def build_parser():
    p = argparse.ArgumentParser(
        prog="media_database",
        description="📁 Media Database – organise & search images/videos by metadata")
    p.add_argument("--db", default=DB_FILE, help=f"SQLite DB path (default: {DB_FILE})")
    sub = p.add_subparsers(dest="cmd", required=True)

    # ingest
    ing = sub.add_parser("ingest", help="Scan and import media files")
    ing.add_argument("paths", nargs="+", help="Files or directories to scan")
    ing.add_argument("--no-recursive", action="store_true")
    ing.add_argument("--force", action="store_true")
    ing.add_argument("--mirror-folders", action="store_true", dest="mirror_folders",
                     help="Mirror real disk folder structure into virtual folders")

    # search
    s = sub.add_parser("search", help="Search the database")
    s.add_argument("query", nargs="?")
    s.add_argument("--type", choices=["image", "video"], dest="media_type")
    s.add_argument("--tags", nargs="+")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to",   dest="date_to")
    s.add_argument("--min-width",  type=int)
    s.add_argument("--min-height", type=int)
    s.add_argument("--ext")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("-v", "--verbose", action="store_true")

    # tag
    t = sub.add_parser("tag", help="Add or remove tags")
    t.add_argument("id", type=int)
    t.add_argument("--add",    nargs="+", metavar="TAG")
    t.add_argument("--remove", nargs="+", metavar="TAG")

    # describe
    d = sub.add_parser("describe", help="Set a description on a media item")
    d.add_argument("id", type=int)
    d.add_argument("description")

    # info
    i = sub.add_parser("info", help="Show full details for a media item")
    i.add_argument("id", type=int)

    # stats
    sub.add_parser("stats", help="Show library statistics")

    # list-tags
    sub.add_parser("list-tags", help="List all tags and their counts")

    # duplicates
    dup = sub.add_parser("duplicates", help="Find duplicate files")
    dup.add_argument("--by", choices=["hash","size","name"], default="hash",
                     help="Matching strategy (default: hash)")
    dup.add_argument("--clean", action="store_true",
                     help="Remove duplicate DB entries (keeps oldest; dry-run by default)")
    dup.add_argument("--yes", action="store_true",
                     help="Actually perform deletion (skip dry-run)")
    dup.add_argument("--keep", choices=["oldest","newest","largest"], default="oldest")

    # export
    ex = sub.add_parser("export", help="Export search results to JSON or CSV")
    ex.add_argument("--format", choices=["json", "csv"], default="json")
    ex.add_argument("--output", default="export.json")
    ex.add_argument("--type",  choices=["image", "video"], dest="media_type")
    ex.add_argument("--tags",  nargs="+")
    ex.add_argument("--from",  dest="date_from")
    ex.add_argument("--to",    dest="date_to")

    # folders
    fol = sub.add_parser("folders", help="Manage virtual folders")
    fol_sub = fol.add_subparsers(dest="folder_cmd", required=True)

    fc = fol_sub.add_parser("create", help="Create a folder")
    fc.add_argument("name")
    fc.add_argument("--parent", type=int, default=None, dest="parent_id", help="Parent folder ID")
    fc.add_argument("--description", default="")

    fl = fol_sub.add_parser("list", help="List folders")
    fl.add_argument("--parent", type=int, default=None, dest="parent_id")

    fol_sub.add_parser("tree", help="Print full folder tree")

    fr = fol_sub.add_parser("rename", help="Rename a folder")
    fr.add_argument("id", type=int)
    fr.add_argument("name")

    fd = fol_sub.add_parser("delete", help="Delete a folder")
    fd.add_argument("id", type=int)
    fd.add_argument("--recursive", action="store_true")

    fa = fol_sub.add_parser("add", help="Add files to a folder")
    fa.add_argument("folder_id", type=int)
    fa.add_argument("media_ids", nargs="+", type=int)

    frm = fol_sub.add_parser("remove", help="Remove files from a folder")
    frm.add_argument("folder_id", type=int)
    frm.add_argument("media_ids", nargs="+", type=int)

    fsh = fol_sub.add_parser("show", help="Show folder contents")
    fsh.add_argument("id", type=int)

    # ui
    ui = sub.add_parser("ui", help="Launch the web UI")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=7432)

    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()
    conn   = init_db(args.db)

    if args.cmd == "ingest":
        for path in args.paths:
            if os.path.isfile(path):
                result = ingest_file(conn, path, force=args.force)
                print(f"  {result}: {path}")
            elif os.path.isdir(path):
                print(f"Scanning {path} …")
                counts = ingest_directory(conn, path,
                                          recursive=not args.no_recursive,
                                          force=args.force,
                                          mirror_folders=getattr(args, "mirror_folders", False))
                print(f"Done → {counts}")
            else:
                print(f"  Not found: {path}")

    elif args.cmd == "search":
        results = search(conn,
                         query=args.query,
                         media_type=args.media_type,
                         tags=args.tags,
                         date_from=args.date_from,
                         date_to=args.date_to,
                         min_width=args.min_width,
                         min_height=args.min_height,
                         ext=args.ext,
                         limit=args.limit)
        print(f"\nFound {len(results)} result(s):\n")
        print_results(results, verbose=args.verbose)

    elif args.cmd == "tag":
        if args.add:
            add_tags(conn, args.id, args.add)
            print(f"  Added tags to #{args.id}: {args.add}")
        if args.remove:
            remove_tags(conn, args.id, args.remove)
            print(f"  Removed tags from #{args.id}: {args.remove}")
        current = get_tags(conn, args.id)
        print(f"  Current tags: {current}")

    elif args.cmd == "describe":
        set_description(conn, args.id, args.description)
        print(f"  Description set for #{args.id}")

    elif args.cmd == "info":
        row = conn.execute("SELECT * FROM media WHERE id=?", (args.id,)).fetchone()
        if not row:
            print(f"  No media with id {args.id}")
        else:
            r = dict(row)
            tags = get_tags(conn, args.id)
            extra = json.loads(r.get("extra_meta") or "{}")
            print(f"\n{'─'*60}")
            for k, v in [
                ("ID", r['id']), ("File", r['file_name']), ("Path", r['file_path']),
                ("Type", f"{r['media_type']} (.{r['extension']})"),
                ("Size", fmt_size(r['file_size'])),
                ("Dimensions", f"{r['width']}×{r['height']}" if r['width'] else "?"),
                ("Date Taken", r['date_taken'] or "(unknown)"),
                ("Date Added", r['date_added']),
                ("Tags", ', '.join(tags) or "(none)"),
                ("Description", r['description'] or "(none)"),
                ("SHA-256", r['sha256']),
            ]:
                print(f"  {k:<12}: {v}")
            if extra:
                print(f"  Extra meta : {json.dumps(extra, indent=14)[1:]}")
            print(f"{'─'*60}\n")

    elif args.cmd == "stats":
        stats(conn)

    elif args.cmd == "list-tags":
        rows = conn.execute("""
            SELECT tag, COUNT(*) AS cnt FROM tags
            GROUP BY tag ORDER BY cnt DESC
        """).fetchall()
        print(f"\n  {'TAG':<30} COUNT")
        print(f"  {'─'*35}")
        for r in rows:
            print(f"  {r['tag']:<30} {r['cnt']}")
        print()

    elif args.cmd == "duplicates":
        groups = find_duplicates(conn, by=args.by)
        print_duplicates(groups, args.by)
        if args.clean:
            dry = not args.yes
            if dry:
                print("  ── DRY RUN (pass --yes to actually delete) ──")
            n = delete_duplicates(conn, groups, keep=args.keep, dry_run=dry)
            if not dry:
                print(f"  Removed {n} duplicate DB entries.")

    elif args.cmd == "export":
        results = search(conn,
                         media_type=args.media_type,
                         tags=args.tags,
                         date_from=args.date_from,
                         date_to=args.date_to,
                         limit=10_000)
        if args.format == "json":
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2, default=str)
        else:
            import csv
            if results:
                with open(args.output, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=results[0].keys())
                    w.writeheader()
                    w.writerows(results)
        print(f"  Exported {len(results)} records to {args.output}")

    elif args.cmd == "folders":
        fc = args.folder_cmd
        if fc == "create":
            fid = create_folder(conn, args.name, parent_id=args.parent_id, description=args.description)
            print(f"  Created folder #{fid}: {args.name}")
        elif fc == "list":
            rows = list_folders(conn, parent_id=args.parent_id)
            for f in rows:
                print(f"  [{f['id']:>4}]  {f['name']:<30}  {f['media_count']} files  {f['child_count']} sub-folders")
        elif fc == "tree":
            print_folder_tree(conn)
        elif fc == "rename":
            rename_folder(conn, args.id, args.name)
            print(f"  Renamed folder #{args.id} to '{args.name}'")
        elif fc == "delete":
            try:
                delete_folder(conn, args.id, recursive=args.recursive)
                print(f"  Deleted folder #{args.id}")
            except ValueError as e:
                print(f"  Error: {e}")
        elif fc == "add":
            add_to_folder(conn, args.folder_id, args.media_ids)
            print(f"  Added {len(args.media_ids)} file(s) to folder #{args.folder_id}")
        elif fc == "remove":
            remove_from_folder(conn, args.folder_id, args.media_ids)
            print(f"  Removed {len(args.media_ids)} file(s) from folder #{args.folder_id}")
        elif fc == "show":
            f = get_folder(conn, args.id)
            if not f:
                print(f"  No folder with id {args.id}")
            else:
                crumbs = " / ".join(x["name"] for x in folder_breadcrumb(conn, args.id))
                print(f"\n  📁 {crumbs}")
                print(f"  Description: {f['description'] or '(none)'}")
                children = list_folders(conn, args.id)
                if children:
                    print(f"  Sub-folders ({len(children)}):")
                    for c in children:
                        print(f"    [{c['id']:>4}]  {c['name']:<25}  {c['media_count']} files")
                media = get_folder_media(conn, args.id)
                print(f"  Files ({len(media)}):")
                print_results(media)

    elif args.cmd == "ui":
        conn.close()
        run_web_ui(args.db, host=args.host, port=args.port)
        return

    conn.close()


if __name__ == "__main__":
    main()
