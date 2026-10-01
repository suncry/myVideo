"""Two isolated local libraries, with macOS authentication at each private entry.

This is an application access gate, not encryption of the user's media or SQLite files.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import app as core

MODE = None  # Configured only by the desktop entry point; unit tests use isolated stores.
BASE = None
AUTHENTICATING = False


def library_paths(base):
    base = Path(base).resolve()
    return {'private': base, 'public': base.with_name(base.name + '-Public')}


def configure(base, private=False):
    global MODE, BASE
    BASE = Path(base).resolve()
    MODE = 'private' if private else 'public'
    core.DATA_DIR = library_paths(BASE)[MODE]
    core.DB_PATH = core.DATA_DIR / 'film_library.db'


def stamp_library():
    if MODE:
        with core.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES('privacy_mode',?)", (MODE,))
        os.chmod(core.DATA_DIR, 0o700)
        os.chmod(core.DB_PATH, 0o600)


def other_data():
    if not MODE or BASE is None:
        raise RuntimeError('双模式尚未初始化')
    return library_paths(BASE)['public' if MODE == 'private' else 'private']


def path_digest(path):
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()


def scan_exclusions():
    """Never surface the other library's labels; protect copies and moved-out items."""
    if not MODE:
        return set(), set()
    paths, fingerprints = set(), set()
    with core.connect() as conn:
        for row in conn.execute('SELECT path_hash,fingerprint FROM privacy_exclusions'):
            paths.add(row[0])
            if row[1]: fingerprints.add(row[1])
    other = other_data() / 'film_library.db'
    if other.exists():
        try:
            with closing(sqlite3.connect(f'{other.as_uri()}?mode=ro', uri=True)) as conn:
                for path, fingerprint in conn.execute('SELECT path,fingerprint FROM movies'):
                    paths.add(path_digest(path))
                    if fingerprint: fingerprints.add(fingerprint)
        except sqlite3.Error as exc:
            raise RuntimeError('暂时无法核对另一资料库，已停止导入以避免内容混入。') from exc
    return paths, fingerprints


def blocked_path(path, fingerprint='', exclusions=None):
    paths, fingerprints = exclusions if exclusions is not None else scan_exclusions()
    return path_digest(path) in paths or bool(fingerprint and fingerprint in fingerprints)


def _load_auth_bridge():
    if sys.platform=='win32':
        from windows_auth import WindowsAuthBridge
        return WindowsAuthBridge()
    import ctypes
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    bridge = ctypes.CDLL(str(root / 'native' / 'libyingku-auth.dylib'))
    for name in ('yingku_auth_begin', 'yingku_auth_status', 'yingku_auth_available'):
        function = getattr(bridge, name)
        function.argtypes, function.restype = [], ctypes.c_int32
    bridge.yingku_auth_cancel.argtypes = []
    bridge.yingku_auth_cancel.restype = None
    return bridge


def authenticate(parent=None):
    """System authentication inside the GUI process; cancellation returns quietly."""
    global AUTHENTICATING
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
    if AUTHENTICATING:
        return False
    try:
        bridge = _load_auth_bridge()
        if sys.platform=='win32':bridge.set_parent_window(parent)
    except (OSError, AttributeError):
        QMessageBox.warning(parent, '无法验证', '系统验证组件无法加载，请重新安装影库。将进入普通模式。')
        return False
    AUTHENTICATING = True
    loop = QEventLoop()
    poll = QTimer()
    timeout = QTimer()
    timeout.setSingleShot(True)
    result = [False]
    was_enabled = parent.isEnabled() if parent is not None else True
    def check():
        status = bridge.yingku_auth_status()
        if status != 0:
            result[0] = status == 1
            loop.quit()
    poll.timeout.connect(check)
    timeout.timeout.connect(loop.quit)
    application = QApplication.instance()
    application.aboutToQuit.connect(loop.quit)
    try:
        if parent is not None:
            parent.show()
            parent.raise_()
            parent.activateWindow()
            parent.setEnabled(False)
        application.processEvents()
        if bridge.yingku_auth_begin() != 0:
            return False
        poll.start(50)
        timeout.start(120000)
        loop.exec()
        return result[0]
    finally:
        poll.stop()
        timeout.stop()
        bridge.yingku_auth_cancel()
        application.aboutToQuit.disconnect(loop.quit)
        AUTHENTICATING = False
        if parent is not None:
            parent.setEnabled(was_enabled)
            parent.raise_()
            parent.activateWindow()


def check_backup_mode(folder):
    if not MODE:
        return
    db = Path(folder).resolve() / 'film_library.db'
    with closing(sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True)) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key='privacy_mode'").fetchone()
    scope = row[0] if row else 'private'  # All legacy backups are private by default.
    if scope != MODE:
        raise ValueError('备份所属模式与当前模式不同。请翻转到对应模式后恢复；旧版本备份请在私密模式恢复。')
    exclusions = scan_exclusions()
    with closing(sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True)) as conn:
        if any(blocked_path(path, fingerprint, exclusions) for path, fingerprint in conn.execute('SELECT path,fingerprint FROM movies')):
            raise ValueError('备份含已分配到另一模式的影片，已停止恢复以避免混入。请使用分区整理后的备份。')


