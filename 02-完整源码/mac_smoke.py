"""Opt-in, isolated QA entry point for the packaged app."""
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, qVersion
from PySide6.QtGui import QFontDatabase
import app as core


def run(qt_app, window, output):
    if not os.environ.get('YINGKU_DATA_DIR') or os.environ.get('YINGKU_DISABLE_STARTUP_TASKS') != '1':
        raise RuntimeError('Smoke testing requires an explicit isolated data directory and disabled startup tasks')
    target = Path(output)
    target.mkdir(parents=True, exist_ok=True)
    if window.cards:
        window.show_movie(window.cards[0].movie['id'])
    def capture():
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            import javdb.__main__
            import certifi
            from desktop import ScanDialog, ManualEditDialog, get_movie, extract_keyframes
            from maintenance_ui import MaintenanceDialog, RelinkDialog
            window.grab().save(str(target / '主界面.png'))
            scan = ScanDialog(window)
            scan.show()
            qt_app.processEvents()
            scan.grab().save(str(target / '扫描源管理.png'))
            scan.close()
            maintenance = MaintenanceDialog(window)
            maintenance.show()
            qt_app.processEvents()
            maintenance.grab().save(str(target / '备份与关联.png'))
            maintenance.close()
            relink = RelinkDialog(window)
            relink.show()
            qt_app.processEvents()
            relink.grab().save(str(target / '关联预览窗口.png'))
            relink.close()
            if window.cards:
                editor = ManualEditDialog(get_movie(window.cards[0].movie['id']),window)
                editor.show()
                qt_app.processEvents()
                editor.grab().save(str(target / '全部演员编辑.png'))
                editor.close()
            ffmpeg = subprocess.run([get_ffmpeg_exe(), '-version'], capture_output=True, text=True, timeout=15, check=True)
            with core.connect() as conn:
                integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
                count = conn.execute('SELECT COUNT(*) FROM movies').fetchone()[0]
                paths = [row[0] for row in conn.execute('SELECT path FROM movies')]
            sample = next((p for p in paths if Path(p).is_file() and Path(p).is_relative_to(core.DATA_DIR)), None)
            extracted = len(extract_keyframes(sample, target / '本地截图验证')) if sample else 0
            report = dict(architecture=platform.machine(), qt=qVersion(), database_integrity=integrity,
                          movie_count=count, data_dir=str(core.DATA_DIR), font_available=('Microsoft YaHei UI' if sys.platform=='win32' else 'PingFang SC') in QFontDatabase.families(),
                          ffmpeg=ffmpeg.stdout.splitlines()[0], extracted_frames=extracted,
                          cert_bundle=Path(certifi.where()).is_file(), catalog_import=True)
            (target / '运行验证.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            window.close()
            qt_app.quit()
        except Exception:
            import traceback
            (target / '失败日志.txt').write_text(traceback.format_exc())
            qt_app.exit(1)
    QTimer.singleShot(1600, capture)
