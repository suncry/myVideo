"""macOS adaptation tests; only disposable fixture files are touched."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QFile

import app
import desktop


@unittest.skipUnless(sys.platform == 'darwin', 'macOS integration')
class MacIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingku-mac-test-')
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.patches = [mock.patch.object(app, 'DATA_DIR', self.data),
                        mock.patch.object(app, 'DB_PATH', self.data / 'test.db')]
        for patch in self.patches:
            patch.start()
        app.init_db()
        self.media = self.root / 'media'
        self.media.mkdir()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def movie(self, folder=None):
        p = (folder or self.media) / '中文 空格影片.mp4'
        p.write_bytes(b'isolated test fixture')
        return p

    def scan(self):
        with mock.patch.object(app, 'probe_video_duration', return_value=660):
            app.scan_roots([str(self.media)])
        return desktop.query_movies()[0]

    def test_native_hide_restore_preserves_unrelated_flags(self):
        p = self.movie()
        original = p.stat().st_flags | stat.UF_NODUMP
        os.chflags(p, original)
        changed, saved = app.hide_media_file(p, protected=True)
        self.assertTrue(changed)
        self.assertEqual(saved, original)
        self.assertEqual(p.stat().st_flags, original | stat.UF_HIDDEN)
        self.assertTrue(app.restore_media_file(p, saved))
        self.assertEqual(p.stat().st_flags, original)

    def test_scan_remove_restores_file_folder_and_keeps_root_visible(self):
        folder = self.media / '电影目录'
        folder.mkdir()
        p = self.movie(folder)
        self.scan()
        self.assertTrue(p.stat().st_flags & stat.UF_HIDDEN)
        self.assertTrue(folder.stat().st_flags & stat.UF_HIDDEN)
        self.assertFalse(self.media.stat().st_flags & stat.UF_HIDDEN)
        self.assertEqual(app.remove_scan_root(str(self.media)), 1)
        self.assertFalse(p.stat().st_flags & stat.UF_HIDDEN)
        self.assertFalse(folder.stat().st_flags & stat.UF_HIDDEN)
        self.assertTrue(p.is_file())

    def test_user_hidden_file_is_not_unhidden_by_remove(self):
        p = self.movie()
        os.chflags(p, p.stat().st_flags | stat.UF_HIDDEN)
        movie = self.scan()
        self.assertEqual(movie['hidden_by_app'], 0)
        app.remove_scan_root(str(self.media))
        self.assertTrue(p.stat().st_flags & stat.UF_HIDDEN)

    def test_symlink_does_not_change_target_attributes(self):
        p = self.movie()
        link = self.media / 'alias.mp4'
        link.symlink_to(p)
        self.assertFalse(app.hide_media_file(link)[0])
        self.assertFalse(p.stat().st_flags & stat.UF_HIDDEN)

    def test_offline_root_keeps_ratings(self):
        self.movie()
        movie = self.scan()
        desktop.update_movie(movie['id'], favorite=True, personal_rating=10)
        moved = self.media.with_name('disconnected')
        self.media.rename(moved)
        app.scan_roots([str(self.media)])
        saved = desktop.get_movie(movie['id'])
        self.assertTrue(saved['favorite'])
        self.assertEqual(saved['personal_rating'], 10)
        self.assertTrue(app.SCAN.snapshot()['errors'])

    def test_recycle_failure_preserves_database_and_hidden_flag(self):
        p = self.movie()
        movie = self.scan()
        before = p.stat().st_flags
        with self.assertRaises(OSError):
            app.recycle_movie_file(movie['id'], recycler=mock.Mock(side_effect=OSError('fixture failure')))
        self.assertEqual(p.stat().st_flags, before)
        self.assertTrue(desktop.get_movie(movie['id'])['exists_now'])

    def test_real_trash_then_restore_only_test_fixture(self):
        p = self.movie()
        sibling = self.media / '中文 空格影片.srt'
        sibling.write_text('fixture subtitle')
        movie = self.scan()
        trashed = []
        def recycle(path):
            file = QFile(str(path))
            self.assertTrue(file.moveToTrash(), file.errorString())
            trashed.append(Path(file.fileName()))
        try:
            app.recycle_movie_file(movie['id'], recycler=recycle)
            self.assertFalse(p.exists())
            self.assertTrue(sibling.exists())
            self.assertFalse(desktop.get_movie(movie['id'])['exists_now'])
            self.assertFalse(trashed[0].stat().st_flags & stat.UF_HIDDEN)
        finally:
            if trashed and trashed[0].exists():
                trashed[0].rename(p)

    def test_volume_identity(self):
        self.assertEqual(app.volume_for_path(Path('/Volumes/影片盘/电影/a.mp4')), '/Volumes/影片盘')
        self.assertEqual(app.volume_for_path(self.media), '/')

if __name__ == '__main__':
    unittest.main()
