import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import desktop
import iina_cleanup


class CleanupTests(unittest.TestCase):
    def test_removes_history_resume_thumbnails_and_state_but_keeps_settings_and_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            library=Path(tmp)/'Library'
            for relative in iina_cleanup.TRACE_PATHS:
                target=library/relative
                if target.suffix=='.plist':target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'history')
                else:target.mkdir(parents=True,exist_ok=True);(target/'test-entry').write_bytes(b'trace')
            keep=[library/'Preferences/com.colliderli.iina.plist',library/'Application Support/com.colliderli.iina/plugins/plugin.js',library/'Application Support/other/history.plist',Path(tmp)/'movie.mp4']
            for path in keep:path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'keep')
            self.assertEqual(iina_cleanup._clear_files(library),4)
            self.assertTrue(all(not (library/p).exists() for p in iina_cleanup.TRACE_PATHS))
            self.assertTrue(all(p.read_bytes()==b'keep' for p in keep))
            self.assertEqual(iina_cleanup._clear_files(library),0)

    def test_does_not_follow_symlink_into_movie_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);library=root/'Library';outside=root/'Movies';outside.mkdir();(outside/'precious.mp4').touch()
            parent=library/'Application Support';parent.mkdir(parents=True)
            (parent/'com.colliderli.iina').symlink_to(outside,target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError,'重定向'):iina_cleanup._clear_files(library)
            self.assertTrue((outside/'precious.mp4').exists())

    def test_terminates_player_before_removing_records_and_only_resets_iina_list(self):
        order=[]
        def run(args,**kwargs):order.append(args);return mock.Mock(returncode=0)
        with mock.patch.object(iina_cleanup.sys,'platform','darwin'),mock.patch.object(iina_cleanup.subprocess,'run',side_effect=run),mock.patch.object(iina_cleanup,'_clear_files',side_effect=lambda path:order.append('delete') or 4):
            self.assertEqual(iina_cleanup.clear_playback_history(),4)
        self.assertEqual(order[0][1],'clear-recents');self.assertEqual(order[1][1],'stop');self.assertEqual(order[2][1],'clear-preferences');self.assertEqual(order[3],'delete')

    def test_refused_quit_does_not_delete_history_while_player_is_writing(self):
        with mock.patch.object(iina_cleanup.sys,'platform','darwin'),mock.patch.object(iina_cleanup.subprocess,'run',side_effect=[mock.Mock(),subprocess.CalledProcessError(1,['stop'])]),mock.patch.object(iina_cleanup,'_clear_files') as remove:
            with self.assertRaisesRegex(RuntimeError,'未能全部清理'):iina_cleanup.clear_playback_history()
            remove.assert_not_called()

    def test_system_recent_list_failure_is_reported(self):
        with mock.patch.object(iina_cleanup.sys,'platform','darwin'),mock.patch.object(iina_cleanup.subprocess,'run',side_effect=[subprocess.TimeoutExpired('clear-recents',12),mock.Mock(),mock.Mock()]),mock.patch.object(iina_cleanup,'_clear_files'):
            with self.assertRaises(RuntimeError):iina_cleanup.clear_playback_history()

    def test_exit_cleans_both_modes_but_internal_relaunch_does_not(self):
        for mode in ['public','private']:
            with self.subTest(mode=mode),mock.patch.object(desktop.privacy,'MODE',mode),mock.patch.object(desktop.iina_cleanup,'clear_playback_history') as cleanup:
                desktop.cleanup_iina_on_exit(mock.Mock(relaunching=False));cleanup.assert_called_once()
        with mock.patch.object(desktop.iina_cleanup,'clear_playback_history') as cleanup:
            desktop.cleanup_iina_on_exit(mock.Mock(relaunching=True));cleanup.assert_not_called()

    def test_exit_silently_ignores_cleanup_failure(self):
        with mock.patch.object(desktop.iina_cleanup,'clear_playback_history',side_effect=RuntimeError('cleanup failed')),mock.patch.object(desktop.QMessageBox,'warning') as message:
            desktop.cleanup_iina_on_exit(mock.Mock(relaunching=False));message.assert_not_called()

    def test_permission_check_never_prompts_or_runs_during_startup(self):
        source=(Path(__file__).parents[1]/'native/IINASession.swift').read_text()
        self.assertNotIn('AXIsProcessTrustedWithOptions',source)
        self.assertNotIn('check_iina_cleanup_access',Path(desktop.__file__).read_text())
