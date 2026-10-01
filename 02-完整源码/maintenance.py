"""Local, reviewable library backup and file relinking operations."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

import app as core

BACKUP_FORMAT = 1
ALLOWED = {'film_library.db', 'window-layout.ini', 'image-cache', 'screenshots', 'transferred-media'}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def unique_destination(parent, prefix):
    parent = Path(parent).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    name = f'{prefix}_{datetime.now():%Y-%m-%d_%H%M%S}'
    candidate = parent / name
    index = 2
    while candidate.exists():
        candidate = parent / f'{name}_{index}'
        index += 1
    return candidate


def validate_database(path):
    with closing(sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('备份数据库完整性检查失败')
        for table, fields in {
            'movies': 'id,path,title,cast_json,screenshots_json,local_poster,personal_rating,hidden_by_app,original_file_attributes',
            'scan_roots': 'path', 'settings': 'key,value',
            'actor_profiles': 'name_key,avatar_url', 'actor_photos': 'photo_url',
            'hidden_folders': 'path,original_file_attributes',
        }.items():
            conn.execute(f'SELECT {fields} FROM {table} LIMIT 0')
        return conn.execute('SELECT COUNT(*) FROM movies').fetchone()[0]


def create_backup(parent):
    """Use SQLite's online snapshot API; keep media outside the backup."""
    parent = Path(parent).expanduser().resolve()
    data = core.DATA_DIR.resolve()
    if parent == data or data in parent.parents:
        raise ValueError('备份位置不能放在影库数据目录内部，请选择其他文件夹')
    destination = unique_destination(parent, '影库备份')
    stage = Path(tempfile.mkdtemp(prefix='.yingku-backup-', dir=parent))
    try:
        with core.connect() as source, closing(sqlite3.connect(stage / 'film_library.db')) as target:
            source.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
        for name in ALLOWED - {'film_library.db'}:
            item = data / name
            if not item.exists():
                continue
            if item.is_symlink() or (item.is_dir() and any(p.is_symlink() for p in item.rglob('*'))):
                raise ValueError('数据缓存中含有符号链接，已停止备份以避免复制外部文件')
            if item.is_dir():
                shutil.copytree(item, stage / name)
            else:
                shutil.copy2(item, stage / name)
        count = validate_database(stage / 'film_library.db')
        manifest = dict(format=BACKUP_FORMAT, app_version='2.8.2', platform=sys.platform,
                        created_at=core.now_iso(), source_data_dir=str(data), movies=count,
                        files={str(p.relative_to(stage)): sha256(p) for p in stage.rglob('*') if p.is_file()})
        (stage / '影库备份.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        stage.rename(destination)
        return str(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def inspect_backup(folder):
    folder = Path(folder).expanduser().resolve()
    manifest = json.loads((folder / '影库备份.json').read_text(encoding='utf-8'))
    if manifest.get('format') != BACKUP_FORMAT or manifest.get('platform') != sys.platform:
        raise ValueError('请选择本平台生成的影库备份；跨平台旧库需要单独迁移')
    import privacy
    privacy.check_backup_mode(folder)
    files = manifest.get('files')
    if not isinstance(files, dict) or 'film_library.db' not in files:
        raise ValueError('备份缺少影片数据库')
    for name, digest in files.items():
        rel = Path(name)
        if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] not in ALLOWED:
            raise ValueError('备份包含不允许的文件路径')
        p = folder / rel
        if not p.is_file() or p.resolve() != p or sha256(p) != digest:
            raise ValueError(f'备份文件缺失或校验失败：{name}')
    actual_count = validate_database(folder / 'film_library.db')
    return {**manifest, 'movies': actual_count, 'folder': str(folder)}


def remap_value(value, old_root, new_root):
    if isinstance(value, str):
        try:
            relative = Path(value).relative_to(old_root)
            return str(Path(new_root) / relative)
        except (ValueError, TypeError):
            return value
    if isinstance(value, list):
        return [remap_value(item, old_root, new_root) for item in value]
    if isinstance(value, dict):
        return {key: remap_value(item, old_root, new_root) for key, item in value.items()}
    return value


def restore_backup(folder):
    """Call only before GUI/database workers start, with the application lock held."""
    info = inspect_backup(folder)
    data = core.DATA_DIR.resolve()
    if Path(info['folder']) == data or data in Path(info['folder']).parents:
        raise ValueError('不能从当前数据目录内部恢复，请先将备份复制到其他位置')
    data.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.yingku-restore-', dir=data.parent))
    rollback = None
    try:
        for name in info['files']:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(info['folder']) / name, target)
            if sha256(target) != info['files'][name]:
                raise ValueError('复制期间备份发生变化，已停止恢复')
        with closing(sqlite3.connect(stage / 'film_library.db')) as conn:
            for row in conn.execute('SELECT id,local_poster,screenshots_json,cast_json FROM movies').fetchall():
                poster = remap_value(row[1], info['source_data_dir'], str(data))
                screenshots = remap_value(json.loads(row[2] or '[]'), info['source_data_dir'], str(data))
                cast = remap_value(json.loads(row[3] or '[]'), info['source_data_dir'], str(data))
                conn.execute('UPDATE movies SET local_poster=?,screenshots_json=?,cast_json=? WHERE id=?',
                             (poster,json.dumps(screenshots,ensure_ascii=False),json.dumps(cast,ensure_ascii=False),row[0]))
            for table, column in [('actor_profiles','avatar_url'),('actor_photos','photo_url')]:
                for rowid,value in conn.execute(f'SELECT rowid,{column} FROM {table}').fetchall():
                    conn.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                                 (remap_value(value,info['source_data_dir'],str(data)),rowid))
            conn.commit()
        validate_database(stage / 'film_library.db')
        safety = create_backup(data.parent / 'YingKu-恢复前备份') if core.DB_PATH.exists() else ''
        if data.exists():
            rollback = unique_destination(data.parent / 'YingKu-恢复前备份', '替换前原始数据')
            data.rename(rollback)
        try:
            stage.rename(data)
        except BaseException:
            if rollback is not None and not data.exists():
                rollback.rename(data)
            raise
        # Retain the original directory too, including any files unknown to this version.
        return dict(safety_backup=safety, original_directory=str(rollback or ''), movies=info['movies'])
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def plan_relink(old_root, new_root):
    old = str(Path(old_root).expanduser().resolve())
    new = Path(new_root).expanduser().resolve()
    if not new.is_dir():
        raise ValueError('新位置不存在或无法读取')
    with core.connect() as conn:
        rows = [dict(row) for row in conn.execute("SELECT * FROM movies WHERE file_status<>'trashed'")
                if core.path_is_within(row['path'], old)]
        occupied = {row['path']: dict(row) for row in conn.execute('SELECT * FROM movies')}
    by_size = defaultdict(list)
    for parent, directories, files in os.walk(new):
        directories[:] = [d for d in directories if not d.startswith('.') and d.lower() not in core.SKIP_DIRS
                          and not (Path(parent)/d).is_symlink()]
        for name in files:
            p = Path(parent)/name
            if p.suffix.lower() not in core.VIDEO_EXTENSIONS or p.is_symlink():
                continue
            try:
                by_size[p.stat().st_size].append(p)
            except OSError:
                continue
    fingerprints = {}
    result = []
    for movie in rows:
        item = dict(id=movie['id'], title=movie['title'], old_path=movie['path'], new_path='',
                    status='未找到', fingerprint=movie['fingerprint'], file_size=movie['file_size'])
        if Path(movie['path']).exists():
            item['status'] = '原文件仍在，保留原关联'
        elif not movie['fingerprint']:
            item['status'] = '没有原文件指纹，需人工核对'
        else:
            candidates = []
            for p in by_size[movie['file_size']]:
                key = str(p)
                if key not in fingerprints:
                    fingerprints[key] = core.fast_fingerprint(p, movie['file_size'])
                if fingerprints[key] == movie['fingerprint']:
                    candidates.append(p)
            exact = new / Path(movie['path']).relative_to(old)
            if exact in candidates:
                candidates = [exact]
            if len(candidates) == 1:
                target = str(candidates[0])
                conflict = occupied.get(target)
                if conflict and conflict['id'] != movie['id'] and not replaceable_scan_record(conflict):
                    item['status'] = '新文件已有个人整理，暂不合并'
                else:
                    item.update(new_path=target, status='可关联：大小与指纹一致')
                    if conflict and conflict['id'] != movie['id']:
                        item.update(merge_id=conflict['id'], status='可关联：合并自动扫描记录，保留旧资料')
            elif len(candidates) > 1:
                item['status'] = '多个文件指纹相同，需人工核对'
        result.append(item)
    targets = defaultdict(list)
    for item in result:
        if item['new_path']:
            targets[item['new_path']].append(item)
    for matches in targets.values():
        if len(matches) > 1:
            for item in matches:
                item.update(new_path='', status='多个旧记录对应同一文件，暂不合并')
    return dict(old_root=old, new_root=str(new), items=result)