def move_movie(movie_id):
    """Call after authentication and while all workers are idle. No media is moved.

    DELETE journaling provides an atomic attached-database transaction. Shared application
    locking ensures that neither store has another desktop writer during this operation.
    """
    if not MODE:
        raise RuntimeError('双模式尚未初始化')
    destination = other_data()
    target_db = destination / 'film_library.db'
    original_data, original_db = core.DATA_DIR, core.DB_PATH
    try:
        core.DATA_DIR, core.DB_PATH = destination, target_db
        core.init_db()
    finally:
        core.DATA_DIR, core.DB_PATH = original_data, original_db
    destination.chmod(0o700)
    target_db.chmod(0o600)
    target_mode = 'public' if MODE == 'private' else 'private'
    copied = []
    def copy_value(value):
        if isinstance(value, list): return [copy_value(v) for v in value]
        if isinstance(value, dict): return {k:copy_value(v) for k,v in value.items()}
        if not isinstance(value, str): return value
        try: relative = Path(value).relative_to(original_data)
        except ValueError: return value
        source = Path(value)
        if not source.is_file(): return ''
        if source.is_symlink() or not source.resolve().is_relative_to(original_data.resolve()):
            raise ValueError('缓存路径异常，已停止移动')
        target = destination / 'transferred-media' / (hashlib.sha256(source.read_bytes()).hexdigest() + source.suffix)
        target.parent.mkdir(exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
            copied.append(target)
        return str(target)
    try:
        with closing(sqlite3.connect(original_db, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA secure_delete=ON')
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('ATTACH DATABASE ? AS target', (str(target_db),))
            conn.execute('PRAGMA target.journal_mode=DELETE')
            conn.execute('BEGIN IMMEDIATE')
            try:
                source = conn.execute('SELECT * FROM movies WHERE id=?', (movie_id,)).fetchone()
                if source is None: raise ValueError('影片已不存在')
                row = dict(source)
                if conn.execute('SELECT 1 FROM target.movies WHERE path=? OR (fingerprint<>\'\' AND fingerprint=?)', (row['path'],row['fingerprint'])).fetchone():
                    raise ValueError('另一模式已存在这部影片，请先处理重复记录。')
                for field in ('local_poster',): row[field] = copy_value(row[field])
                frame_key = row.get('fingerprint') or hashlib.sha1(row['path'].encode()).hexdigest()[:20]
                local_frames = sorted((original_data / 'screenshots' / frame_key).glob('frame_*.jpg'))
                if local_frames:
                    row['screenshots_json'] = json.dumps([str(p) for p in local_frames])
                cast = core.json_value(row['cast_json'], [])
                for field in ('cast_json','screenshots_json'):
                    row[field] = json.dumps(copy_value(core.json_value(row[field], [])), ensure_ascii=False)
                del row['id']
                columns = list(row)
                new_id = conn.execute(f"INSERT INTO target.movies({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", list(row.values())).lastrowid
                for person in cast:
                    names = core.unique_actor_names([person.get('name',''), *person.get('aliases',[])])
                    keys = {core.normalize_actor_name(name) for name in names}
                    for profile in conn.execute('SELECT * FROM actor_profiles').fetchall():
                        aliases = {core.normalize_actor_name(n) for n in core.json_value(profile['aliases_json'],[])}
                        if profile['name_key'] not in keys and not aliases.intersection(keys): continue
                        fields = dict(profile)
                        fields['avatar_url'] = copy_value(fields['avatar_url'])
                        conn.execute(f"INSERT OR IGNORE INTO target.actor_profiles({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", list(fields.values()))
                # Keep ownership of the original file flags with the movie.
                parent = str(Path(row['path']).parent)
                hidden = conn.execute('SELECT * FROM hidden_folders WHERE path=?', (parent,)).fetchone()
                if hidden:
                    fields = dict(hidden)
                    conn.execute(f"INSERT OR IGNORE INTO target.hidden_folders({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",list(fields.values()))
                digest = path_digest(row['path'])
                conn.execute('INSERT OR REPLACE INTO privacy_exclusions VALUES(?,?)', (digest,row['fingerprint']))
                conn.execute('DELETE FROM target.privacy_exclusions WHERE path_hash=? OR (fingerprint<>\'\' AND fingerprint=?)',(digest,row['fingerprint']))
                conn.execute("INSERT OR REPLACE INTO target.settings VALUES('privacy_mode',?)", (target_mode,))
                conn.execute('DELETE FROM movies WHERE id=?',(movie_id,))
                core._prune_unused_actor_profiles(conn)
                conn.commit()
                try:
                    prune_unused_cache(conn, original_data)
                except Exception:
                    pass
                return new_id
            except BaseException:
                conn.rollback()
                raise
    except BaseException:
        for item in copied: item.unlink(missing_ok=True)
        raise


def prune_unused_cache(conn, data):
    """Remove app-owned orphan images after partition moves; never touch source media."""
    values=set()
    def gather(value):
        if isinstance(value,str):
            values.add(value)
            if value.startswith(('[','{')):
                try:gather(json.loads(value))
                except ValueError:pass
        elif isinstance(value,dict):
            for v in value.values():gather(v)
        elif isinstance(value,(list,tuple)):
            for v in value:gather(v)
    frames=set()
    for row in conn.execute('SELECT * FROM movies'):
        gather(dict(row))
        frames.add(row['fingerprint'] or hashlib.sha1(row['path'].encode()).hexdigest()[:20])
    for table in ('actor_profiles','actor_photos'):
        for row in conn.execute(f'SELECT * FROM {table}'):gather(dict(row))
    cache_names={hashlib.sha256(v.encode()).hexdigest()+'.img' for v in values if v.startswith(('http://','https://'))}
    for directory in ('image-cache','screenshots','transferred-media'):
        root=Path(data)/directory
        if not root.exists() or root.is_symlink():continue
        for path in root.rglob('*'):
            if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):continue
            referenced=str(path) in values
            if directory=='image-cache':referenced=referenced or path.name in cache_names
            if directory=='screenshots':referenced=referenced or path.relative_to(root).parts[0] in frames
            if not referenced:path.unlink()
