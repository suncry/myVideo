from __future__ import annotations

import argparse
import ctypes
import difflib
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import certifi
import player

# A standalone macOS bundle has no Python installation providing CA files.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
if os.environ.get("YINGKU_DATA_DIR"):
    DATA_DIR = Path(os.environ["YINGKU_DATA_DIR"]).expanduser().resolve()
elif sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library" / "Application Support" / "YingKu"
elif sys.platform == "win32":
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / 'AppData' / 'Local') / 'YingKu'
elif getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", APP_DIR)) / "YingKu"
else:
    DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "film_library.db"
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".flv", ".webm",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".rm", ".rmvb",
}
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "appdata", ".git", "node_modules",
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3's context manager, then release the file handle."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                title TEXT NOT NULL,
                original_title TEXT DEFAULT '',
                year INTEGER,
                media_type TEXT DEFAULT 'movie',
                overview TEXT DEFAULT '',
                genres TEXT DEFAULT '[]',
                poster_url TEXT DEFAULT '',
                backdrop_url TEXT DEFAULT '',
                local_poster TEXT DEFAULT '',
                cast_json TEXT DEFAULT '[]',
                screenshots_json TEXT DEFAULT '[]',
                screenshots_status TEXT DEFAULT '',
                screenshots_attempted_at TEXT DEFAULT '',
                duration_seconds REAL DEFAULT 0,
                source TEXT DEFAULT 'local',
                source_id TEXT DEFAULT '',
                match_status TEXT DEFAULT 'unmatched',
                match_confidence REAL DEFAULT 0,
                last_match_attempt TEXT DEFAULT '',
                match_note TEXT DEFAULT '',
                external_rating REAL,
                personal_rating REAL DEFAULT 0,
                favorite INTEGER DEFAULT 0,
                play_count INTEGER DEFAULT 0,
                last_played_at TEXT DEFAULT '',
                watch_status TEXT DEFAULT 'unwatched',
                disposition TEXT DEFAULT 'keep',
                notes TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                file_size INTEGER DEFAULT 0,
                modified_at REAL DEFAULT 0,
                fingerprint TEXT DEFAULT '',
                drive TEXT DEFAULT '',
                exists_now INTEGER DEFAULT 1,
                hidden_by_app INTEGER DEFAULT 0,
                original_file_attributes INTEGER DEFAULT -1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_movies_title ON movies(title);
            CREATE INDEX IF NOT EXISTS idx_movies_fingerprint ON movies(fingerprint);
            CREATE INDEX IF NOT EXISTS idx_movies_drive ON movies(drive);
            CREATE TABLE IF NOT EXISTS privacy_exclusions (path_hash TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_roots (
                path TEXT PRIMARY KEY,
                last_scanned_at TEXT,
                enabled INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS hidden_folders (
                path TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                original_file_attributes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actor_profiles (
                name_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                avatar_url TEXT DEFAULT '',
                source TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                aliases_json TEXT DEFAULT '[]',
                biography TEXT DEFAULT '',
                info_json TEXT DEFAULT '{}',
                source_refs_json TEXT DEFAULT '{}',
                status TEXT DEFAULT 'new',
                last_attempt TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actor_photos (
                id INTEGER PRIMARY KEY,
                name_key TEXT NOT NULL,
                photo_url TEXT NOT NULL,
                source TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                selected INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE(name_key,photo_url),
                FOREIGN KEY(name_key) REFERENCES actor_profiles(name_key) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_actor_photos_name ON actor_photos(name_key,selected DESC,id);
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(movies)")}
        if "file_status" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN file_status TEXT DEFAULT 'available'")
            conn.execute("UPDATE movies SET file_status='trashed' WHERE exists_now=0")
        if "screenshots_json" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN screenshots_json TEXT DEFAULT '[]'")
        if "screenshots_status" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN screenshots_status TEXT DEFAULT ''")
        if "screenshots_attempted_at" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN screenshots_attempted_at TEXT DEFAULT ''")
        if "duration_seconds" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN duration_seconds REAL DEFAULT 0")
        if "play_count" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN play_count INTEGER DEFAULT 0")
        if "last_played_at" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN last_played_at TEXT DEFAULT ''")
        if "match_confidence" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN match_confidence REAL DEFAULT 0")
        if "last_match_attempt" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN last_match_attempt TEXT DEFAULT ''")
        if "match_note" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN match_note TEXT DEFAULT ''")
        if "hidden_by_app" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN hidden_by_app INTEGER DEFAULT 0")
        if "original_file_attributes" not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN original_file_attributes INTEGER DEFAULT -1")
        actor_columns = {row["name"] for row in conn.execute("PRAGMA table_info(actor_profiles)")}
        if "display_name" not in actor_columns:
            conn.execute("ALTER TABLE actor_profiles ADD COLUMN display_name TEXT DEFAULT ''")
        if "aliases_json" not in actor_columns:
            conn.execute("ALTER TABLE actor_profiles ADD COLUMN aliases_json TEXT DEFAULT '[]'")
        if "biography" not in actor_columns:
            conn.execute("ALTER TABLE actor_profiles ADD COLUMN biography TEXT DEFAULT ''")
        if "info_json" not in actor_columns:
            conn.execute("ALTER TABLE actor_profiles ADD COLUMN info_json TEXT DEFAULT '{}'")
        if "source_refs_json" not in actor_columns:
            conn.execute("ALTER TABLE actor_profiles ADD COLUMN source_refs_json TEXT DEFAULT '{}'")

    with connect() as conn:
        for column, declaration in [('favorite', 'INTEGER DEFAULT 0'), ('retained', 'INTEGER DEFAULT 0'), ('insights_json', "TEXT DEFAULT '{}'" )]:
            if column not in actor_columns:
                conn.execute(f"ALTER TABLE actor_profiles ADD COLUMN {column} {declaration}")
        if 'insights_json' not in columns:
            conn.execute("ALTER TABLE movies ADD COLUMN insights_json TEXT DEFAULT '{}'")


def json_value(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def movie_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["favorite"] = bool(item["favorite"])
    item["exists_now"] = bool(item["exists_now"])
    item["genres"] = json_value(item.get("genres"), [])
    item["tags"] = json_value(item.get("tags"), [])
    item["cast"] = json_value(item.pop("cast_json", "[]"), [])
    item["screenshots"] = json_value(item.pop("screenshots_json", "[]"), [])
    item["size_label"] = human_size(item.get("file_size", 0))
    item["poster"] = poster_for(item)
    return item


def poster_for(item: dict[str, Any]) -> str:
    local = item.get("local_poster", "")
    if local and Path(local).is_file():
        return "/api/local-image?path=" + urllib.parse.quote(local)
    return item.get("poster_url", "") or ""


def human_size(value: int | float) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


RELEASE_WORDS = re.compile(
    r"\b(?:2160p|1080p|720p|480p|4k|uhd|bluray|blu-ray|bdrip|brrip|web-?dl|webrip|"
    r"hdtv|dvdrip|remux|x26[45]|h\.26[45]|hevc|avc|aac\d*|dts(?:-hd)?|atmos|"
    r"hdr10?\+?|dolby[ ._-]?vision|proper|repack|extended|uncut|multi|dual[ ._-]?audio)\b",
    re.IGNORECASE,
)


def clean_filename(path: Path) -> tuple[str, int | None]:
    name = path.stem
    name = re.sub(r"[\[【(（].{0,80}?[\]】)）]", " ", name)
    year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", name)
    year = int(year_match.group(1)) if year_match else None
    if year_match:
        name = name[: year_match.start()] + " " + name[year_match.end() :]
    name = RELEASE_WORDS.sub(" ", name)
    name = re.sub(r"\b(?:S\d{1,2}E\d{1,3}|EP?\d{1,3}|CD\d|DISC\d|PART\d)\b", " ", name, flags=re.I)
    name = re.sub(r"[._]+", " ", name)
    name = re.sub(r"\s+-\s+[^-]{2,18}$", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_.")
    return name or path.stem, year


def fast_fingerprint(path: Path, size: int) -> str:
    digest = hashlib.blake2b(digest_size=12)
    digest.update(str(size).encode())
    try:
        with path.open("rb") as handle:
            digest.update(handle.read(256 * 1024))
            if size > 512 * 1024:
                handle.seek(max(0, size - 256 * 1024))
                digest.update(handle.read(256 * 1024))
        return digest.hexdigest()
    except OSError:
        return ""


def probe_video_duration(path: Path) -> float:
    """Return a video's duration in seconds; zero means it could not be read."""
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            [get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=flags, timeout=18,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if not match:
            return 0
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError):
        return 0


def duration_threshold_seconds() -> int:
    try:
        minutes = int(setting_value("min_duration_minutes") or "10")
    except ValueError:
        minutes = 10
    return max(0, minutes) * 60


FILE_ATTRIBUTE_HIDDEN = stat.UF_HIDDEN if sys.platform == "darwin" else 0x2
# macOS has no Windows SYSTEM visibility mode. Never map it to BSD system flags.
FILE_ATTRIBUTE_SYSTEM = 0 if sys.platform == "darwin" else 0x4
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def hide_files_enabled() -> bool:
    """New installations hide imported media by default."""
    return setting_value("hide_files_after_import").lower() != "false"


def hide_files_protected() -> bool:
    return setting_value("hide_files_system_attribute").lower() == "true"


def get_file_attributes(path: Path | str) -> int:
    if sys.platform == "darwin":
        try:
            return os.stat(path, follow_symlinks=False).st_flags
        except OSError:
            return -1
    if os.name != "nt":
        return -1
    getter = ctypes.windll.kernel32.GetFileAttributesW
    getter.argtypes = [ctypes.c_wchar_p]
    getter.restype = ctypes.c_uint32
    attributes = int(getter(str(path)))
    return -1 if attributes == INVALID_FILE_ATTRIBUTES else attributes


def set_file_attributes(path: Path | str, attributes: int) -> bool:
    if sys.platform == "darwin":
        if attributes < 0 or Path(path).is_symlink():
            return False
        try:
            os.chflags(path, attributes, follow_symlinks=False)
            return True
        except OSError:
            return False
    if os.name != "nt" or attributes < 0:
        return False
    setter = ctypes.windll.kernel32.SetFileAttributesW
    setter.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    setter.restype = ctypes.c_int
    return bool(setter(str(path), int(attributes)))


def hide_media_file(path: Path | str, protected: bool = False) -> tuple[bool, int]:
    """Hide a file in place, returning whether attributes changed and the original mask."""
    original = get_file_attributes(path)
    if original < 0:
        return False, original
    desired = original | FILE_ATTRIBUTE_HIDDEN
    if protected:
        desired |= FILE_ATTRIBUTE_SYSTEM
    if desired == original:
        return False, original
    return set_file_attributes(path, desired), original


def ensure_media_hidden(
    path: Path | str, protected: bool = False, original_attributes: int = -1,
) -> bool:
    """Keep an app-owned file hidden without replacing its saved original attributes."""
    attributes = get_file_attributes(path)
    if attributes < 0:
        return False
    desired = attributes | FILE_ATTRIBUTE_HIDDEN
    if protected:
        desired |= FILE_ATTRIBUTE_SYSTEM
    elif original_attributes >= 0 and not (original_attributes & FILE_ATTRIBUTE_SYSTEM):
        desired &= ~FILE_ATTRIBUTE_SYSTEM
    return desired == attributes or set_file_attributes(path, desired)


def restore_media_file(path: Path | str, original_attributes: int) -> bool:
    """Restore the exact attribute mask saved before this app hid the file."""
    return set_file_attributes(path, int(original_attributes))


def find_local_poster(video: Path) -> str:
    candidates = [
        video.with_suffix(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")
    ]
    candidates.extend(video.parent / name for name in (
        "poster.jpg", "poster.png", "folder.jpg", "cover.jpg", "fanart.jpg"
    ))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return ""


def read_nfo(video: Path) -> dict[str, Any]:
    candidates = [video.with_suffix(".nfo"), video.parent / "movie.nfo", video.parent / "tvshow.nfo"]
    nfo = next((item for item in candidates if item.is_file()), None)
    if not nfo:
        return {}
    try:
        root = ET.parse(nfo).getroot()
        actors = []
        for actor in root.findall("actor")[:12]:
            actors.append({
                "name": actor.findtext("name", ""),
                "role": actor.findtext("role", ""),
                "avatar": actor.findtext("thumb", ""),
            })
        return {
            "title": root.findtext("title", "").strip(),
            "original_title": root.findtext("originaltitle", "").strip(),
            "year": int(root.findtext("year", "0") or 0) or None,
            "overview": (root.findtext("plot", "") or root.findtext("outline", "")).strip(),
            "genres": [node.text.strip() for node in root.findall("genre") if node.text],
            "cast": actors,
            "poster_url": root.findtext("thumb", "").strip(),
        }
    except (ET.ParseError, OSError, ValueError):
        return {}


class ScanState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data = {
            "running": False, "processed": 0, "found": 0, "filtered": 0,
            "hidden": 0, "folders_hidden": 0, "removed": 0,
            "current": "", "errors": [], "started_at": "",
        }

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            self.data.update(kwargs)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.data)


SCAN = ScanState()
DURATION_FILTER = ScanState()


class MatchState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data = self.default_data()

    @staticmethod
    def default_data() -> dict[str, Any]:
        return {
            "running": False, "total": 0, "processed": 0, "matched": 0,
            "review": 0, "no_match": 0, "failed": 0, "current": "",
            "last_result": "", "errors": [],
        }

    def reset(self) -> None:
        with self.lock:
            self.data = self.default_data()

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            self.data.update(kwargs)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.data)


AUTO_MATCH = MatchState()
ACTOR_ENRICH = MatchState()


def scan_roots(roots: list[str]) -> None:
    SCAN.update(
        running=True, processed=0, found=0, filtered=0, hidden=0, folders_hidden=0, removed=0,
        current="", errors=[], started_at=now_iso(),
    )
    found = 0
    filtered = 0
    hidden = 0
    folders_hidden = 0
    removed = 0
    processed = 0
    errors: list[str] = []
    threshold = duration_threshold_seconds()
    should_hide = hide_files_enabled()
    protected_hide = hide_files_protected()
    try:
        import privacy
        exclusions = privacy.scan_exclusions()
        with connect() as conn:
            for root_text in roots:
                root = Path(root_text).expanduser()
                if not root.exists() or not root.is_dir():
                    errors.append(f"无法访问：{root}")
                    continue
                resolved_root = str(root.resolve())
                seen_paths: set[str] = set()
                unscanned_dirs: list[str] = []

                def walk_error(exc: OSError) -> None:
                    failed_path = str(getattr(exc, "filename", "") or "")
                    if failed_path:
                        unscanned_dirs.append(str(Path(failed_path).resolve()))
                    if len(errors) < 30:
                        errors.append(f"{failed_path or resolved_root}: {exc}")

                conn.execute(
                    "INSERT INTO scan_roots(path,last_scanned_at,enabled) VALUES(?,?,1) "
                    "ON CONFLICT(path) DO UPDATE SET last_scanned_at=excluded.last_scanned_at,enabled=1",
                    (resolved_root, now_iso()),
                )
                for dirpath, dirnames, filenames in os.walk(root, onerror=walk_error):
                    skipped = [d for d in dirnames if d.lower() in SKIP_DIRS or d.startswith(".")]
                    unscanned_dirs.extend(str(Path(dirpath) / name) for name in skipped)
                    dirnames[:] = [d for d in dirnames if d not in skipped]
                    for filename in filenames:
                        path = Path(dirpath) / filename
                        if path.suffix.lower() not in VIDEO_EXTENSIONS:
                            continue
                        if privacy.MODE:
                            if privacy.blocked_path(path, exclusions=exclusions):
                                continue
                            try:
                                if privacy.blocked_path(path, fast_fingerprint(path, path.stat().st_size), exclusions):
                                    continue
                            except OSError:
                                continue
                        processed += 1
                        SCAN.update(processed=processed, current=str(path))
                        try:
                            resolved_path = str(path.resolve())
                        except OSError:
                            resolved_path = os.path.abspath(str(path))
                        # Seeing the directory entry is enough to preserve an existing record,
                        # even when metadata or file contents are temporarily unreadable.
                        seen_paths.add(os.path.normcase(resolved_path))
                        try:
                            stat = path.stat()
                            existing = conn.execute(
                                "SELECT id,file_size,modified_at,duration_seconds,hidden_by_app,"
                                "original_file_attributes,file_status FROM movies WHERE path=?", (resolved_path,)
                            ).fetchone()
                            if existing and existing["file_status"] == 'trashed':
                                continue  # Records removed from the library stay in the recycle bin during scans.
                            if existing and existing["file_size"] == stat.st_size and existing["modified_at"] == stat.st_mtime:
                                duration = float(existing["duration_seconds"] or 0) or probe_video_duration(path)
                                if duration and not existing["duration_seconds"]:
                                    conn.execute("UPDATE movies SET duration_seconds=? WHERE id=?", (duration, existing["id"]))
                                if threshold and duration and duration < threshold:
                                    if existing["hidden_by_app"] and path.is_file():
                                        restore_media_file(path, existing["original_file_attributes"])
                                    conn.execute("DELETE FROM movies WHERE id=?", (existing["id"],))
                                    filtered += 1
                                    SCAN.update(filtered=filtered)
                                    continue
                                if should_hide:
                                    if existing["hidden_by_app"]:
                                        ensure_media_hidden(
                                            path, protected_hide, existing["original_file_attributes"],
                                        )
                                    else:
                                        changed, original = hide_media_file(path, protected_hide)
                                        if changed:
                                            hidden += 1
                                            conn.execute(
                                                "UPDATE movies SET hidden_by_app=1,original_file_attributes=? WHERE id=?",
                                                (original, existing["id"]),
                                            )
                                elif existing["hidden_by_app"]:
                                    if restore_media_file(path, existing["original_file_attributes"]):
                                        conn.execute(
                                            "UPDATE movies SET hidden_by_app=0,original_file_attributes=-1 WHERE id=?",
                                            (existing["id"],),
                                        )
                                conn.execute("UPDATE movies SET exists_now=1,file_status='available' WHERE id=?", (existing["id"],))
                                SCAN.update(hidden=hidden)
                                continue
                            duration = probe_video_duration(path)
                            if threshold and duration and duration < threshold:
                                if existing:
                                    if existing["hidden_by_app"] and path.is_file():
                                        restore_media_file(path, existing["original_file_attributes"])
                                    conn.execute("DELETE FROM movies WHERE id=?", (existing["id"],))
                                filtered += 1
                                SCAN.update(filtered=filtered)
                                continue
                            hidden_by_app = int(existing["hidden_by_app"] or 0) if existing else 0
                            original_attributes = (
                                int(existing["original_file_attributes"])
                                if existing and existing["original_file_attributes"] is not None else -1
                            )
                            if should_hide:
                                if hidden_by_app:
                                    ensure_media_hidden(path, protected_hide, original_attributes)
                                else:
                                    changed, original = hide_media_file(path, protected_hide)
                                    if changed:
                                        hidden_by_app = 1
                                        original_attributes = original
                                        hidden += 1
                            elif hidden_by_app and restore_media_file(path, original_attributes):
                                hidden_by_app = 0
                                original_attributes = -1
                            title, year = clean_filename(path)
                            nfo = read_nfo(path)
                            title = nfo.get("title") or title
                            year = nfo.get("year") or year
                            local_poster = find_local_poster(path)
                            fingerprint = fast_fingerprint(path, stat.st_size)
                            timestamp = now_iso()
                            values = (
                                resolved_path, filename, title, nfo.get("original_title", ""), year,
                                nfo.get("overview", ""), json.dumps(nfo.get("genres", []), ensure_ascii=False),
                                nfo.get("poster_url", ""), local_poster,
                                json.dumps(nfo.get("cast", []), ensure_ascii=False),
                                "nfo" if nfo else "local", "matched" if nfo else "unmatched",
                                duration, stat.st_size, stat.st_mtime, fingerprint, volume_for_path(path),
                                hidden_by_app, original_attributes, timestamp, timestamp,
                            )
                            conn.execute(
                                """INSERT INTO movies(
                                    path,filename,title,original_title,year,overview,genres,poster_url,local_poster,
                                    cast_json,source,match_status,duration_seconds,file_size,modified_at,fingerprint,drive,
                                    hidden_by_app,original_file_attributes,created_at,updated_at
                                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(path) DO UPDATE SET
                                    filename=excluded.filename,file_size=excluded.file_size,modified_at=excluded.modified_at,
                                    duration_seconds=excluded.duration_seconds,fingerprint=excluded.fingerprint,
                                    drive=excluded.drive,exists_now=1,file_status='available',updated_at=excluded.updated_at,
                                    hidden_by_app=excluded.hidden_by_app,
                                    original_file_attributes=excluded.original_file_attributes,
                                    title=CASE WHEN movies.match_status IN ('unmatched','review','no_match') THEN excluded.title ELSE movies.title END,
                                    year=COALESCE(movies.year,excluded.year),
                                    local_poster=CASE WHEN excluded.local_poster<>'' THEN excluded.local_poster ELSE movies.local_poster END
                                """,
                                values,
                            )
                            found += 1
                            SCAN.update(hidden=hidden)
                            if processed % 20 == 0:
                                conn.commit()
                                SCAN.update(found=found)
                        except (OSError, sqlite3.Error) as exc:
                            if len(errors) < 30:
                                errors.append(f"{path}: {exc}")
                root_rows = conn.execute("SELECT id,path FROM movies WHERE exists_now=1").fetchall()
                missing_ids = [
                    row["id"] for row in root_rows
                    if path_is_within(row["path"], resolved_root)
                    and os.path.normcase(row["path"]) not in seen_paths
                    and not any(path_is_within(row["path"], excluded) for excluded in unscanned_dirs)
                ]
                if missing_ids:
                    conn.executemany("UPDATE movies SET exists_now=0,file_status='missing' WHERE id=?", ((movie_id,) for movie_id in missing_ids))
                    removed += len(missing_ids)
                    SCAN.update(removed=removed)
                _prune_unused_actor_profiles(conn)
            folder_result = sync_hidden_folders(conn)
            folders_hidden = int(folder_result["hidden"])
            conn.commit()
    finally:
        SCAN.update(
            running=False, found=found, filtered=filtered, hidden=hidden,
            folders_hidden=folders_hidden, removed=removed, processed=processed,
            current="", errors=errors, finished_at=now_iso(),
        )


def enforce_duration_threshold() -> dict[str, Any]:
    """Re-check the library and remove short-video records without touching files."""
    threshold = duration_threshold_seconds()
    DURATION_FILTER.update(
        running=True, processed=0, found=0, filtered=0, current="",
        errors=[], started_at=now_iso(),
    )
    removed = processed = 0
    errors: list[str] = []
    try:
        if not threshold:
            return {"total": 0, "processed": 0, "removed": 0, "errors": [], "threshold_minutes": 0}
        with connect() as conn:
            rows = conn.execute(
                "SELECT id,path,duration_seconds,hidden_by_app,original_file_attributes "
                "FROM movies WHERE exists_now=1"
            ).fetchall()
            for row in rows:
                processed += 1
                path = Path(row["path"])
                DURATION_FILTER.update(processed=processed, current=str(path))
                try:
                    duration = float(row["duration_seconds"] or 0)
                    if not duration and path.is_file():
                        duration = probe_video_duration(path)
                        if duration:
                            conn.execute("UPDATE movies SET duration_seconds=? WHERE id=?", (duration, row["id"]))
                    if duration and duration < threshold:
                        if row["hidden_by_app"] and path.is_file():
                            restore_media_file(path, row["original_file_attributes"])
                        conn.execute("DELETE FROM movies WHERE id=?", (row["id"],))
                        removed += 1
                        DURATION_FILTER.update(filtered=removed)
                    if processed % 20 == 0:
                        conn.commit()
                except (OSError, sqlite3.Error) as exc:
                    if len(errors) < 30:
                        errors.append(f"{path}: {exc}")
            conn.commit()
            sync_hidden_folders(conn)
        return {
            "total": len(rows), "processed": processed, "removed": removed,
            "errors": errors, "threshold_minutes": threshold // 60,
        }
    finally:
        DURATION_FILTER.update(
            running=False, processed=processed, filtered=removed, current="",
            errors=errors, finished_at=now_iso(),
        )


def register_scan_root(path: str) -> str:
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError("扫描源不存在或不是文件夹")
    with connect() as conn:
        conn.execute(
            "INSERT INTO scan_roots(path,last_scanned_at,enabled) VALUES(?,NULL,1) "
            "ON CONFLICT(path) DO UPDATE SET enabled=1",
            (str(root),),
        )
    return str(root)


def remove_scan_root(path: str) -> int:
    """Remove a managed root, restoring app-hidden files without deleting media."""
    root = str(Path(path).expanduser().resolve())
    with connect() as conn:
        other_roots = [row["path"] for row in conn.execute(
            "SELECT path FROM scan_roots WHERE enabled=1 AND path<>?", (root,)
        )]
        rows = conn.execute(
            "SELECT id,path,hidden_by_app,original_file_attributes FROM movies"
        ).fetchall()
        delete_rows = [
            row for row in rows
            if path_is_within(row["path"], root)
            and not any(path_is_within(row["path"], other) for other in other_roots)
        ]
        for row in delete_rows:
            media_path = Path(row["path"])
            if row["hidden_by_app"] and media_path.is_file():
                restore_media_file(media_path, row["original_file_attributes"])
        delete_ids = [row["id"] for row in delete_rows]
        if delete_ids:
            conn.executemany("DELETE FROM movies WHERE id=?", ((movie_id,) for movie_id in delete_ids))
        conn.execute("DELETE FROM scan_roots WHERE path=?", (root,))
        _prune_unused_actor_profiles(conn)
        sync_hidden_folders(conn)
    return len(delete_ids)


def _prune_unused_actor_profiles(conn: sqlite3.Connection) -> None:
    used_actor_keys = {
        normalize_actor_name(str(person.get("name") or ""))
        for row in conn.execute("SELECT cast_json FROM movies WHERE file_status<>'trashed'")
        for person in json_value(row["cast_json"], [])
        if person.get("name")
    }
    profile_keys = [row["name_key"] for row in conn.execute("SELECT name_key FROM actor_profiles WHERE favorite=0 AND retained=0")]
    unused = [key for key in profile_keys if key not in used_actor_keys]
    if unused:
        conn.executemany("DELETE FROM actor_profiles WHERE name_key=?", ((key,) for key in unused))


def sync_hidden_folders(conn: sqlite3.Connection) -> dict[str, int]:
    """Hide direct movie folders (never scan roots) and restore folders no longer needed."""
    roots = [str(row["path"]) for row in conn.execute(
        "SELECT path FROM scan_roots WHERE enabled=1"
    )]
    root_keys = {os.path.normcase(os.path.abspath(root)) for root in roots}
    required: dict[str, tuple[str, str]] = {}
    for row in conn.execute("SELECT path FROM movies WHERE exists_now=1"):
        movie_path = str(row["path"])
        parent = str(Path(movie_path).parent)
        parent_key = os.path.normcase(os.path.abspath(parent))
        if parent_key in root_keys:
            continue
        containing_roots = [root for root in roots if path_is_within(movie_path, root)]
        if containing_roots:
            required[parent_key] = (parent, max(containing_roots, key=len))

    tracked = {
        os.path.normcase(os.path.abspath(str(row["path"]))): dict(row)
        for row in conn.execute("SELECT path,root_path,original_file_attributes FROM hidden_folders")
    }
    hidden_count = restored_count = 0
    hide_enabled = hide_files_enabled()
    protected = hide_files_protected()
    if hide_enabled:
        for folder_key, (folder_text, root) in required.items():
            folder = Path(folder_text)
            saved = tracked.get(folder_key)
            if not folder.is_dir():
                continue
            if saved:
                ensure_media_hidden(folder, protected, saved["original_file_attributes"])
                continue
            changed, original = hide_media_file(folder, protected)
            if changed:
                conn.execute(
                    "INSERT OR IGNORE INTO hidden_folders(path,root_path,original_file_attributes,created_at) "
                    "VALUES(?,?,?,?)",
                    (str(folder), root, original, now_iso()),
                )
                hidden_count += 1

    for folder_key, saved in tracked.items():
        if hide_enabled and folder_key in required:
            continue
        folder = Path(saved["path"])
        if not folder.exists() and any(
            path_is_within(row["path"], str(folder))
            for row in conn.execute("SELECT path FROM movies WHERE file_status='missing'")
        ):
            continue  # Retain ownership information for a later folder relink.
        if not folder.exists() or restore_media_file(folder, saved["original_file_attributes"]):
            conn.execute("DELETE FROM hidden_folders WHERE path=?", (saved["path"],))
            restored_count += 1
    return {"hidden": hidden_count, "restored": restored_count}


def path_is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(path), os.path.normcase(root))) == os.path.normcase(root)
    except ValueError:
        return False


def list_scan_roots() -> list[dict[str, Any]]:
    with connect() as conn:
        roots = [dict(row) for row in conn.execute(
            "SELECT path,last_scanned_at,enabled FROM scan_roots WHERE enabled=1 ORDER BY path COLLATE NOCASE"
        )]
        movies = [dict(row) for row in conn.execute(
            "SELECT path,file_size,exists_now,file_status FROM movies"
        )]
    for root in roots:
        items = [movie for movie in movies if movie["exists_now"] and path_is_within(movie["path"], root["path"])]
        root["movie_count"] = len(items)
        root["missing_count"] = sum(1 for movie in movies if movie["file_status"] == "missing" and path_is_within(movie["path"], root["path"]))
        root["file_size"] = sum(int(movie["file_size"] or 0) for movie in items)
        root["size_label"] = human_size(root["file_size"])
        root["available"] = Path(root["path"]).is_dir()
    return roots


def _move_path_to_recycle_bin(path: Path) -> None:
    if sys.platform == "darwin":
        from PySide6.QtCore import QFile

        media = QFile(str(path))
        if not media.moveToTrash():
            raise OSError(f"无法移到废纸篓：{media.errorString()}。影片和影库记录均已保留。")
        return
    if os.name != "nt":
        raise OSError("当前系统暂不支持安全移到回收站")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_int), ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 3  # FO_DELETE
    operation.pFrom = str(path) + "\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise OSError(f"无法移到回收站（错误代码 {result}）")


def recycle_movie_file(movie_id: int, recycler: Any = None) -> str:
    with connect() as conn:
        row = conn.execute("SELECT path,hidden_by_app,original_file_attributes FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not row:
        raise ValueError("影片记录不存在")
    path = Path(row["path"]).resolve()
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("该记录不是受支持的影片文件")
    if not path.is_file():
        raise FileNotFoundError("影片文件已经不存在")
    # App-hidden media should be visible and recoverable in Finder's Trash.
    attributes = get_file_attributes(path)
    restored = False
    if sys.platform == "darwin" and row["hidden_by_app"]:
        restored = restore_media_file(path, row["original_file_attributes"])
    try:
        (recycler or _move_path_to_recycle_bin)(path)
    except Exception:
        if restored and path.exists():
            set_file_attributes(path, attributes)
        raise
    with connect() as conn:
        conn.execute(
            "UPDATE movies SET exists_now=0,file_status='trashed',disposition='delete',updated_at=? WHERE id=?",
            (now_iso(), movie_id),
        )
        sync_hidden_folders(conn)
    return str(path)


def trash_movie_record(movie_id: int) -> None:
    """Remove from library listing; the media file stays untouched on disk."""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM movies WHERE id=?", (movie_id,)).fetchone():
            raise ValueError("影片记录不存在")
        conn.execute("UPDATE movies SET file_status='trashed',exists_now=0,updated_at=? WHERE id=?", (now_iso(), movie_id))


def restore_movie_record(movie_id: int) -> None:
    """Return a trashed record to the library; the file must still exist."""
    with connect() as conn:
        row = conn.execute("SELECT path FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not row:
        raise ValueError("影片记录不存在")
    if not Path(row["path"]).is_file():
        raise FileNotFoundError(f"影片文件已不存在：{row['path']}")
    with connect() as conn:
        conn.execute("UPDATE movies SET file_status='available',exists_now=1,updated_at=? WHERE id=?", (now_iso(), movie_id))


def purge_movie_record(movie_id: int) -> str:
    """Permanently delete the file from disk and remove the record."""
    with connect() as conn:
        row = conn.execute("SELECT path FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not row:
        raise ValueError("影片记录不存在")
    path = Path(row["path"])
    if path.is_file():
        path.unlink()
    with connect() as conn:
        conn.execute("DELETE FROM movies WHERE id=?", (movie_id,))
    return str(path)


def trashed_movies(query: str = "", actor: str = "") -> list[dict[str, Any]]:
    """All records removed from the library; files remain on disk until purged."""
    where = ["file_status='trashed'"]
    args: list[Any] = []
    if query:
        where.append("(title LIKE ? OR original_title LIKE ? OR filename LIKE ?)")
        args.extend([f"%{query}%"] * 3)
    if actor:
        where.append("cast_json LIKE ?")
        args.append(f"%{actor}%")
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM movies WHERE {' AND '.join(where)} ORDER BY updated_at DESC", args).fetchall()
    return [movie_dict(row) for row in rows]


def trashed_actor_facets() -> list[dict[str, Any]]:
    """Actor counts across trashed records for the recycle bin filter."""
    with connect() as conn:
        rows = conn.execute("SELECT cast_json FROM movies WHERE file_status='trashed' AND cast_json<>''").fetchall()
    counter: dict[str, int] = {}
    for row in rows:
        for person in json_value(row[0], []):
            name = person.get("name", "")
            if name:
                counter[name] = counter.get(name, 0) + 1
    return [{"name": n, "count": c} for n, c in sorted(counter.items(), key=lambda x: -x[1])[:30]]


def get_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
    result = {row["key"]: row["value"] for row in rows}
    if result.get("tmdb_token"):
        result["tmdb_token_configured"] = "true"
        result["tmdb_token"] = ""
    return result


def setting_value(key: str) -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def http_json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    request_headers = {"Accept": "application/json", "User-Agent": "YingKu/1.0 (local media library)"}
    request_headers.update(headers or {})
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def search_tmdb(query: str, year: int | None = None) -> list[dict[str, Any]]:
    token = setting_value("tmdb_token")
    if not token:
        raise ValueError("请先在设置中填写 TMDb API Read Access Token")
    params = {"query": query, "language": "zh-CN", "include_adult": "true"}
    url = "https://api.themoviedb.org/3/search/multi?" + urllib.parse.urlencode(params)
    data = http_json(url, headers={"Authorization": f"Bearer {token}"})
    output = []
    for item in data.get("results", []):
        media_type = item.get("media_type")
        if media_type not in {"movie", "tv"}:
            continue
        date = item.get("release_date") or item.get("first_air_date") or ""
        item_year = int(date[:4]) if date[:4].isdigit() else None
        score = 0.5
        if year and item_year:
            score += max(0, 0.35 - 0.12 * abs(year - item_year))
        output.append({
            "provider": "tmdb", "id": str(item.get("id")), "media_type": media_type,
            "title": item.get("title") or item.get("name") or "",
            "original_title": item.get("original_title") or item.get("original_name") or "",
            "year": item_year, "overview": item.get("overview") or "",
            "poster": f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get("poster_path") else "",
            "external_rating": item.get("vote_average"), "confidence": round(score, 2),
        })
    return output[:12]


def search_bangumi(query: str) -> list[dict[str, Any]]:
    url = "https://api.bgm.tv/v0/search/subjects?" + urllib.parse.urlencode({"limit": 12, "offset": 0})
    data = http_json(url, method="POST", body={"keyword": query, "sort": "match"})
    output = []
    for item in data.get("data", []):
        date = item.get("date") or ""
        images = item.get("images") or {}
        output.append({
            "provider": "bangumi", "id": str(item.get("id")), "media_type": "anime" if item.get("type") == 2 else "tv",
            "title": item.get("name_cn") or item.get("name") or "", "original_title": item.get("name") or "",
            "year": int(date[:4]) if date[:4].isdigit() else None, "overview": item.get("summary") or "",
            "poster": images.get("large") or images.get("common") or "",
            "external_rating": (item.get("rating") or {}).get("score"), "confidence": 0.55,
        })
    return output


def search_code_catalog(query: str) -> list[dict[str, Any]]:
    """Experimental metadata source for code-based niche releases."""
    try:
        from javdb.__main__ import fetch_search
    except ImportError as exc:
        raise ValueError("当前版本未包含编号资料库组件") from exc
    results = fetch_search(query)
    output = []
    for item in results[:12]:
        date = item.get("date") or ""
        output.append({
            "provider": "catalog", "id": item.get("code") or item.get("link"), "link": item.get("link", ""),
            "media_type": "movie", "title": item.get("title") or item.get("code") or query,
            "original_title": item.get("code") or "", "year": int(date[:4]) if date[:4].isdigit() else None,
            "overview": "", "poster": "", "external_rating": None, "confidence": 0.72,
        })
    return output


def normalize_actor_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name or "").casefold().split())


def chinese_display_name(names: list[str]) -> str:
    for name in names:
        value = str(name or "").strip()
        has_han = bool(re.search(r"[\u3400-\u9fff]", value))
        has_kana = bool(re.search(r"[\u3040-\u30ff]", value))
        if has_han and not has_kana:
            return value
    return ""


def unique_actor_names(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = re.sub(r"\s+", " ", str(value or "").strip())
        key = normalize_actor_name(name)
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result[:80]


def flatten_profile_value(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(
            str(item.get("v") or item.get("value") or "") if isinstance(item, dict) else str(item)
            for item in value
            if item
        )
    if isinstance(value, dict):
        return str(value.get("v") or value.get("value") or "")
    return str(value or "")


def discover_actor_profiles() -> int:
    """Register actors found in movies and reuse already known avatars."""
    timestamp = now_iso()
    with connect() as conn:
        rows = conn.execute("SELECT id,cast_json FROM movies WHERE exists_now=1 AND cast_json<>'[]'").fetchall()
        alias_to_key: dict[str, str] = {}
        for profile in conn.execute("SELECT name_key,name,aliases_json FROM actor_profiles"):
            for alias in unique_actor_names([profile["name"], *json_value(profile["aliases_json"], [])]):
                alias_to_key[normalize_actor_name(alias)] = profile["name_key"]
        for row in rows:
            cast = json_value(row["cast_json"], [])
            for person in cast:
                name = str(person.get("name") or "").strip()
                raw_key = normalize_actor_name(name)
                key = alias_to_key.get(raw_key, raw_key)
                if not key:
                    continue
                avatar = str(person.get("avatar") or "").strip()
                display_name = chinese_display_name([name])
                conn.execute(
                    """INSERT INTO actor_profiles(name_key,name,display_name,avatar_url,source,status,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(name_key) DO UPDATE SET
                    name=actor_profiles.name,
                    display_name=CASE WHEN actor_profiles.display_name='' THEN excluded.display_name ELSE actor_profiles.display_name END,
                    avatar_url=CASE WHEN actor_profiles.avatar_url='' THEN excluded.avatar_url ELSE actor_profiles.avatar_url END,
                    source=CASE WHEN actor_profiles.avatar_url='' AND excluded.avatar_url<>'' THEN excluded.source ELSE actor_profiles.source END,
                    status=CASE WHEN actor_profiles.avatar_url='' AND excluded.avatar_url<>'' THEN 'matched' ELSE actor_profiles.status END,
                    updated_at=excluded.updated_at""",
                    (key, name, display_name, avatar, "movie_metadata" if avatar else "", "matched" if avatar else "new", timestamp),
                )
                profile = conn.execute(
                    "SELECT aliases_json,source_refs_json FROM actor_profiles WHERE name_key=?", (key,)
                ).fetchone()
                person_aliases = person.get("aliases") or []
                if isinstance(person_aliases, str):
                    person_aliases = [person_aliases]
                aliases = unique_actor_names(
                    json_value(profile["aliases_json"], []) + list(person_aliases) + [name]
                )
                refs = json_value(profile["source_refs_json"], {})
                profile_url = str(person.get("profile_url") or "")
                person_source = str(person.get("source") or "")
                if profile_url:
                    refs[person_source or "metadata"] = profile_url
                conn.execute(
                    "UPDATE actor_profiles SET aliases_json=?,source_refs_json=? WHERE name_key=?",
                    (json.dumps(aliases, ensure_ascii=False), json.dumps(refs, ensure_ascii=False), key),
                )
                for alias in aliases:
                    alias_to_key[normalize_actor_name(alias)] = key
        cached = {
            row["name_key"]: row["avatar_url"] for row in conn.execute(
                "SELECT name_key,avatar_url FROM actor_profiles WHERE avatar_url<>''"
            )
        }
        changed = 0
        for row in rows:
            cast = json_value(row["cast_json"], [])
            dirty = False
            for person in cast:
                avatar = cached.get(normalize_actor_name(str(person.get("name") or "")), "")
                if avatar and not person.get("avatar"):
                    person["avatar"] = avatar
                    dirty = True
            if dirty:
                conn.execute(
                    "UPDATE movies SET cast_json=?,updated_at=? WHERE id=?",
                    (json.dumps(cast, ensure_ascii=False), timestamp, row["id"]),
                )
                changed += 1
        pending = conn.execute("SELECT COUNT(*) FROM actor_profiles WHERE status='new'").fetchone()[0]
    return int(pending)


def pending_actor_profiles(limit: int = 60) -> list[dict[str, Any]]:
    retry_before = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM actor_profiles WHERE status='new' OR
            ((avatar_url='' OR display_name='') AND status IN ('no_match','failed') AND last_attempt<?)
            ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END,last_attempt,updated_at LIMIT ?""",
            (retry_before, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def store_actor_photos(name_key: str, photos: list[dict[str, Any]], selected_url: str = "") -> int:
    """Store at most 50 distinct candidates per actor and preserve the selected photo."""
    timestamp = now_iso()
    inserted = 0
    with connect() as conn:
        profile = conn.execute(
            "SELECT avatar_url,source,source_id FROM actor_profiles WHERE name_key=?", (name_key,)
        ).fetchone()
        if not profile:
            return 0
        current = selected_url or str(profile["avatar_url"] or "")
        candidates = list(photos)
        if current:
            candidates.insert(0, {
                "avatar": current, "source": profile["source"] or "existing",
                "source_id": profile["source_id"] or "",
            })
        for photo in candidates:
            url = str(photo.get("avatar") or photo.get("photo_url") or "").strip()
            if not url:
                continue
            cursor = conn.execute(
                """INSERT OR IGNORE INTO actor_photos(name_key,photo_url,source,source_id,selected,created_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    name_key, url, str(photo.get("source") or ""), str(photo.get("source_id") or ""),
                    1 if current and url == current else 0, timestamp,
                ),
            )
            inserted += max(0, cursor.rowcount)
        if current:
            conn.execute(
                "UPDATE actor_photos SET selected=CASE WHEN photo_url=? THEN 1 ELSE 0 END WHERE name_key=?",
                (current, name_key),
            )
        rows = conn.execute(
            "SELECT id,selected FROM actor_photos WHERE name_key=? ORDER BY selected DESC,id DESC", (name_key,)
        ).fetchall()
        if len(rows) > 50:
            conn.executemany("DELETE FROM actor_photos WHERE id=?", ((row["id"],) for row in rows[50:]))
    return inserted


def actor_photo_choices(name_key: str) -> list[dict[str, Any]]:
    with connect() as conn:
        profile = conn.execute(
            "SELECT avatar_url,source,source_id FROM actor_profiles WHERE name_key=?", (name_key,)
        ).fetchone()
    if profile and profile["avatar_url"]:
        store_actor_photos(name_key, [], str(profile["avatar_url"]))
    with connect() as conn:
        rows = conn.execute(
            """SELECT photo_url,source,source_id,selected,created_at FROM actor_photos
            WHERE name_key=? ORDER BY selected DESC,id DESC LIMIT 50""", (name_key,)
        ).fetchall()
    return [dict(row) for row in rows]


def select_actor_photo(name_key: str, photo_url: str) -> int:
    with connect() as conn:
        photo = conn.execute(
            "SELECT source,source_id FROM actor_photos WHERE name_key=? AND photo_url=?", (name_key, photo_url)
        ).fetchone()
        if not photo:
            raise ValueError("这张照片已不在演员候选库中")
        conn.execute(
            "UPDATE actor_photos SET selected=CASE WHEN photo_url=? THEN 1 ELSE 0 END WHERE name_key=?",
            (photo_url, name_key),
        )
        conn.execute(
            """UPDATE actor_profiles SET avatar_url=?,status='matched',updated_at=?
            WHERE name_key=?""",
            (photo_url, now_iso(), name_key),
        )
    return apply_actor_avatar(name_key, photo_url, replace=True)


def tmdb_person_photo_candidates(person_id: str, limit: int = 30) -> list[dict[str, str]]:
    token = setting_value("tmdb_token")
    if not token or not person_id:
        return []
    images = http_json(
        f"https://api.themoviedb.org/3/person/{person_id}/images",
        headers={"Authorization": f"Bearer {token}"},
    ).get("profiles", [])
    portraits = [
        image for image in images
        if image.get("file_path") and 0.45 <= float(image.get("aspect_ratio") or 0) <= 1.0
        and int(image.get("height") or 0) >= int(image.get("width") or 0)
    ]
    portraits.sort(
        key=lambda image: (
            float(image.get("vote_average") or 0) + min(2.0, float(image.get("vote_count") or 0) / 10),
            int(image.get("height") or 0),
        ),
        reverse=True,
    )
    return [
        {
            "avatar": f"https://image.tmdb.org/t/p/w500{image['file_path']}",
            "source": "tmdb", "source_id": str(person_id),
        }
        for image in portraits[:limit]
    ]


def search_tmdb_actor_avatar(name: str) -> dict[str, Any] | None:
    token = setting_value("tmdb_token")
    if not token:
        return None
    params = {"query": name, "language": "zh-CN", "include_adult": "true", "page": 1}
    data = http_json(
        "https://api.themoviedb.org/3/search/person?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {token}"},
    )
    key = normalize_actor_name(name)
    matches: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
    for item in data.get("results", [])[:8]:
        if not item.get("profile_path"):
            continue
        try:
            detail = http_json(
                f"https://api.themoviedb.org/3/person/{item['id']}?language=zh-CN",
                headers={"Authorization": f"Bearer {token}"},
            )
        except (OSError, ValueError, urllib.error.URLError):
            detail = item
        aliases = unique_actor_names(
            [item.get("name"), detail.get("name"), *(detail.get("also_known_as") or [])]
        )
        if any(normalize_actor_name(alias) == key for alias in aliases):
            matches.append((item, detail, aliases))
    if not matches:
        return None
    person, detail, aliases = max(matches, key=lambda value: float(value[0].get("popularity") or 0))
    profile_path = person["profile_path"]
    try:
        candidates = tmdb_person_photo_candidates(str(person["id"]), 1)
        if candidates:
            profile_path = candidates[0]["avatar"].rsplit("/w500", 1)[-1]
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return {
        "avatar": f"https://image.tmdb.org/t/p/w342{profile_path}",
        "source": "tmdb", "source_id": str(person.get("id") or ""),
        "display_name": chinese_display_name(aliases), "aliases": aliases,
        "biography": str(detail.get("biography") or ""),
        "info": {
            key: value for key, value in {
                "生日": detail.get("birthday"), "出生地": detail.get("place_of_birth"),
                "IMDb": detail.get("imdb_id"),
            }.items() if value
        },
    }


def search_wikidata_actor_avatar(name: str) -> dict[str, str] | None:
    has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", name))
    languages = ("zh", "ja", "en") if has_cjk else ("en", "zh", "ja")
    person_words = (
        "actor", "actress", "performer", "idol", "singer", "model", "演员", "演員", "艺人", "藝人",
        "女优", "女優", "俳優", "タレント", "モデル", "歌手",
    )
    entity_id = ""
    for language in languages:
        params = {
            "action": "wbsearchentities", "search": name, "language": language,
            "uselang": language, "format": "json", "limit": 8,
        }
        data = http_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(params))
        for item in data.get("search", []):
            label = str(item.get("label") or "")
            matched_text = str((item.get("match") or {}).get("text") or "")
            description = str(item.get("description") or "").casefold()
            exact = normalize_actor_name(label) == normalize_actor_name(name) or normalize_actor_name(matched_text) == normalize_actor_name(name)
            if exact and any(word in description for word in person_words):
                entity_id = str(item.get("id") or "")
                break
        if entity_id:
            break
    if not entity_id:
        return None
    entity_params = {
        "action": "wbgetentities", "ids": entity_id, "props": "claims|labels|aliases",
        "languages": "zh-hans|zh-cn|zh|ja|en", "languagefallback": 1, "format": "json",
    }
    entity_data = http_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(entity_params))
    entity = ((entity_data.get("entities") or {}).get(entity_id) or {})
    labels = entity.get("labels") or {}
    localized = chinese_display_name([
        str((labels.get(language) or {}).get("value") or "")
        for language in ("zh-hans", "zh-cn", "zh", "ja", "en")
    ])
    aliases = unique_actor_names([
        name,
        *[str((labels.get(language) or {}).get("value") or "") for language in ("zh-hans", "zh-cn", "zh", "ja", "en")],
        *[
            str(item.get("value") or "")
            for language in ("zh-hans", "zh-cn", "zh", "ja", "en")
            for item in ((entity.get("aliases") or {}).get(language) or [])
        ],
    ])
    claims = entity.get("claims") or {}
    images = claims.get("P18") or []
    ranked = sorted(images, key=lambda item: item.get("rank") == "preferred", reverse=True)
    for claim in ranked:
        filename = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
        if filename:
            normalized_file = str(filename).replace(" ", "_")
            digest = hashlib.md5(normalized_file.encode("utf-8")).hexdigest()
            quoted = urllib.parse.quote(normalized_file, safe="")
            thumb_name = f"330px-{quoted}" + (".png" if normalized_file.lower().endswith(".svg") else "")
            return {
                "avatar": f"https://upload.wikimedia.org/wikipedia/commons/thumb/{digest[0]}/{digest[:2]}/{quoted}/{thumb_name}",
                "source": "wikidata", "source_id": entity_id, "display_name": localized,
                "aliases": aliases,
            }
    return {
        "avatar": "", "source": "wikidata", "source_id": entity_id,
        "display_name": localized, "aliases": aliases,
    } if localized or aliases else None


def search_wikipedia_actor_avatar(name: str) -> dict[str, str] | None:
    has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", name))
    languages = ("zh", "ja", "en") if has_cjk else ("en", "zh")
    person_words = (
        "actor", "actress", "performer", "singer", "model", "演员", "演員", "艺人", "藝人",
        "女优", "女優", "俳優", "タレント", "モデル", "歌手",
    )
    for language in languages:
        params = {
            "action": "query", "format": "json", "formatversion": 2, "redirects": 1,
            "titles": name, "prop": "pageimages|pageterms", "piprop": "thumbnail",
            "pithumbsize": 320, "wbptterms": "description",
        }
        data = http_json(f"https://{language}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params))
        for page in (data.get("query") or {}).get("pages", []):
            thumbnail = (page.get("thumbnail") or {}).get("source")
            if not thumbnail or page.get("missing"):
                continue
            descriptions = (page.get("terms") or {}).get("description") or []
            description = " ".join(str(value).casefold() for value in descriptions)
            if description and not any(word in description for word in person_words):
                continue
            return {
                "avatar": thumbnail, "source": f"wikipedia-{language}",
                "source_id": str(page.get("pageid") or ""),
                "display_name": chinese_display_name([str(page.get("title") or "")]) if language == "zh" else "",
            }
    return None


def search_bangumi_actor_profile(name: str) -> dict[str, Any] | None:
    data = http_json(
        "https://api.bgm.tv/v0/search/persons?limit=10",
        method="POST", body={"keyword": name},
    )
    key = normalize_actor_name(name)
    matches: list[tuple[dict[str, Any], list[str], str, dict[str, str]]] = []
    for person in data.get("data", []) if isinstance(data, dict) else []:
        display_name = ""
        aliases: list[str] = [str(person.get("name") or "")]
        info: dict[str, str] = {}
        for item in person.get("infobox") or []:
            label = str(item.get("key") or "")
            value = item.get("value")
            if label == "简体中文名":
                display_name = flatten_profile_value(value)
                aliases.append(display_name)
            elif label == "别名":
                if isinstance(value, list):
                    for entry in value:
                        aliases.extend(
                            part.strip() for part in re.split(r"[、,;/／]+", flatten_profile_value(entry)) if part.strip()
                        )
            elif label and label not in {"链接", "引用来源"}:
                flattened = flatten_profile_value(value)
                if flattened:
                    info[label] = flattened
        aliases = unique_actor_names(aliases)
        if not any(normalize_actor_name(alias) == key for alias in aliases):
            continue
        matches.append((person, aliases, display_name, info))
    if not matches:
        return None
    person, aliases, display_name, info = max(
        matches,
        key=lambda value: (
            bool((value[0].get("images") or {}).get("large") or value[0].get("img")),
            int((value[0].get("stat") or {}).get("collects") or 0),
        ),
    )
    images = person.get("images") or {}
    avatar = str(images.get("large") or images.get("medium") or person.get("img") or "")
    return {
        "avatar": avatar, "source": "bangumi-person", "source_id": str(person.get("id") or ""),
        "display_name": display_name or chinese_display_name(aliases), "aliases": aliases,
        "biography": str(person.get("summary") or ""), "info": info,
    }


def _catalog_text(fragment: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip(" -\r\n\t")


def fetch_catalog_movie_actors(page_url: str) -> list[dict[str, str]]:
    if not page_url:
        return []
    try:
        from javdb.__main__ import _fetch_html
    except ImportError:
        return []
    source = _fetch_html(page_url)
    if not source:
        return []
    pattern = re.compile(
        r'(?is)<a[^>]+href="(https://www\.javdatabase\.com/idols/[^"?#]+/?)"[^>]*>(.*?)</a>'
    )
    actors: list[dict[str, str]] = []
    seen: set[str] = set()
    for profile_url, label in pattern.findall(source):
        name = _catalog_text(label)
        if name and profile_url not in seen and "/idols/?" not in profile_url:
            seen.add(profile_url)
            actors.append({
                "name": name, "role": "主演", "avatar": "",
                "source": "catalog-actor", "profile_url": profile_url,
            })
    return actors[:20]


def search_catalog_actor_profile(name: str, profile_url: str = "") -> dict[str, Any] | None:
    try:
        from javdb.__main__ import _fetch_html
    except ImportError:
        return None
    if not profile_url:
        search_url = "https://www.javdatabase.com/?" + urllib.parse.urlencode({"post_type": "idols", "s": name})
        search_html = _fetch_html(search_url)
        if not search_html:
            return None
        key = normalize_actor_name(name)
        links = re.findall(
            r'(?is)href="(https://www\.javdatabase\.com/idols/[^"?#]+/?)"[^>]*>(.*?)</a>', search_html
        )
        exact = [
            (url, _catalog_text(label)) for url, label in links
            if "/idols/?" not in url and normalize_actor_name(_catalog_text(label)) == key
        ]
        if not exact:
            return None
        profile_url = exact[0][0]
    source = _fetch_html(profile_url)
    if not source:
        return None
    heading = re.search(r'(?is)<h1[^>]*class="[^"]*idol-name[^"]*"[^>]*>(.*?)</h1>', source)
    primary_name = re.sub(r"\s+-\s+JAV Profile.*$", "", _catalog_text(heading.group(1) if heading else name), flags=re.I)
    japanese = ""
    jp_match = re.search(r"(?is)<b[^>]*>\s*JP\s*:</b>\s*([^<]+)", source)
    if jp_match:
        japanese = _catalog_text(jp_match.group(1))
    aliases = unique_actor_names([name, primary_name, japanese])
    image_match = re.search(
        r'(?is)<img[^>]+src="(https://www\.javdatabase\.com/idolimages/full/[^"]+)"', source
    )
    info: dict[str, str] = {}
    for output_name, label in (
        ("生日", "DOB"), ("出道", "Debut"), ("出生地", "Birthplace"),
        ("身高", "Height"), ("罩杯", "Cup"), ("三围", "Measurements"),
    ):
        match = re.search(
            rf"(?is)<b[^>]*>\s*{re.escape(label)}\s*:</b>\s*(.*?)(?=\s*-\s*<b|<br|</p>)", source
        )
        value = _catalog_text(match.group(1)) if match else ""
        if value and value != "?":
            info[output_name] = value
    return {
        "avatar": image_match.group(1) if image_match else "",
        "source": "catalog-actor", "source_id": profile_url,
        "display_name": chinese_display_name([japanese]) or japanese, "aliases": aliases,
        "biography": "", "info": info, "profile_url": profile_url,
    }


def search_wikipedia_actor_photos(name: str, limit: int = 10) -> list[dict[str, str]]:
    """Use Wikipedia full-text search when the actor's page title is not an exact name match."""
    has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", name))
    languages = ("zh", "ja", "en") if has_cjk else ("en", "zh", "ja")
    person_words = (
        "actor", "actress", "performer", "idol", "singer", "model", "演员", "演員", "艺人", "藝人",
        "女优", "女優", "俳優", "タレント", "モデル", "歌手", "グラビア",
    )
    key = normalize_actor_name(name)
    results: list[dict[str, str]] = []
    for language in languages:
        params = {
            "action": "query", "format": "json", "formatversion": 2,
            "generator": "search", "gsrsearch": name, "gsrnamespace": 0, "gsrlimit": 8,
            "prop": "pageimages|pageterms", "piprop": "thumbnail", "pithumbsize": 500,
            "wbptterms": "description",
        }
        data = http_json(f"https://{language}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params))
        for page in (data.get("query") or {}).get("pages", []):
            title = str(page.get("title") or "")
            description = " ".join(
                str(value).casefold() for value in ((page.get("terms") or {}).get("description") or [])
            )
            thumbnail = str((page.get("thumbnail") or {}).get("source") or "")
            title_match = key in normalize_actor_name(title) or normalize_actor_name(title) in key
            if not thumbnail or not title_match or (description and not any(word in description for word in person_words)):
                continue
            results.append({
                "avatar": thumbnail, "source": f"wikipedia-search-{language}",
                "source_id": str(page.get("pageid") or ""),
            })
            if len(results) >= limit:
                return results
    return results


def search_wikimedia_commons_actor_photos(
    names: list[str], limit: int = 24,
) -> list[dict[str, str]]:
    """Search portrait-shaped files in Wikimedia Commons by the actor's known names."""
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in dict.fromkeys(value.strip() for value in names if value and value.strip()):
        params = {
            "action": "query", "format": "json", "formatversion": 2,
            "generator": "search", "gsrsearch": f'"{name}"', "gsrnamespace": 6, "gsrlimit": 20,
            "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": 500,
        }
        data = http_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params))
        for page in (data.get("query") or {}).get("pages", []):
            info = ((page.get("imageinfo") or [{}])[0])
            mime = str(info.get("mime") or "")
            width, height = int(info.get("width") or 0), int(info.get("height") or 0)
            url = str(info.get("thumburl") or info.get("url") or "")
            ratio = width / height if height else 0
            if not url or url in seen or not mime.startswith("image/") or not (0.4 <= ratio <= 1.15):
                continue
            seen.add(url)
            results.append({
                "avatar": url, "source": "wikimedia-commons",
                "source_id": str(page.get("pageid") or ""),
            })
            if len(results) >= limit:
                return results
    return results


def collect_actor_photo_candidates(
    name: str, display_name: str = "", seed: dict[str, Any] | None = None,
    aliases: list[str] | None = None, limit: int = 50,
) -> tuple[list[dict[str, str]], list[str]]:
    candidates: list[dict[str, str]] = []
    errors: list[str] = []
    if seed and seed.get("avatar"):
        candidates.append(seed)
    if seed and seed.get("source") == "tmdb" and seed.get("source_id"):
        try:
            candidates.extend(tmdb_person_photo_candidates(str(seed["source_id"]), 30))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"tmdb_person_photo_candidates: {exc}")
    search_names = unique_actor_names([name, display_name, *(aliases or []), *((seed or {}).get("aliases") or [])])
    providers = tuple(
        [lambda query=query: search_wikipedia_actor_photos(query, 6) for query in search_names[:3]]
        + [lambda: search_wikimedia_commons_actor_photos(search_names[:8], 24)]
    )
    for provider in providers:
        try:
            candidates.extend(provider())
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(str(exc))
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = str(candidate.get("avatar") or "")
        if url and url not in seen:
            seen.add(url)
            unique.append(candidate)
        if len(unique) >= limit:
            break
    return unique, errors


def merge_actor_results(primary: dict[str, Any], *supporting: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(primary)
    valid = [item for item in supporting if item]
    result["aliases"] = unique_actor_names([
        *result.get("aliases", []),
        *(alias for item in valid for alias in item.get("aliases", [])),
    ])
    if not result.get("display_name"):
        result["display_name"] = next((str(item.get("display_name") or "") for item in valid if item.get("display_name")), "")
    if not result.get("biography"):
        result["biography"] = next((str(item.get("biography") or "") for item in valid if item.get("biography")), "")
    info: dict[str, Any] = {}
    for item in reversed(valid):
        info.update(item.get("info") or {})
    info.update(result.get("info") or {})
    result["info"] = info
    return result


def lookup_actor_avatar(
    name: str, aliases: list[str] | None = None, catalog_profile_url: str = "",
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if catalog_profile_url:
        try:
            catalog = search_catalog_actor_profile(name, catalog_profile_url)
            if catalog:
                return catalog, errors
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"search_catalog_actor_profile: {exc}")
    for query in unique_actor_names([name, *(aliases or [])])[:6]:
        wikidata: dict[str, Any] | None = None
        try:
            wikidata = search_wikidata_actor_avatar(query)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"search_wikidata_actor_avatar: {exc}")
        try:
            tmdb = search_tmdb_actor_avatar(query)
            if tmdb:
                return merge_actor_results(tmdb, wikidata), errors
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"search_tmdb_actor_avatar: {exc}")
        try:
            bangumi = search_bangumi_actor_profile(query)
            if bangumi:
                return merge_actor_results(bangumi, wikidata), errors
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"search_bangumi_actor_profile: {exc}")
        if wikidata and wikidata.get("avatar"):
            return wikidata, errors
        try:
            wikipedia = search_wikipedia_actor_avatar(query)
            if wikipedia:
                return merge_actor_results(wikipedia, wikidata), errors
        except (OSError, ValueError, urllib.error.URLError) as exc:
            errors.append(f"search_wikipedia_actor_avatar: {exc}")
    try:
        catalog = search_catalog_actor_profile(name, catalog_profile_url)
        if catalog:
            return catalog, errors
    except (OSError, ValueError, urllib.error.URLError) as exc:
        errors.append(f"search_catalog_actor_profile: {exc}")
    return None, errors


