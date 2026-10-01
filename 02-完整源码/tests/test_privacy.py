import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog
import app as core
import desktop
import privacy
import maintenance


class PrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingku-privacy-')
        self.root = Path(self.temp.name).resolve()
        self.base = self.root/'library'
        self.original = (core.DATA_DIR, core.DB_PATH, privacy.MODE, privacy.BASE)
        self.use('private')

    def tearDown(self):
        core.DATA_DIR, core.DB_PATH, privacy.MODE, privacy.BASE = self.original
        self.temp.cleanup()

    def use(self, mode):
        privacy.configure(self.base, mode == 'private')
        core.init_db()
        privacy.stamp_library()
        with core.connect() as c:
            c.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',
                         [('min_duration_minutes','0'),('hide_files_after_import','false'),('auto_match_after_scan','false')])

    def seed(self, title='私密样本', filename='private.mp4', actor='私密演员'):
        media = self.root/'media'
        media.mkdir(exist_ok=True)
        path = media/filename
        path.write_bytes(filename.encode()*64)
        with core.connect() as c:
            return c.execute('INSERT INTO movies(path,filename,title,fingerprint,file_size,cast_json,favorite,personal_rating,notes,play_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (str(path),filename,title,core.fast_fingerprint(path,path.stat().st_size),path.stat().st_size,
                 json.dumps([{'name':actor}]),1,9,'保留备注',4,core.now_iso(),core.now_iso())).lastrowid

    def test_ordinary_is_empty_and_has_no_private_facets_or_stats(self):
        mid = self.seed()
        core.discover_actor_profiles()
        self.use('public')
        self.assertEqual(desktop.query_movies(), [])
        self.assertEqual(desktop.query_movies(query='私密'), [])
        self.assertIsNone(desktop.get_movie(mid))
        self.assertEqual(desktop.query_actor_facets(), [])
        self.assertEqual(desktop.actor_display_name_map(), {})
        self.assertEqual(desktop.dashboard_stats()['total'], 0)
        self.assertEqual(core.list_scan_roots(), [])

    def test_data_settings_and_cache_locations_are_distinct(self):
        self.seed()
        private_cache = desktop.ImageManager.disk_cache_path('https://example.test/photo')
        with core.connect() as c: c.execute("INSERT INTO settings VALUES('test_private','secret')")
        self.use('public')
        self.assertNotEqual(private_cache,desktop.ImageManager.disk_cache_path('https://example.test/photo'))
        with core.connect() as c:
            self.assertIsNone(c.execute("SELECT value FROM settings WHERE key='test_private'").fetchone())

    def test_scan_skips_other_partition_and_renamed_copy(self):
        self.seed()
        original = self.root/'media/private.mp4'
        (original.parent/'renamed.mp4').write_bytes(original.read_bytes())
        self.use('public')
        with mock.patch.object(core,'probe_video_duration',return_value=660):
            core.scan_roots([str(original.parent)])
        self.assertEqual(desktop.query_movies(), [])
        self.assertEqual(core.SCAN.snapshot()['processed'],0)

    def test_broken_other_database_stops_scan_without_leaking_names(self):
        self.seed()
        self.use('public')
        (self.base/'film_library.db').write_bytes(b'broken')
        with self.assertRaisesRegex(RuntimeError,'停止导入'):
            core.scan_roots([str(self.root/'media')])
        self.assertFalse(core.SCAN.snapshot()['running'])
        self.assertEqual(desktop.query_movies(), [])

    def test_move_preserves_personal_metadata_and_selected_actor(self):
        mid = self.seed()
        core.discover_actor_profiles()
        new_id = privacy.move_movie(mid)
        self.assertIsNone(desktop.get_movie(mid))
        self.use('public')
        movie = desktop.get_movie(new_id)
        self.assertEqual((movie['personal_rating'],movie['favorite'],movie['notes'],movie['play_count']), (9,True,'保留备注',4))
        self.assertEqual(movie['cast'][0]['name'],'私密演员')
        self.assertEqual(desktop.query_actor_facets()[0]['name'],'私密演员')
        self.assertTrue(Path(movie['path']).exists())

    def test_move_copies_only_referenced_images_and_generated_frames(self):
        mid=self.seed()
        poster=core.DATA_DIR/'image-cache/one.jpg'; poster.parent.mkdir();poster.write_bytes(b'poster')
        unrelated=poster.with_name('unrelated.jpg');unrelated.write_bytes(b'private-unrelated')
        movie=desktop.get_movie(mid)
        frames=desktop.MainWindow.screenshot_dir(movie);frames.mkdir(parents=True)
        (frames/'frame_01.jpg').write_bytes(b'frame')
        desktop.update_movie(mid,local_poster=str(poster))
        new_id=privacy.move_movie(mid)
        self.use('public')
        moved=desktop.get_movie(new_id)
        self.assertTrue(Path(moved['local_poster']).is_relative_to(core.DATA_DIR))
        self.assertEqual(Path(moved['local_poster']).read_bytes(),b'poster')
        self.assertEqual(Path(moved['screenshots'][0]).read_bytes(),b'frame')
        self.assertFalse(any(p.read_bytes()==b'private-unrelated' for p in (core.DATA_DIR/'transferred-media').iterdir()))

    def test_move_removes_orphan_cache_but_keeps_remaining_movie_image(self):
        mid=self.seed()
        keep=self.seed('保留','keep.mp4','另一演员')
        folder=core.DATA_DIR/'image-cache';folder.mkdir()
        moved=folder/'moved.jpg';moved.write_bytes(b'moved')
        kept=folder/'kept.jpg';kept.write_bytes(b'kept')
        desktop.update_movie(mid,local_poster=str(moved))
        desktop.update_movie(keep,local_poster=str(kept))
        privacy.move_movie(mid)
        self.assertFalse(moved.exists())
        self.assertTrue(kept.exists())

    def test_batch_partition_dialog_shows_current_scope_only(self):
        from privacy_ui import PartitionDialog
        self.seed()
        self.use('public')
        self.seed('普通样本','ordinary.mp4','普通演员')
        window=desktop.MainWindow()
        dialog=PartitionDialog(window)
        self.assertEqual(dialog.list.count(),1)
        self.assertEqual(dialog.list.item(0).text(),'普通样本')
        dialog.select_visible()
        self.assertEqual(dialog.list.item(0).checkState(),Qt.CheckState.Checked)
        dialog.clear_checked()
        self.assertEqual(dialog.list.item(0).checkState(),Qt.CheckState.Unchecked)
        dialog.reject();window.close();window.deleteLater()

    def test_move_conflict_retains_both_records(self):
        mid=self.seed()
        self.use('public');self.seed()
        self.use('private')
        with self.assertRaisesRegex(ValueError,'已存在'):
            privacy.move_movie(mid)
        self.assertIsNotNone(desktop.get_movie(mid))
        self.use('public');self.assertEqual(len(desktop.query_movies()),1)

    def test_move_transaction_failure_rolls_back_both_libraries(self):
        mid=self.seed()
        with mock.patch.object(core,'_prune_unused_actor_profiles',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):privacy.move_movie(mid)
        self.assertIsNotNone(desktop.get_movie(mid))
        self.use('public');self.assertEqual(desktop.query_movies(),[])

    def test_move_back_removes_old_exclusion_and_stays_in_one_library(self):
        mid=self.seed();new_id=privacy.move_movie(mid)
        self.use('public');returned=privacy.move_movie(new_id)
        self.assertEqual(desktop.query_movies(),[])
        self.use('private')
        movie=desktop.get_movie(returned)
        self.assertFalse(privacy.blocked_path(movie['path'],movie['fingerprint']))
        self.assertEqual(len(desktop.query_movies()),1)

    def test_moved_out_record_not_reimported_even_if_destination_record_removed(self):
        mid=self.seed();new_id=privacy.move_movie(mid)
        self.use('public')
        with core.connect() as c:c.execute('DELETE FROM movies WHERE id=?',(new_id,))
        self.use('private')
        core.scan_roots([str(self.root/'media')])
        self.assertEqual(desktop.query_movies(),[])

    def test_private_backup_cannot_restore_into_ordinary(self):
        self.seed();backup=maintenance.create_backup(self.root/'backups')
        self.use('public')
        with self.assertRaisesRegex(ValueError,'模式不同'):maintenance.inspect_backup(backup)
        self.assertEqual(desktop.query_movies(),[])

    def test_old_backup_defaults_to_private(self):
        self.seed()
        with core.connect() as c:c.execute("DELETE FROM settings WHERE key='privacy_mode'")
        backup=maintenance.create_backup(self.root/'backups')
        self.assertEqual(maintenance.inspect_backup(backup)['movies'],1)
        self.use('public')
        with self.assertRaisesRegex(ValueError,'旧版本'):maintenance.inspect_backup(backup)

    def test_own_backup_restores_and_keeps_moved_cache(self):
        mid=self.seed();privacy.move_movie(mid)
        self.use('public')
        backup=maintenance.create_backup(self.root/'backups')
        self.assertEqual(maintenance.inspect_backup(backup)['movies'],1)
        result=maintenance.restore_backup(backup)
        self.assertEqual(result['movies'],1)
        self.assertEqual(desktop.dashboard_stats()['total'],1)

    def test_backup_before_move_cannot_reintroduce_moved_record(self):
        mid=self.seed();backup=maintenance.create_backup(self.root/'backups')
        privacy.move_movie(mid)
        with self.assertRaisesRegex(ValueError,'另一模式'):maintenance.inspect_backup(backup)

    def test_library_permissions_are_owner_only(self):
        import os
        if os.name=='nt':self.skipTest('NTFS permissions use Windows ACLs, not POSIX modes')
        self.assertEqual(core.DATA_DIR.stat().st_mode & 0o777,0o700)
        self.assertEqual(core.DB_PATH.stat().st_mode & 0o777,0o600)

    def test_lock_immediately_hides_movie_and_modal_and_discards_callbacks(self):
        self.seed()
        window=desktop.MainWindow()
        window.show()
        dialog=QDialog(window);dialog.show()
        with mock.patch.object(window.pool,'activeThreadCount',return_value=1):
            window.begin_flip(False)
            window.finish_flip()
        self.assertTrue(window.privacy_locking)
        self.assertFalse(window.protected_content.isVisible())
        self.assertFalse(dialog.isVisible())
        self.assertIn('已锁定',window.centralWidget().text())
        self.assertEqual(window.privacy_relaunch_args,['--public'])
        window.privacy_restart_timer.stop()
        self.qt.removeEventFilter(window)
        window.close()
        window.deleteLater()

    def test_private_session_survives_background_and_has_no_idle_lock(self):
        window=desktop.MainWindow()
        with mock.patch.object(window,'begin_flip') as flip:
            self.qt.applicationStateChanged.emit(Qt.ApplicationState.ApplicationInactive)
            self.qt.applicationStateChanged.emit(Qt.ApplicationState.ApplicationActive)
            self.qt.processEvents()
            flip.assert_not_called()
        self.assertFalse(hasattr(window,'privacy_idle_timer'))
        self.assertFalse(hasattr(window,'lock_watcher'))
        self.assertEqual(privacy.MODE,'private')
        self.qt.removeEventFilter(window)
        window.close();window.deleteLater()

    def test_playback_and_browser_do_not_leave_private_library(self):
        mid=self.seed('测试影片','play.mp4','测试演员')
        window=desktop.MainWindow();window.current_movie_id=mid
        def external_app(url):
            self.qt.applicationStateChanged.emit(Qt.ApplicationState.ApplicationInactive)
            return True
        with mock.patch.object(window,'begin_flip') as flip, mock.patch.object(window,'show_movie'), mock.patch.object(desktop.QDesktopServices,'openUrl',side_effect=external_app), mock.patch.object(desktop.player,'play_movie',side_effect=external_app):
            window.play_current()
            window.search_actor('测试演员')
            self.qt.processEvents()
            flip.assert_not_called()
            self.assertEqual(privacy.MODE,'private')
            self.assertFalse(window.privacy_locking)
        self.qt.removeEventFilter(window)
        window.close();window.deleteLater()

    def test_failed_playback_does_not_increment_count_or_leave_private_mode(self):
        mid=self.seed('测试影片','failure.mp4','测试演员')
        window=desktop.MainWindow();window.current_movie_id=mid
        try:
            before=desktop.get_movie(mid)['play_count']
            with mock.patch.object(desktop.player,'play_movie',side_effect=RuntimeError('IINA 未能打开')), mock.patch.object(window,'show_error') as error:
                window.play_current()
                error.assert_called_once()
            self.assertEqual(desktop.get_movie(mid)['play_count'],before)
            self.assertEqual(privacy.MODE,'private')
        finally:
            self.qt.removeEventFilter(window)
            window.close();window.deleteLater()

    def test_authentication_uses_native_result_and_cancel_is_quiet(self):
        for status,expected in [(1,True),(-1,False),(-2,False)]:
            with self.subTest(status=status), mock.patch.object(privacy,'_load_auth_bridge') as loader, mock.patch('PySide6.QtWidgets.QMessageBox.information') as message:
                bridge=loader.return_value
                bridge.yingku_auth_begin.return_value=0
                bridge.yingku_auth_status.return_value=status
                self.assertEqual(privacy.authenticate(),expected)
                bridge.yingku_auth_cancel.assert_called_once()
                message.assert_not_called()
                self.assertFalse(privacy.AUTHENTICATING)

    def test_missing_auth_component_keeps_private_locked(self):
        with mock.patch.object(privacy,'_load_auth_bridge',side_effect=OSError), mock.patch('PySide6.QtWidgets.QMessageBox.warning'):
            self.assertFalse(privacy.authenticate())

    def test_unavailable_auth_keeps_private_locked(self):
        with mock.patch.object(privacy,'_load_auth_bridge') as loader:
            loader.return_value.yingku_auth_begin.return_value=-2
            self.assertFalse(privacy.authenticate())
            self.assertFalse(privacy.AUTHENTICATING)

    def test_search_preference_migrates_and_is_shared_between_modes(self):
        import actor_search
        self.use('public')
        with core.connect() as c:
            c.execute('INSERT INTO settings VALUES(?,?)',(actor_search.SETTING_KEY,'https://example.com/?q=<name>'))
        self.use('private')
        self.assertEqual(actor_search.load_template(),'https://example.com/?q=<name>')
        actor_search.save_template('https://example.org/find/<name>')
        self.use('public')
        self.assertEqual(actor_search.load_template(),'https://example.org/find/<name>')
        self.use('private')
        self.assertEqual(actor_search.load_template(),'https://example.org/find/<name>')

    def test_public_window_only_lists_ordinary_records(self):
        self.seed()
        self.use('public')
        self.seed('普通样本','ordinary.mp4','普通演员')
        window=desktop.MainWindow()
        self.assertEqual([c.movie['title'] for c in window.cards],['普通样本'])
        self.assertEqual(window.brand.text(),'◉  影 库')
        self.assertFalse(hasattr(window,'flip_button'))
        self.assertIn('共 1 部',window.bottom_stats.text())
        window.close();window.deleteLater()

if __name__=='__main__':unittest.main()