def replaceable_scan_record(row):
    return (not row['favorite'] and not row['personal_rating'] and not row['play_count']
            and not row['notes'] and not core.json_value(row['tags'], [])
            and row['watch_status'] == 'unwatched' and row['disposition'] == 'keep'
            and row['match_status'] != 'manual' and row['file_status'] != 'trashed')


def apply_relink(plan, selected_ids):
    selected = [item for item in plan['items'] if item['id'] in set(selected_ids)]
    if not selected:
        raise ValueError('请选择至少一个已核对的文件')
    import privacy
    if privacy.MODE:
        exclusions = privacy.scan_exclusions()
        for item in selected:
            if privacy.blocked_path(item.get('new_path', ''), item.get('fingerprint', ''), exclusions):
                raise ValueError('所选文件属于另一模式，已停止重新关联。')
    old, new = plan['old_root'], plan['new_root']
    with core.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        validated = []
        for item in selected:
            row = conn.execute('SELECT * FROM movies WHERE id=?', (item['id'],)).fetchone()
            p = Path(item['new_path'])
            if (not row or row['path'] != item['old_path'] or row['file_status'] == 'trashed'
                or Path(row['path']).exists() or not item['new_path'] or p.is_symlink()
                or not core.path_is_within(str(p.resolve()), new) or not p.is_file()
                or p.stat().st_size != row['file_size']
                or core.fast_fingerprint(p, row['file_size']) != row['fingerprint']):
                raise ValueError('预览后的文件或记录已变化，请重新预览；未修改任何关联')
            conflict = conn.execute('SELECT * FROM movies WHERE path=? AND id<>?', (str(p),row['id'])).fetchone()
            if conflict and (conflict['id'] != item.get('merge_id') or not replaceable_scan_record(conflict)):
                raise ValueError('新文件已有个人整理或记录变化；未修改任何关联')
            validated.append((row,p,conflict))
        for row,p,conflict in validated:
            if conflict:
                conn.execute('DELETE FROM movies WHERE id=?', (conflict['id'],))
            attributes = core.get_file_attributes(p)
            owner = conflict if conflict and conflict['hidden_by_app'] else row
            owned_hidden = bool(owner['hidden_by_app'] and attributes >= 0 and attributes & core.FILE_ATTRIBUTE_HIDDEN)
            original = (attributes & ~core.FILE_ATTRIBUTE_HIDDEN) | (owner['original_file_attributes'] & core.FILE_ATTRIBUTE_HIDDEN) if owned_hidden else -1
            poster = remap_value(row['local_poster'], old, new)
            if poster and not Path(poster).is_file():
                poster = row['local_poster']
            cast = remap_value(core.json_value(row['cast_json'], []), old, new)
            screenshots = remap_value(core.json_value(row['screenshots_json'], []), old, new)
            conn.execute("""UPDATE movies SET path=?,filename=?,modified_at=?,drive=?,exists_now=1,file_status='available',
                          local_poster=?,cast_json=?,screenshots_json=?,hidden_by_app=?,original_file_attributes=? WHERE id=?""",
                         (str(p),p.name,p.stat().st_mtime,core.volume_for_path(p),poster,json.dumps(cast,ensure_ascii=False),
                         json.dumps(screenshots,ensure_ascii=False),int(owned_hidden),original,row['id']))
        for table, column in [('actor_profiles','avatar_url'),('actor_photos','photo_url')]:
            for row in conn.execute(f'SELECT rowid AS rid,{column} FROM {table}').fetchall():
                mapped = remap_value(row[column], old, new)
                if mapped != row[column] and Path(mapped).is_file():
                    conn.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (mapped,row['rid']))
        conn.execute('INSERT OR IGNORE INTO scan_roots(path,enabled) VALUES(?,1)', (new,))
        remaining = [r for r in conn.execute("SELECT path FROM movies WHERE file_status<>'trashed'")
                     if core.path_is_within(r['path'],old)]
        if not remaining and old != new:
            conn.execute('DELETE FROM scan_roots WHERE path=?', (old,))
        # Transfer only app-owned, missing folders whose mapped folder is still hidden.
        for row in conn.execute('SELECT * FROM hidden_folders').fetchall():
            if not core.path_is_within(row['path'],old) or Path(row['path']).exists():
                continue
            mapped = Path(remap_value(row['path'], old, new))
            attrs = core.get_file_attributes(mapped)
            if mapped != Path(new) and attrs >= 0 and attrs & core.FILE_ATTRIBUTE_HIDDEN:
                original = (attrs & ~core.FILE_ATTRIBUTE_HIDDEN) | (row['original_file_attributes'] & core.FILE_ATTRIBUTE_HIDDEN)
                conn.execute('INSERT OR IGNORE INTO hidden_folders(path,root_path,original_file_attributes,created_at) VALUES(?,?,?,?)',
                             (str(mapped),new,original,core.now_iso()))
        return len(validated)
