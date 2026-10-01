"""Clear only IINA playback traces, after IINA has stopped writing them."""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

# Keep preferences, plugins, subtitles, downloaded files and other applications intact.
TRACE_PATHS = (
    'Application Support/com.colliderli.iina/history.plist',
    'Application Support/com.colliderli.iina/watch_later',
    'Caches/com.colliderli.iina/thumb_cache',
    'Saved Application State/com.colliderli.iina.savedState',
)


def _clear_files(library: Path) -> int:
    removed = 0
    for relative in TRACE_PATHS:
        target = library / relative
        # Never follow a redirected parent into a media directory or another app.
        if any(parent.is_symlink() for parent in target.parents if parent != library and library in parent.parents):
            raise RuntimeError('IINA 记录目录被重定向，已停止清理以保护其他文件。')
        if target.is_symlink() or target.is_file():
            target.unlink()
            removed += 1
        elif target.is_dir():
            shutil.rmtree(target)
            removed += 1
    return removed


def clear_playback_history() -> int:
    if sys.platform != 'darwin':
        return 0
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    helper = str(root / 'native' / 'yingku-iina-session')
    recent_error = None
    try:
        subprocess.run([helper, 'clear-recents'], check=True, capture_output=True, timeout=12)
    except (OSError, subprocess.SubprocessError) as exc:
        message = getattr(exc, 'stderr', b'') or b''
        recent_error = message.decode('utf-8', errors='replace') if isinstance(message, bytes) else message
        recent_error = recent_error or 'IINA 最近打开菜单未清理。'
    try:
        # Graceful termination drains IINA's pending history writes before deletion.
        subprocess.run([helper, 'stop'], check=True, capture_output=True, timeout=20)
        subprocess.run([helper, 'clear-preferences'], check=True, capture_output=True, timeout=8)
        removed = _clear_files(Path.home() / 'Library')
        if recent_error:
            raise RuntimeError('已清理播放历史和续播记录，但' + recent_error)
        return removed
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('IINA 播放记录未能全部清理，请关闭 IINA 后重新打开并退出影库以重试。') from exc


def request_recent_menu_access() -> bool:
    if sys.platform != 'darwin':
        return True
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    try:
        result = subprocess.run([str(root / 'native' / 'yingku-iina-session'), 'request-access'],
                                check=True, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() == 'authorized'
    except (OSError, subprocess.SubprocessError):
        return False
