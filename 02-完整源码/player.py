"""Explicit movie playback; system video defaults are repaired independently."""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

IINA_BUNDLE_ID = 'com.colliderli.iina'
VIDEO_EXTENSIONS = ('mp4', 'mkv', 'avi', 'mov', 'wmv', 'm4v', 'flv', 'webm',
                    'ts', 'm2ts', 'mts', 'mpg', 'mpeg', 'vob', 'rm', 'rmvb',
                    '3gp', '3g2', 'm2v', 'ogv', 'asf', 'f4v', 'mk3d')


def play_movie(path: str) -> None:
    movie = Path(path).expanduser().resolve()
    if not movie.is_file():
        raise RuntimeError('影片文件暂时无法访问，请检查硬盘或重新关联文件夹。')
    try:
        if sys.platform == 'darwin':
            # No default-handler lookup and no shell interpolation, even for unusual filenames.
            subprocess.run(['/usr/bin/open', '-b', IINA_BUNDLE_ID, str(movie)],
                           check=True, capture_output=True, timeout=8)
        elif os.name == 'nt':
            os.startfile(str(movie))
        else:
            subprocess.run(['xdg-open', str(movie)], check=True, capture_output=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as exc:
        message = ('无法使用 IINA 播放，请确认 IINA 已安装且可以正常打开。'
                   if sys.platform == 'darwin' else '无法打开播放器，请检查默认播放器设置。')
        raise RuntimeError(message) from exc


def restore_video_defaults() -> int:
    if sys.platform != 'darwin':
        return 0
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    helper = root / 'native' / 'yingku-video-defaults'
    try:
        result = subprocess.run([str(helper), 'repair', *VIDEO_EXTENSIONS],
                                check=True, capture_output=True, text=True, timeout=60)
        report = json.loads(result.stdout)
        if not report.get('installed'):
            raise RuntimeError('未找到 IINA，请先安装 IINA。')
        rows = report.get('associations', [])
        covered = {row.get('extension') for row in rows}
        if covered != set(VIDEO_EXTENSIONS) or any(
            row.get('status') != 0 or str(row.get('after', '')).lower() != IINA_BUNDLE_ID for row in rows
        ):
            raise RuntimeError('部分视频文件关联未恢复；影库内播放仍会直接使用 IINA。')
        return sum(str(row.get('before', '')).lower() != IINA_BUNDLE_ID for row in rows)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError('视频文件关联检查未完成；影库内播放仍会直接使用 IINA。') from exc
