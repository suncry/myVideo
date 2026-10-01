import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import player


class PlayerTests(unittest.TestCase):
    def test_mac_playback_explicitly_targets_iina_and_preserves_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            name='中文 空格 $(touch nope).mp4' if os.name=='nt' else '中文 空格 "引号" $(touch nope).mp4'
            movie=Path(tmp)/name;movie.write_bytes(b'test')
            with mock.patch.object(player.sys,'platform','darwin'), mock.patch.object(player.subprocess,'run') as run:
                player.play_movie(str(movie))
                self.assertEqual(run.call_args.args[0],['/usr/bin/open','-b','com.colliderli.iina',str(movie.resolve())])
                self.assertNotIn('shell',run.call_args.kwargs)

    @unittest.skipUnless(os.name=='nt', 'Windows system playback')
    def test_windows_playback_uses_default_player_with_unicode_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            movie=Path(tmp)/'中文 空格 & $(literal).mp4';movie.write_bytes(b'test')
            with mock.patch.object(player.os,'startfile') as launch, mock.patch.object(player.subprocess,'run') as run:
                player.play_movie(str(movie))
                launch.assert_called_once_with(str(movie.resolve()))
                run.assert_not_called()

    def test_missing_file_does_not_launch_anything(self):
        with mock.patch.object(player.subprocess,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'暂时无法访问'):player.play_movie('/no-such-yingku-movie.mp4')
            run.assert_not_called()

    def test_iina_failure_does_not_fall_back_to_changed_system_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            movie=Path(tmp)/'test.mp4';movie.touch()
            with mock.patch.object(player.sys,'platform','darwin'), mock.patch.object(player.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['open'])) as run:
                with self.assertRaisesRegex(RuntimeError,'IINA'):player.play_movie(str(movie))
                self.assertEqual(run.call_count,1)

    def report(self):
        return {'installed':True,'associations':[dict(extension=e,before='old.player',after=player.IINA_BUNDLE_ID,status=0) for e in player.VIDEO_EXTENSIONS]}

    def test_repair_verifies_every_video_type_and_limits_scope(self):
        with mock.patch.object(player.sys,'platform','darwin'), mock.patch.object(player.subprocess,'run',return_value=mock.Mock(stdout=json.dumps(self.report()))) as run:
            self.assertEqual(player.restore_video_defaults(),len(player.VIDEO_EXTENSIONS))
            args=run.call_args.args[0]
            self.assertEqual(args[1],'repair')
            self.assertEqual(set(args[2:]),set(player.VIDEO_EXTENSIONS))
            self.assertTrue(set(player.VIDEO_EXTENSIONS).isdisjoint({'mp3','jpg','pdf','folder','http','https'}))

    def test_false_success_or_partial_report_is_not_reported_as_repaired(self):
        for mode in ['unchanged','partial','missing']:
            report=self.report()
            if mode=='unchanged':report['associations'][0]['after']='old.player'
            if mode=='partial':report['associations'].pop()
            if mode=='missing':report['installed']=False
            with self.subTest(mode=mode),mock.patch.object(player.sys,'platform','darwin'),mock.patch.object(player.subprocess,'run',return_value=mock.Mock(stdout=json.dumps(report))):
                with self.assertRaises(RuntimeError):player.restore_video_defaults()

    def test_timeout_is_reported_without_blocking_forever(self):
        with mock.patch.object(player.sys,'platform','darwin'),mock.patch.object(player.subprocess,'run',side_effect=subprocess.TimeoutExpired('helper',60)):
            with self.assertRaisesRegex(RuntimeError,'检查未完成'):player.restore_video_defaults()
