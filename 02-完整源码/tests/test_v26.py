import json
from contextlib import closing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from PySide6.QtWidgets import QApplication
import app as core
import desktop
import maintenance as ops


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='yingku-v26-')
        self.root = Path(self.temp.name).resolve()
        self.data = self.root/'data'
        self.old = self.root/'old'
        self.new = self.root/'new'
        self.old.mkdir(); self.new.mkdir()
        self.patches = [mock.patch.object(core,'DATA_DIR',self.data),
                        mock.patch.object(core,'DB_PATH',self.data/'film_library.db')]
        for p in self.patches: p.start()
        core.init_db()
        with core.connect() as c:
            c.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',
                          [('hide_files_after_import','false'),('min_duration_minutes','0')])

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def seed(self, name='影片.mp4', payload=b'distinct-content'):
        p=self.old/name
        p.write_bytes(payload)
        with mock.patch.object(core,'probe_video_duration',return_value=660):
            core.scan_roots([str(self.old)])
        with core.connect() as c:
            movie=c.execute('SELECT * FROM movies WHERE path=?',(str(p.resolve()),)).fetchone()
        desktop.update_movie(movie['id'],personal_rating=10,favorite=True,notes='保持备注',watch_status='watched')
        return movie['id'],p

    def test_relink_after_automatic_rescan_retains_id_and_personal_data(self):
        mid,p=self.seed()
        (self.old/'影片.nfo').write_text('sidecar stays')
        target=self.old/'新名字.mp4'
        p.rename(target)
        with mock.patch.object(core,'probe_video_duration',return_value=660):
            core.scan_roots([str(self.old)])
        self.assertEqual(desktop.get_movie(mid)['file_status'],'missing')
        plan=ops.plan_relink(self.old,self.old)
        item=next(x for x in plan['items'] if x['id']==mid)
        self.assertIn('merge_id',item)
        self.assertEqual(ops.apply_relink(plan,[mid]),1)
        saved=desktop.get_movie(mid)
        self.assertEqual(saved['path'],str(target))
        self.assertEqual(saved['personal_rating'],10)
        self.assertTrue(saved['favorite'])
        self.assertEqual(saved['notes'],'保持备注')
        self.assertEqual(saved['watch_status'],'watched')
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM movies').fetchone()[0],1)
        self.assertTrue((self.old/'影片.nfo').exists())

    def test_offline_source_relink_keeps_actor_metadata_and_play_count(self):
        mid,p=self.seed()
        cast=[{'name':'演员甲','aliases':['Alias'],'profile_url':'https://example.test/profile'}]
        desktop.update_movie(mid,cast_json=cast)
        desktop.record_movie_play(mid)
        p.rename(self.new/p.name)
        self.old.rmdir()
        plan=ops.plan_relink(self.old,self.new)
        ops.apply_relink(plan,[mid])
        saved=desktop.get_movie(mid)
        self.assertEqual(saved['cast'],cast)
        self.assertEqual(saved['play_count'],1)
        self.assertEqual([r['path'] for r in core.list_scan_roots()],[str(self.new)])

    def test_missing_record_keeps_actor_profile_for_relink(self):
        mid,p=self.seed()
        desktop.update_movie(mid,cast_json=[{'name':'演员甲'}])
        core.discover_actor_profiles()
        p.rename(self.new/p.name)
        core.scan_roots([str(self.old)])
        with core.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM actor_profiles').fetchone()[0],1)

    def test_relink_moves_local_actor_photo_reference(self):
        mid,p=self.seed()
        photo=self.old/'actor.jpg'; photo.write_bytes(b'photo')
        desktop.update_movie(mid,cast_json=[{'name':'演员甲','avatar':str(photo)}])
        core.discover_actor_profiles()
        key=core.normalize_actor_name('演员甲')
        core.store_actor_photos(key,[{'avatar':str(photo)}],str(photo))
        p.rename(self.new/p.name); photo.rename(self.new/photo.name)
        plan=ops.plan_relink(self.old,self.new)
        ops.apply_relink(plan,[mid])
        with core.connect() as c:
            avatar=c.execute('SELECT avatar_url FROM actor_profiles WHERE name_key=?',(key,)).fetchone()[0]
        self.assertEqual(avatar,str(self.new/'actor.jpg'))
        self.assertEqual(core.actor_photo_choices(key)[0]['photo_url'],avatar)

    def test_ambiguous_duplicate_is_not_assigned(self):
        mid,p=self.seed()
        payload=p.read_bytes(); p.unlink()
        (self.new/'a.mp4').write_bytes(payload); (self.new/'b.mp4').write_bytes(payload)
        plan=ops.plan_relink(self.old,self.new)
        self.assertFalse(plan['items'][0]['new_path'])
        self.assertIn('多个',plan['items'][0]['status'])

    def test_new_record_with_personal_edits_is_never_overwritten(self):
        mid,p=self.seed(); p.rename(self.new/p.name)
        with mock.patch.object(core,'probe_video_duration',return_value=660):
            core.scan_roots([str(self.new)])
        with core.connect() as c:
            newer=c.execute('SELECT id FROM movies WHERE id<>?',(mid,)).fetchone()[0]
        desktop.update_movie(newer,personal_rating=6)
        plan=ops.plan_relink(self.old,self.new)
        self.assertFalse(plan['items'][0]['new_path'])
        self.assertIn('个人整理',plan['items'][0]['status'])
        self.assertEqual(desktop.get_movie(newer)['personal_rating'],6)

    def test_plan_revalidation_is_atomic(self):
        a,p=self.seed('a.mp4',b'first'); b,q=self.seed('b.mp4',b'second')
        p.rename(self.new/p.name); q.rename(self.new/q.name)
        plan=ops.plan_relink(self.old,self.new)
        (self.new/'b.mp4').write_bytes(b'changed')
        with self.assertRaises(ValueError): ops.apply_relink(plan,[a,b])
        self.assertEqual(desktop.get_movie(a)['path'],str(p))
        self.assertEqual(desktop.get_movie(b)['path'],str(q))

    def test_new_personal_edit_after_preview_blocks_merge(self):
        mid,p=self.seed(); p.rename(self.new/p.name)
        with mock.patch.object(core,'probe_video_duration',return_value=660): core.scan_roots([str(self.new)])
        plan=ops.plan_relink(self.old,self.new)
        new_id=plan['items'][0]['merge_id']
        desktop.update_movie(new_id,favorite=True)
        with self.assertRaises(ValueError): ops.apply_relink(plan,[mid])
        self.assertEqual(desktop.get_movie(mid)['path'],str(p))
        self.assertTrue(desktop.get_movie(new_id)['favorite'])

    def test_backup_restore_includes_cache_and_safety_snapshot(self):
        mid,p=self.seed()
        (self.data/'image-cache').mkdir()
        (self.data/'image-cache'/'avatar.img').write_bytes(b'avatar')
        backup=ops.create_backup(self.root/'backups')
        desktop.update_movie(mid,personal_rating=2)
        report=ops.restore_backup(backup)
        self.assertEqual(desktop.get_movie(mid)['personal_rating'],10)
        self.assertEqual((self.data/'image-cache'/'avatar.img').read_bytes(),b'avatar')
        self.assertTrue(Path(report['safety_backup']).is_dir())
        import sqlite3
        with closing(sqlite3.connect(Path(report['safety_backup'])/'film_library.db')) as c:
            self.assertEqual(c.execute('SELECT personal_rating FROM movies WHERE id=?',(mid,)).fetchone()[0],2)
        self.assertTrue(p.exists())

    def test_restore_remaps_local_cache_paths(self):
        mid,_=self.seed()
        (self.data/'screenshots').mkdir()
        frame=self.data/'screenshots'/'one.jpg'; frame.write_bytes(b'frame')
        with core.connect() as c:
            c.execute('UPDATE movies SET screenshots_json=? WHERE id=?',(json.dumps([str(frame)]),mid))
        backup=ops.create_backup(self.root/'backups')
        destination=self.root/'other-data'
        with mock.patch.object(core,'DATA_DIR',destination),mock.patch.object(core,'DB_PATH',destination/'film_library.db'):
            ops.restore_backup(backup)
            core.init_db()
            self.assertEqual(desktop.get_movie(mid)['screenshots'],[str(destination/'screenshots'/'one.jpg')])
            self.assertTrue((destination/'screenshots'/'one.jpg').is_file())

    def test_tampered_backup_does_not_change_current_data(self):
        mid,_=self.seed()
        backup=Path(ops.create_backup(self.root/'backups'))
        with (backup/'film_library.db').open('ab') as f: f.write(b'tamper')
        with self.assertRaises(ValueError): ops.restore_backup(backup)
        self.assertEqual(desktop.get_movie(mid)['personal_rating'],10)

    def test_backup_rejects_internal_destination_and_symlink_cache(self):
        self.seed()
        with self.assertRaises(ValueError): ops.create_backup(self.data/'backups')
        (self.data/'image-cache').symlink_to(self.old)
        with self.assertRaises(ValueError): ops.create_backup(self.root/'backups')

    def test_manifest_traversal_rejected(self):
        self.seed(); backup=Path(ops.create_backup(self.root/'backups'))
        manifest=backup/'影库备份.json'; content=json.loads(manifest.read_text())
        content['files']['../outside']='0'*64; manifest.write_text(json.dumps(content))
        with self.assertRaises(ValueError): ops.inspect_backup(backup)

    def test_failed_restore_directory_swap_rolls_back(self):
        mid,_=self.seed(); backup=ops.create_backup(self.root/'backups')
        desktop.update_movie(mid,personal_rating=6)
        real_rename=Path.rename
        def fail_stage(path,target):
            if path.name.startswith('.yingku-restore-'): raise OSError('injected disk failure')
            return real_rename(path,target)
        with mock.patch.object(Path,'rename',fail_stage):
            with self.assertRaises(OSError): ops.restore_backup(backup)
        self.assertEqual(desktop.get_movie(mid)['personal_rating'],6)

    def seed_large(self):
        timestamp=core.now_iso()
        with core.connect() as c:
            c.executemany('INSERT INTO movies(path,filename,title,favorite,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                [(str(self.old/f'{i}.mp4'),f'{i}.mp4',f'测试{i:04}',i%2,timestamp,timestamp) for i in range(321)])

    def test_paging_reaches_all_321_movies_without_duplicates(self):
        self.seed_large(); ids=[]
        for offset in range(0,321,60):
            rows,total=desktop.query_movie_page(sort='title',limit=60,offset=offset)
            self.assertEqual(total,321); ids.extend(row['id'] for row in rows)
        self.assertEqual(len(ids),321); self.assertEqual(len(set(ids)),321)
        rows,total=desktop.query_movie_page(query='测试',favorite_filter='liked',limit=60,offset=120)
        self.assertEqual(total,160); self.assertEqual(len(rows),40)
        self.assertTrue(all(row['favorite'] for row in rows))

    def test_ui_paging_resets_on_filter(self):
        self.seed_large(); qt=QApplication.instance() or QApplication([])
        with mock.patch.dict(os.environ,{'YINGKU_SETTINGS_PATH':str(self.root/'ui.ini'),'YINGKU_DISABLE_STARTUP_TASKS':'1'}):
            window=desktop.MainWindow()
            self.assertEqual(len(window.cards),60)
            for _ in range(5): window.next_page.click()
            self.assertEqual(window.movie_page,5)
            self.assertEqual(len(window.cards),21)
            self.assertFalse(window.next_page.isEnabled())
            window.favorite_filter_combo.setCurrentIndex(1)
            self.assertEqual(window.movie_page,0)
            self.assertIn('160',window.result_label.text())
            window.close()

    def test_backup_and_restore_dialog_actions(self):
        from maintenance_ui import MaintenanceDialog
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        self.seed()
        qt=QApplication.instance() or QApplication([])
        dialog=MaintenanceDialog()
        selected=[]
        dialog.restore_requested.connect(selected.append)
        destination=self.root/'ui-backups';destination.mkdir()
        with mock.patch.object(QFileDialog,'getExistingDirectory',return_value=str(destination)):
            dialog.backup()
            for _ in range(200):
                qt.processEvents()
                if not dialog.busy: break
                QTest.qWait(10)
        self.assertFalse(dialog.busy)
        backups=list(destination.glob('影库备份_*'))
        self.assertEqual(len(backups),1)
        self.assertIn('已保存',dialog.status.text())
        with mock.patch.object(QFileDialog,'getExistingDirectory',return_value=str(backups[0])), mock.patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
            dialog.restore()
            for _ in range(200):
                qt.processEvents()
                if not dialog.busy: break
                QTest.qWait(10)
        self.assertEqual(selected,[str(backups[0])])
        dialog.close()

    def test_actor_editor_preserves_all_12_and_extra_fields(self):
        mid,_=self.seed(); qt=QApplication.instance() or QApplication([])
        cast=[{'name':f'演员{i}','role':'主演','avatar':'','aliases':[f'alias{i}'],
               'profile_url':f'https://example.test/{i}','source':'fixture'} for i in range(12)]
        desktop.update_movie(mid,cast_json=cast)
        dialog=desktop.ManualEditDialog(desktop.get_movie(mid))
        self.assertEqual(len(dialog.cast_rows),12)
        dialog.title_edit.setText('只改片名')
        dialog.save()
        self.assertEqual(desktop.get_movie(mid)['cast'],cast)
        dialog.cast_rows[0][0].setText('演员改名')
        dialog.add_cast_row({'name':'新演员'})
        dialog.save()
        saved=desktop.get_movie(mid)['cast']
        self.assertEqual(len(saved),13)
        self.assertEqual(saved[0]['aliases'],['alias0'])
        self.assertEqual(saved[-1]['name'],'新演员')
        dialog.close()

if __name__=='__main__': unittest.main()