def apply_actor_avatar(name_key: str, avatar: str, replace: bool = False) -> int:
    changed = 0
    with connect() as conn:
        profile = conn.execute(
            "SELECT name,aliases_json FROM actor_profiles WHERE name_key=?", (name_key,)
        ).fetchone()
        accepted_names = {name_key}
        if profile:
            accepted_names.update(
                normalize_actor_name(alias)
                for alias in unique_actor_names([profile["name"], *json_value(profile["aliases_json"], [])])
            )
        rows = conn.execute("SELECT id,cast_json FROM movies WHERE exists_now=1 AND cast_json<>'[]'").fetchall()
        for row in rows:
            cast = json_value(row["cast_json"], [])
            dirty = False
            for person in cast:
                if (
                    normalize_actor_name(str(person.get("name") or "")) in accepted_names
                    and (replace or not person.get("avatar"))
                    and person.get("avatar") != avatar
                ):
                    person["avatar"] = avatar
                    dirty = True
            if dirty:
                conn.execute(
                    "UPDATE movies SET cast_json=?,updated_at=? WHERE id=?",
                    (json.dumps(cast, ensure_ascii=False), now_iso(), row["id"]),
                )
                changed += 1
    return changed


def refresh_actor_profiles(
    name_keys: list[str] | None = None, *, only_missing: bool = False,
) -> dict[str, Any]:
    """Refresh actor data; normal refresh skips profiles that already have an avatar."""
    discover_actor_profiles()
    with connect() as conn:
        if name_keys:
            placeholders = ",".join("?" for _ in name_keys)
            rows = conn.execute(
                f"SELECT * FROM actor_profiles WHERE name_key IN ({placeholders}) ORDER BY name_key",
                tuple(name_keys),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM actor_profiles ORDER BY name_key").fetchall()
    scoped_actors = [dict(row) for row in rows]
    actors = [
        actor for actor in scoped_actors
        if not only_missing or not str(actor.get("avatar_url") or "").strip()
    ]
    skipped = len(scoped_actors) - len(actors)
    ACTOR_ENRICH.reset()
    ACTOR_ENRICH.update(running=True, total=len(actors), processed=0, matched=0, localized=0, no_match=0, failed=0)
    avatar_updated = name_updated = photos_added = unchanged = failed = 0
    errors: list[str] = []
    try:
        for index, actor in enumerate(actors, 1):
            ACTOR_ENRICH.update(current=actor["name"])
            old_aliases = json_value(actor.get("aliases_json"), [])
            old_refs = json_value(actor.get("source_refs_json"), {})
            result, lookup_errors = lookup_actor_avatar(
                actor["name"], old_aliases, str(old_refs.get("catalog-actor") or ""),
            )
            old_avatar = str(actor.get("avatar_url") or "")
            old_display = str(actor.get("display_name") or "")
            candidates, candidate_errors = collect_actor_photo_candidates(
                actor["name"], str((result or {}).get("display_name") or old_display),
                result, unique_actor_names(old_aliases + list((result or {}).get("aliases") or [])),
            )
            lookup_errors.extend(candidate_errors)
            if not result and candidates:
                result = {**candidates[0], "display_name": old_display}
            if not result:
                failed += 1
                errors.extend(f"{actor['name']}: {message}" for message in lookup_errors[:2])
                with connect() as conn:
                    conn.execute(
                        "UPDATE actor_profiles SET status=?,last_attempt=?,updated_at=? WHERE name_key=?",
                        ("failed" if lookup_errors else "no_match", now_iso(), now_iso(), actor["name_key"]),
                    )
                ACTOR_ENRICH.update(
                    processed=index, failed=failed, errors=errors[-30:],
                    last_result=f"{actor['name']}：未找到新资料",
                )
                continue
            new_avatar = str(result.get("avatar") or "") or old_avatar
            new_display = str(result.get("display_name") or "") or old_display
            new_aliases = unique_actor_names(old_aliases + list(result.get("aliases") or []) + [actor["name"], new_display])
            biography = str(result.get("biography") or actor.get("biography") or "")
            profile_info = {**json_value(actor.get("info_json"), {}), **(result.get("info") or {})}
            source_refs = dict(old_refs)
            if result.get("profile_url"):
                source_refs[str(result.get("source") or "profile")] = str(result["profile_url"])
            changed = False
            if new_avatar and new_avatar != old_avatar:
                avatar_updated += 1
                changed = True
                apply_actor_avatar(actor["name_key"], new_avatar, replace=True)
            if new_display and new_display != old_display:
                name_updated += 1
                changed = True
            if not changed:
                unchanged += 1
            with connect() as conn:
                conn.execute(
                    """UPDATE actor_profiles SET display_name=?,avatar_url=?,source=?,source_id=?,aliases_json=?,
                    biography=?,info_json=?,source_refs_json=?,status='matched',last_attempt=?,updated_at=?
                    WHERE name_key=?""",
                    (
                        new_display, new_avatar, result.get("source") or actor.get("source") or "",
                        result.get("source_id") or actor.get("source_id") or "",
                        json.dumps(new_aliases, ensure_ascii=False), biography,
                        json.dumps(profile_info, ensure_ascii=False), json.dumps(source_refs, ensure_ascii=False),
                        now_iso(), now_iso(), actor["name_key"],
                    ),
                )
            added_now = store_actor_photos(actor["name_key"], candidates, new_avatar)
            photos_added += added_now
            changes: list[str] = []
            if new_avatar and new_avatar != old_avatar:
                changes.append("头像")
            if new_display and new_display != old_display:
                changes.append("中文姓名")
            if added_now:
                changes.append(f"{added_now} 张照片")
            ACTOR_ENRICH.update(
                processed=index, matched=avatar_updated, localized=name_updated,
                no_match=unchanged, failed=failed, errors=errors[-30:],
                last_result=f"{actor['name']}：" + ("已更新" + "、".join(changes) if changes else "资料无变化"),
            )
            time.sleep(0.08)
    finally:
        ACTOR_ENRICH.update(
            running=False, current="", matched=avatar_updated, localized=name_updated,
            no_match=unchanged, failed=failed, errors=errors[-30:], finished_at=now_iso(),
        )
    return {
        "total": len(actors), "processed": len(actors), "avatar_updated": avatar_updated,
        "name_updated": name_updated, "photos_added": photos_added,
        "unchanged": unchanged, "failed": failed, "skipped": skipped,
        "errors": errors[-30:],
    }


def enrich_actor_avatars(limit: int = 60) -> dict[str, Any]:
    discover_actor_profiles()
    pending = pending_actor_profiles(limit)
    ACTOR_ENRICH.reset()
    ACTOR_ENRICH.update(running=True, total=len(pending), processed=0, matched=0, localized=0, no_match=0, failed=0)
    matched = localized = no_match = failed = 0
    errors: list[str] = []
    try:
        for index, actor in enumerate(pending, 1):
            name = actor["name"]
            ACTOR_ENRICH.update(current=name)
            old_aliases = json_value(actor.get("aliases_json"), [])
            old_refs = json_value(actor.get("source_refs_json"), {})
            result, lookup_errors = lookup_actor_avatar(
                name, old_aliases, str(old_refs.get("catalog-actor") or ""),
            )
            candidates = [result] if result and result.get("avatar") else []
            if not (result or {}).get("avatar"):
                candidates, candidate_errors = collect_actor_photo_candidates(
                    name, str((result or {}).get("display_name") or actor.get("display_name") or ""),
                    result, unique_actor_names(old_aliases + list((result or {}).get("aliases") or [])),
                )
                lookup_errors.extend(candidate_errors)
                if candidates:
                    result = {**(result or {}), **candidates[0]}
            new_avatar = str((result or {}).get("avatar") or "")
            display_name = str((result or {}).get("display_name") or actor.get("display_name") or "")
            avatar = new_avatar or str(actor.get("avatar_url") or "")
            aliases = unique_actor_names(old_aliases + list((result or {}).get("aliases") or []) + [name, display_name])
            biography = str((result or {}).get("biography") or actor.get("biography") or "")
            profile_info = {**json_value(actor.get("info_json"), {}), **((result or {}).get("info") or {})}
            source_refs = dict(old_refs)
            if (result or {}).get("profile_url"):
                source_refs[str((result or {}).get("source") or "profile")] = str(result["profile_url"])
            status = "matched" if avatar else ("failed" if lookup_errors and not result else "no_match")
            if new_avatar and not actor.get("avatar_url"):
                matched += 1
                apply_actor_avatar(actor["name_key"], new_avatar)
            if display_name and not actor.get("display_name"):
                localized += 1
            if not avatar and lookup_errors and not result:
                failed += 1
                errors.extend(f"{name}: {message}" for message in lookup_errors[:2])
            elif not avatar:
                no_match += 1
            with connect() as conn:
                conn.execute(
                    """UPDATE actor_profiles SET display_name=?,avatar_url=?,source=?,source_id=?,aliases_json=?,
                    biography=?,info_json=?,source_refs_json=?,status=?,last_attempt=?,updated_at=? WHERE name_key=?""",
                    (
                        display_name, avatar, (result or {}).get("source", actor.get("source", "")),
                        (result or {}).get("source_id", ""), json.dumps(aliases, ensure_ascii=False), biography,
                        json.dumps(profile_info, ensure_ascii=False), json.dumps(source_refs, ensure_ascii=False),
                        status, now_iso(), now_iso(), actor["name_key"],
                    ),
                )
            added_now = store_actor_photos(actor["name_key"], candidates, avatar)
            result_text = "已补全资料" if new_avatar or (display_name and not actor.get("display_name")) or added_now else (
                "未找到新资料" if not avatar else "资料无变化"
            )
            ACTOR_ENRICH.update(
                processed=index, matched=matched, localized=localized, no_match=no_match, failed=failed,
                errors=errors[-30:], last_result=f"{name}：{result_text}",
            )
            time.sleep(0.08)
    finally:
        ACTOR_ENRICH.update(
            running=False, current="", matched=matched, no_match=no_match, failed=failed,
            localized=localized, errors=errors[-30:], finished_at=now_iso(),
        )
    return ACTOR_ENRICH.snapshot()


def extract_media_code(text: str) -> str:
    """Extract a stable release code from noisy filenames."""
    normalized = unicodedata.normalize("NFKC", text or "").upper().replace("_", "-")
    fc2 = re.search(r"(?<![A-Z0-9])FC2[-\s]?PPV[-\s]?(\d{4,9})(?!\d)", normalized)
    if fc2:
        return f"FC2-PPV-{fc2.group(1)}"
    matches = re.findall(r"(?<![A-Z0-9])([A-Z]{2,10})[-\s]?(\d{2,6})(?!\d)", normalized)
    if not matches:
        return ""
    # Prefer the last code: release-site prefixes often appear before the actual title code.
    prefix, number = matches[-1]
    return f"{prefix}-{number}"


def normalize_match_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").lower()
    value = re.sub(r"\b\d{4,}\s*(?:com|net|org)?\s*@", " ", value, flags=re.I)
    value = re.sub(r"https?://\S+|\b\S+\.(?:com|net|org|tv)\b", " ", value, flags=re.I)
    value = RELEASE_WORDS.sub(" ", value)
    value = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", " ", value)
    return re.sub(r"[^0-9a-z\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", value)


def auto_candidate_confidence(movie: dict[str, Any], candidate: dict[str, Any]) -> float:
    movie_code = extract_media_code(f"{movie.get('title','')} {movie.get('filename','')}")
    candidate_code = extract_media_code(
        f"{candidate.get('id','')} {candidate.get('title','')} {candidate.get('original_title','')}"
    )
    if movie_code and candidate_code and movie_code == candidate_code:
        return 0.995
    target = normalize_match_text(movie.get("title") or movie.get("filename") or "")
    names = [normalize_match_text(candidate.get("title", "")), normalize_match_text(candidate.get("original_title", ""))]
    similarities = [difflib.SequenceMatcher(None, target, name).ratio() for name in names if name]
    score = max(similarities, default=0.0)
    if target and target in names:
        score = max(score, 0.94)
    movie_year, candidate_year = movie.get("year"), candidate.get("year")
    if movie_year and candidate_year:
        distance = abs(int(movie_year) - int(candidate_year))
        score += 0.06 if distance == 0 else (0.02 if distance == 1 else -0.18)
    elif score < 0.94:
        score -= 0.03
    return round(max(0.0, min(0.995, score)), 3)


def record_match_attempt(movie_id: int, status: str, confidence: float = 0, note: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE movies SET match_status=?,match_confidence=?,last_match_attempt=?,match_note=?,updated_at=? WHERE id=?",
            (status, confidence, now_iso(), note[:1000], now_iso(), movie_id),
        )


def auto_match_one(movie: dict[str, Any]) -> str:
    """Match one movie automatically, writing only high-confidence unambiguous results."""
    movie_id = int(movie["id"])
    code = extract_media_code(f"{movie.get('title','')} {movie.get('filename','')}")
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    if code:
        try:
            code_candidates = search_code_catalog(code)
            exact = [
                item for item in code_candidates
                if extract_media_code(f"{item.get('id','')} {item.get('title','')} {item.get('original_title','')}") == code
            ]
            if len(exact) == 1:
                exact[0]["confidence"] = 0.995
                apply_metadata(movie_id, exact[0])
                record_match_attempt(movie_id, "matched", 0.995, f"自动精确编号匹配：{code}")
                return "matched"
            candidates.extend(exact or code_candidates)
        except Exception as exc:
            errors.append(f"编号资料库：{exc}")

    query = movie.get("title") or Path(movie.get("filename", "")).stem
    if setting_value("tmdb_token"):
        try:
            candidates.extend(search_tmdb(query, movie.get("year")))
        except Exception as exc:
            errors.append(f"TMDb：{exc}")
    try:
        candidates.extend(search_bangumi(query))
    except Exception as exc:
        errors.append(f"Bangumi：{exc}")

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (str(candidate.get("provider", "")), str(candidate.get("id", "")))
        candidate["confidence"] = auto_candidate_confidence(movie, candidate)
        if key not in unique or candidate["confidence"] > unique[key]["confidence"]:
            unique[key] = candidate
    ranked = sorted(unique.values(), key=lambda item: item.get("confidence", 0), reverse=True)
    if not ranked:
        record_match_attempt(movie_id, "no_match", 0, "；".join(errors) or "没有候选资料")
        return "no_match"

    best = ranked[0]
    best_score = float(best.get("confidence", 0))
    second_score = float(ranked[1].get("confidence", 0)) if len(ranked) > 1 else 0
    ambiguous = second_score >= best_score - 0.055
    if best_score >= 0.9 and not ambiguous:
        try:
            apply_metadata(movie_id, best)
            record_match_attempt(
                movie_id, "matched", best_score,
                f"自动匹配：{best.get('provider','')} · {best.get('title','')}",
            )
            return "matched"
        except Exception as exc:
            errors.append(f"应用候选：{exc}")
            record_match_attempt(movie_id, "unmatched", best_score, "；".join(errors))
            return "failed"

    preview = " | ".join(
        f"{item.get('provider','')}:{item.get('title','')}({item.get('confidence',0):.2f})" for item in ranked[:3]
    )
    status = "review" if best_score >= 0.55 else "no_match"
    record_match_attempt(movie_id, status, best_score, ("候选冲突；" if ambiguous else "置信度不足；") + preview)
    return status


def auto_match_retry_before() -> str:
    return (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")


def auto_match_pending_count() -> int:
    with connect() as conn:
        return int(conn.execute(
            """SELECT COUNT(*) FROM movies WHERE exists_now=1 AND
            (match_status='unmatched' OR (match_status='no_match' AND (last_match_attempt='' OR last_match_attempt<?)))""",
            (auto_match_retry_before(),),
        ).fetchone()[0])


def auto_match_library(limit: int = 0) -> dict[str, Any]:
    if AUTO_MATCH.snapshot().get("running"):
        raise RuntimeError("自动匹配正在进行")
    with connect() as conn:
        sql = """SELECT * FROM movies WHERE exists_now=1 AND
        (match_status='unmatched' OR (match_status='no_match' AND (last_match_attempt='' OR last_match_attempt<?)))
        ORDER BY updated_at DESC"""
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, (auto_match_retry_before(),)).fetchall()
    movies = [movie_dict(row) for row in rows]
    counters = {"matched": 0, "review": 0, "no_match": 0, "failed": 0}
    errors: list[str] = []
    AUTO_MATCH.update(
        running=True, total=len(movies), processed=0, current="", errors=[],
        matched=0, review=0, no_match=0, failed=0, started_at=now_iso(),
    )
    try:
        for index, movie in enumerate(movies, 1):
            AUTO_MATCH.update(current=movie.get("title", ""), processed=index - 1)
            try:
                result = auto_match_one(movie)
                counters[result if result in counters else "failed"] += 1
            except Exception as exc:
                counters["failed"] += 1
                if len(errors) < 30:
                    errors.append(f"{movie.get('title','')}：{exc}")
                record_match_attempt(int(movie["id"]), "unmatched", 0, str(exc))
            AUTO_MATCH.update(processed=index, errors=errors, **counters)
            time.sleep(0.12)
    finally:
        AUTO_MATCH.update(running=False, current="", finished_at=now_iso(), errors=errors, **counters)
    return AUTO_MATCH.snapshot()


def apply_metadata(movie_id: int, candidate: dict[str, Any]) -> None:
    provider = candidate.get("provider")
    cast: list[dict[str, str]] = []
    genres: list[str] = []
    overview = candidate.get("overview", "")
    poster = candidate.get("poster", "")
    backdrop = ""
    screenshots: list[str] = []
    if provider == "tmdb":
        token = setting_value("tmdb_token")
        media_type = candidate.get("media_type", "movie")
        url = f"https://api.themoviedb.org/3/{media_type}/{candidate['id']}?" + urllib.parse.urlencode({
            "language": "zh-CN", "append_to_response": "credits"
        })
        detail = http_json(url, headers={"Authorization": f"Bearer {token}"})
        overview = detail.get("overview") or overview
        genres = [g.get("name", "") for g in detail.get("genres", []) if g.get("name")]
        poster = f"https://image.tmdb.org/t/p/w780{detail['poster_path']}" if detail.get("poster_path") else poster
        backdrop = f"https://image.tmdb.org/t/p/w1280{detail['backdrop_path']}" if detail.get("backdrop_path") else ""
        for person in (detail.get("credits") or {}).get("cast", [])[:12]:
            cast.append({
                "name": person.get("name", ""), "role": person.get("character", ""),
                "avatar": f"https://image.tmdb.org/t/p/w185{person['profile_path']}" if person.get("profile_path") else "",
            })
    elif provider == "bangumi":
        detail = http_json(f"https://api.bgm.tv/v0/subjects/{candidate['id']}")
        overview = detail.get("summary") or overview
        tags = detail.get("tags") or []
        genres = [tag.get("name", "") for tag in tags[:8] if tag.get("name")]
        people = http_json(f"https://api.bgm.tv/v0/subjects/{candidate['id']}/persons")
        for person in people[:12] if isinstance(people, list) else []:
            images = person.get("images") or {}
            relation = person.get("relation") or person.get("career") or "演出"
            cast.append({
                "name": person.get("name") or person.get("name_cn") or "",
                "role": relation if isinstance(relation, str) else "演出",
                "avatar": images.get("medium") or images.get("large") or "",
            })
    elif provider == "catalog":
        try:
            from javdb.__main__ import fetch_movie_details
        except ImportError as exc:
            raise ValueError("当前版本未包含编号资料库组件") from exc
        metadata, cover, previews = fetch_movie_details(candidate.get("link") or candidate.get("id"))
        title = metadata.get("Title") or candidate.get("title") or ""
        code = metadata.get("DVD ID") or candidate.get("original_title") or ""
        release_date = metadata.get("Release Date") or ""
        candidate["title"] = title
        candidate["original_title"] = code
        candidate["year"] = int(release_date[:4]) if release_date[:4].isdigit() else candidate.get("year")
        overview = metadata.get("Plot") or overview
        poster = cover or poster
        genres = [x.strip() for x in (metadata.get("Genre(s)") or "").split(",") if x.strip()]
        linked_people = fetch_catalog_movie_actors(candidate.get("link") or "")
        people = [x.strip() for x in (metadata.get("Idol(s)/Actress(es)") or "").split(",") if x.strip()]
        cast = linked_people[:12] or [
            {"name": name, "role": "主演", "avatar": "", "source": "catalog-actor"}
            for name in people[:12]
        ]
        screenshots = [p.get("preview") or p.get("img") for p in previews if p.get("preview") or p.get("img")][:10]
    fields = {
        "title": candidate.get("title") or "", "original_title": candidate.get("original_title") or "",
        "year": candidate.get("year"), "media_type": candidate.get("media_type") or "movie",
        "insights_json": "{}",
        "overview": overview, "genres": json.dumps(genres, ensure_ascii=False), "poster_url": poster,
        "backdrop_url": backdrop, "cast_json": json.dumps(cast, ensure_ascii=False), "source": provider,
        "screenshots_json": json.dumps(screenshots, ensure_ascii=False),
        "source_id": str(candidate.get("id", "")), "match_status": "matched",
        "match_confidence": float(candidate.get("confidence") or 1.0),
        "external_rating": candidate.get("external_rating"), "updated_at": now_iso(),
    }
    with connect() as conn:
        assignments = ",".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE movies SET {assignments} WHERE id=?", (*fields.values(), movie_id))


def volume_for_path(path: Path) -> str:
    if sys.platform == "darwin":
        parts = path.resolve().parts
        if len(parts) >= 3 and parts[1] == "Volumes":
            return str(Path("/Volumes") / parts[2])
        return "/"
    return path.drive.upper()


def list_drives() -> list[dict[str, Any]]:
    drives = []
    if os.name == "nt":
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        roots = [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]
    elif sys.platform == "darwin":
        roots = ["/"]
        try:
            roots += [str(p) for p in Path("/Volumes").iterdir() if p.is_dir() and p.resolve() != Path("/")]
        except OSError:
            pass
    else:
        roots = ["/"]
    for root in roots:
        try:
            usage = shutil.disk_usage(root)
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(root) if os.name == "nt" else 3
            if drive_type not in {2, 3, 4}:
                continue
            drives.append({
                "path": root, "name": root.rstrip("\\/") or "Macintosh HD", "total": usage.total, "free": usage.free,
                "total_label": human_size(usage.total), "free_label": human_size(usage.free),
                "kind": {2: "移动磁盘", 3: "本地磁盘", 4: "网络磁盘"}.get(drive_type, "磁盘"),
            })
        except OSError:
            pass
    return drives


class Handler(SimpleHTTPRequestHandler):
    server_version = "YingKu/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if not parsed.path.startswith("/api/"):
                return super().do_GET()
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/api/drives":
                return self.send_json({"drives": list_drives()})
            if parsed.path == "/api/scan/status":
                return self.send_json(SCAN.snapshot())
            if parsed.path == "/api/settings":
                return self.send_json(get_settings())
            if parsed.path == "/api/dashboard":
                return self.send_json(self.dashboard())
            if parsed.path == "/api/movies":
                return self.send_json(self.movies(params))
            match = re.fullmatch(r"/api/movies/(\d+)", parsed.path)
            if match:
                with connect() as conn:
                    row = conn.execute("SELECT * FROM movies WHERE id=?", (int(match.group(1)),)).fetchone()
                return self.send_json(movie_dict(row) if row else {"error": "影片不存在"}, 200 if row else 404)
            if parsed.path == "/api/local-image":
                path = Path(params.get("path", [""])[0])
                return self.send_local_image(path)
            return self.send_json({"error": "接口不存在"}, 404)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            body = self.body_json()
            if parsed.path == "/api/scan":
                if SCAN.snapshot()["running"]:
                    return self.send_json({"error": "扫描正在进行"}, 409)
                roots = [str(Path(p)) for p in body.get("roots", []) if p]
                if not roots:
                    raise ValueError("请至少选择一个扫描位置")
                threading.Thread(target=scan_roots, args=(roots,), daemon=True).start()
                return self.send_json({"started": True, "roots": roots}, 202)
            if parsed.path == "/api/settings":
                allowed = {"tmdb_token"}
                with connect() as conn:
                    for key, value in body.items():
                        if key in allowed and value:
                            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value).strip()))
                return self.send_json({"saved": True})
            if parsed.path == "/api/metadata/search":
                query = str(body.get("query", "")).strip()
                if not query:
                    raise ValueError("请输入搜索关键词")
                provider = body.get("provider", "all")
                results, warnings = [], []
                if provider in {"all", "tmdb"}:
                    try:
                        results.extend(search_tmdb(query, body.get("year")))
                    except Exception as exc:
                        warnings.append(f"TMDb：{exc}")
                if provider in {"all", "bangumi"}:
                    try:
                        results.extend(search_bangumi(query))
                    except Exception as exc:
                        warnings.append(f"Bangumi：{exc}")
                return self.send_json({"results": results, "warnings": warnings})
            match = re.fullmatch(r"/api/movies/(\d+)/(play|folder|metadata)", parsed.path)
            if match:
                movie_id, action = int(match.group(1)), match.group(2)
                with connect() as conn:
                    row = conn.execute("SELECT path FROM movies WHERE id=?", (movie_id,)).fetchone()
                if not row:
                    return self.send_json({"error": "影片不存在"}, 404)
                if action == "metadata":
                    apply_metadata(movie_id, body.get("candidate") or {})
                    return self.send_json({"saved": True})
                target = row["path"] if action == "play" else str(Path(row["path"]).parent)
                if not Path(target).exists():
                    return self.send_json({"error": "文件或目录已不存在"}, 404)
                if action == "play":
                    player.play_movie(target)
                else:
                    os.startfile(target) if os.name == "nt" else subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target])
                return self.send_json({"opened": True})
            return self.send_json({"error": "接口不存在"}, 404)
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except (urllib.error.URLError, TimeoutError) as exc:
            return self.send_json({"error": f"网络请求失败：{exc}"}, 502)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 500)

    def do_PATCH(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        match = re.fullmatch(r"/api/movies/(\d+)", parsed.path)
        if not match:
            return self.send_json({"error": "接口不存在"}, 404)
        try:
            body = self.body_json()
            allowed = {
                "title", "original_title", "year", "overview", "personal_rating", "favorite",
                "watch_status", "disposition", "notes", "tags", "genres", "local_poster",
            }
            fields = {key: value for key, value in body.items() if key in allowed}
            for key in ("tags", "genres"):
                if key in fields:
                    fields[key] = json.dumps(fields[key], ensure_ascii=False)
            if "favorite" in fields:
                fields["favorite"] = 1 if fields["favorite"] else 0
            if not fields:
                raise ValueError("没有可保存的字段")
            fields["updated_at"] = now_iso()
            with connect() as conn:
                assignments = ",".join(f"{key}=?" for key in fields)
                conn.execute(f"UPDATE movies SET {assignments} WHERE id=?", (*fields.values(), int(match.group(1))))
            return self.send_json({"saved": True})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 500)

    def dashboard(self) -> dict[str, Any]:
        with connect() as conn:
            stats = conn.execute(
                """SELECT COUNT(*) total,
                SUM(CASE WHEN favorite=1 THEN 1 ELSE 0 END) favorites,
                SUM(CASE WHEN disposition='delete' THEN 1 ELSE 0 END) delete_count,
                SUM(CASE WHEN match_status IN ('unmatched','review','no_match') THEN 1 ELSE 0 END) unmatched,
                SUM(file_size) bytes,
                SUM(CASE WHEN watch_status='watched' THEN 1 ELSE 0 END) watched
                FROM movies WHERE exists_now=1"""
            ).fetchone()
            duplicates = conn.execute(
                "SELECT COALESCE(SUM(c-1),0) n FROM (SELECT COUNT(*) c FROM movies WHERE fingerprint<>'' AND exists_now=1 GROUP BY fingerprint HAVING c>1)"
            ).fetchone()["n"]
            drives = [dict(r) for r in conn.execute(
                "SELECT drive,COUNT(*) count,SUM(file_size) bytes FROM movies WHERE exists_now=1 GROUP BY drive ORDER BY drive"
            )]
            ratings = [dict(r) for r in conn.execute(
                "SELECT CAST(personal_rating AS INTEGER) rating,COUNT(*) count FROM movies WHERE personal_rating>0 GROUP BY CAST(personal_rating AS INTEGER) ORDER BY rating"
            )]
        result = dict(stats)
        result.update({"duplicates": duplicates, "size_label": human_size(result.pop("bytes") or 0), "drives": drives, "ratings": ratings})
        return result

    def movies(self, params: dict[str, list[str]]) -> dict[str, Any]:
        q = params.get("q", [""])[0].strip()
        view = params.get("view", ["all"])[0]
        sort = params.get("sort", ["updated"])[0]
        page = max(1, int(params.get("page", ["1"])[0]))
        limit = min(120, max(12, int(params.get("limit", ["48"])[0])))
        where = ["exists_now=1"]
        args: list[Any] = []
        if q:
            where.append("(title LIKE ? OR original_title LIKE ? OR filename LIKE ? OR notes LIKE ? OR tags LIKE ? OR cast_json LIKE ?)")
            needle = f"%{q}%"
            args.extend([needle] * 6)
        filters = {
            "favorite": "favorite=1", "unmatched": "match_status IN ('unmatched','review','no_match')", "delete": "disposition='delete'",
            "review": "disposition='review'", "unwatched": "watch_status='unwatched'",
            "duplicates": "fingerprint IN (SELECT fingerprint FROM movies WHERE fingerprint<>'' GROUP BY fingerprint HAVING COUNT(*)>1)",
        }
        if view in filters:
            where.append(filters[view])
        order = {
            "updated": "updated_at DESC", "title": "title COLLATE NOCASE", "year": "year DESC",
            "rating": "personal_rating DESC,updated_at DESC", "size": "file_size DESC",
        }.get(sort, "updated_at DESC")
        where_sql = " AND ".join(where)
        with connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) n FROM movies WHERE {where_sql}", args).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM movies WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
                (*args, limit, (page - 1) * limit),
            ).fetchall()
        return {"items": [movie_dict(r) for r in rows], "total": total, "page": page, "pages": max(1, (total + limit - 1) // limit)}

    def send_local_image(self, path: Path) -> None:
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return self.send_error(HTTPStatus.NOT_FOUND)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    if sys.platform in ("darwin","win32"):
        raise SystemExit("双模式请使用 desktop.py 或影库桌面应用；旧网页入口不提供隐私验证，已停用。")
    parser = argparse.ArgumentParser(description="影库 - 本地影片管理器")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    init_db()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"影库已启动：{url}")
    print("关闭本窗口即可退出。")
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
