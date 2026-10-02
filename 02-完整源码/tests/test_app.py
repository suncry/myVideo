import tempfile
import unittest
import os
import json
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

import app
import desktop


class FilmLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_patch = mock.patch.object(app, "DB_PATH", self.root / "test.db")
        self.db_patch.start()
        app.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_clean_release_filename(self):
        title, year = app.clean_filename(Path("霸王别姬.1993.1080p.BluRay.x265.DTS.mkv"))
        self.assertEqual(title, "霸王别姬")
        self.assertEqual(year, 1993)

    def test_reads_nfo_cast_and_local_poster(self):
        video = self.root / "Movie.mkv"
        video.write_bytes(b"video")
        (self.root / "Movie.jpg").write_bytes(b"image")
        (self.root / "Movie.nfo").write_text(
            "<movie><title>测试影片</title><year>2020</year><plot>简介</plot>"
            "<genre>剧情</genre><actor><name>主演甲</name><role>角色乙</role>"
            "<thumb>https://example.com/a.jpg</thumb></actor></movie>", encoding="utf-8"
        )
        nfo = app.read_nfo(video)
        self.assertEqual(nfo["title"], "测试影片")
        self.assertEqual(nfo["cast"][0]["name"], "主演甲")
        self.assertTrue(app.find_local_poster(video).endswith("Movie.jpg"))

    def test_scan_indexes_video_and_ignores_other_files(self):
        video = self.root / "小众电影.2018.720p.mkv"
        video.write_bytes(b"a" * 1024)
        (self.root / "notes.txt").write_text("skip", encoding="utf-8")
        app.scan_roots([str(self.root)])
        with app.connect() as conn:
            rows = conn.execute("SELECT title,year,file_size FROM movies").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "小众电影")
        self.assertEqual(rows[0]["year"], 2018)
        self.assertEqual(rows[0]["file_size"], 1024)

    def test_duplicate_fingerprint_is_stable(self):
        first = self.root / "a.mp4"
        second = self.root / "b.mp4"
        payload = b"same-content" * 100
        first.write_bytes(payload)
        second.write_bytes(payload)
        self.assertEqual(app.fast_fingerprint(first, len(payload)), app.fast_fingerprint(second, len(payload)))

    def test_schema_includes_key_screenshots(self):
        with app.connect() as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(movies)")}
            actor_columns = {row["name"] for row in conn.execute("PRAGMA table_info(actor_profiles)")}
        self.assertIn("screenshots_json", columns)
        self.assertIn("screenshots_status", columns)
        self.assertIn("screenshots_attempted_at", columns)
        self.assertIn("duration_seconds", columns)
        self.assertIn("play_count", columns)
        self.assertIn("last_played_at", columns)
        self.assertIn("match_confidence", columns)
        self.assertIn("last_match_attempt", columns)
        self.assertIn("hidden_by_app", columns)
        self.assertIn("original_file_attributes", columns)
        self.assertIn("avatar_url", actor_columns)
        self.assertIn("display_name", actor_columns)
        self.assertIn("aliases_json", actor_columns)
        self.assertIn("biography", actor_columns)
        self.assertIn("info_json", actor_columns)
        self.assertIn("source_refs_json", actor_columns)
        with app.connect() as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("actor_photos", tables)
        self.assertIn("hidden_folders", tables)

    def test_extracts_release_codes_from_noisy_names(self):
        self.assertEqual(app.extract_media_code("489155 com@MIDA-517-C.mp4"), "MIDA-517")
        self.assertEqual(app.extract_media_code("hhd800.com@COSX-021.mkv"), "COSX-021")
        self.assertEqual(app.extract_media_code("FC2-PPV-1234567.mp4"), "FC2-PPV-1234567")

    def test_exact_code_candidate_has_safe_auto_confidence(self):
        movie = {"title": "KAM-237", "filename": "KAM-237.mp4", "year": None}
        candidate = {"id": "KAM-237", "title": "KAM-237", "original_title": "KAM-237", "year": 2025}
        self.assertEqual(app.auto_candidate_confidence(movie, candidate), 0.995)

    def test_auto_match_uses_unique_exact_code(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO movies(path,filename,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(self.root / "KAM-237.mp4"), "KAM-237.mp4", "KAM-237", timestamp, timestamp),
            )
            movie_id = cursor.lastrowid
            row = conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
        movie = app.movie_dict(row)
        exact = {"provider": "catalog", "id": "KAM-237", "title": "KAM-237", "original_title": "KAM-237", "link": "https://example.test/kam-237"}
        with mock.patch.object(app, "search_code_catalog", return_value=[exact]), mock.patch.object(app, "apply_metadata") as apply:
            result = app.auto_match_one(movie)
        self.assertEqual(result, "matched")
        apply.assert_called_once()
        with app.connect() as conn:
            status = conn.execute("SELECT match_status,match_confidence FROM movies WHERE id=?", (movie_id,)).fetchone()
        self.assertEqual(status["match_status"], "matched")
        self.assertGreater(status["match_confidence"], 0.99)

    def test_saved_no_match_and_review_are_not_requeried_immediately(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.executemany(
                """INSERT INTO movies(path,filename,title,match_status,last_match_attempt,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                [
                    (str(self.root / "new.mp4"), "new.mp4", "New", "unmatched", "", timestamp, timestamp),
                    (str(self.root / "none.mp4"), "none.mp4", "None", "no_match", timestamp, timestamp, timestamp),
                    (str(self.root / "review.mp4"), "review.mp4", "Review", "review", timestamp, timestamp, timestamp),
                ],
            )
        self.assertEqual(app.auto_match_pending_count(), 1)

    def test_scan_source_can_be_managed_without_deleting_movies(self):
        video = self.root / "保留影片.mp4"
        video.write_bytes(b"video")
        app.scan_roots([str(self.root)])
        sources = app.list_scan_roots()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["movie_count"], 1)
        removed = app.remove_scan_root(str(self.root))
        self.assertEqual(removed, 1)
        self.assertEqual(app.list_scan_roots(), [])
        with app.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0], 0)
        self.assertTrue(video.exists())

    def test_windows_hidden_attributes_preserve_original_mask(self):
        video = self.root / "隐私影片.mp4"
        video.write_bytes(b"video")
        with mock.patch.object(app, "get_file_attributes", return_value=32), mock.patch.object(
            app, "set_file_attributes", return_value=True
        ) as setter:
            changed, original = app.hide_media_file(video, protected=True)
        self.assertTrue(changed)
        self.assertEqual(original, 32)
        setter.assert_called_once_with(video, 32 | app.FILE_ATTRIBUTE_HIDDEN | app.FILE_ATTRIBUTE_SYSTEM)

    @unittest.skipUnless(os.name == "nt", "Windows file attributes")
    def test_actual_windows_file_can_be_hidden_and_restored_in_place(self):
        video = self.root / "原地隐藏.mp4"
        video.write_bytes(b"video")
        original = app.get_file_attributes(video)
        changed, saved = app.hide_media_file(video)
        self.assertTrue(changed)
        self.assertEqual(saved, original)
        self.assertTrue(app.get_file_attributes(video) & app.FILE_ATTRIBUTE_HIDDEN)
        self.assertTrue(app.restore_media_file(video, original))
        self.assertEqual(app.get_file_attributes(video), original)

    @unittest.skipUnless(os.name == "nt", "Windows folder attributes")
    def test_actual_windows_folder_can_be_hidden_and_restored_in_place(self):
        folder = self.root / "原地隐藏目录"
        folder.mkdir()
        original = app.get_file_attributes(folder)
        changed, saved = app.hide_media_file(folder)
        self.assertTrue(changed)
        self.assertEqual(saved, original)
        self.assertTrue(app.get_file_attributes(folder) & app.FILE_ATTRIBUTE_HIDDEN)
        self.assertTrue(app.restore_media_file(folder, original))
        self.assertEqual(app.get_file_attributes(folder), original)

    def test_scan_hides_imported_movie_and_removes_missing_record(self):
        video = self.root / "自动同步.mp4"
        video.write_bytes(b"video")
        with mock.patch.object(app, "hide_media_file", return_value=(True, 32)):
            app.scan_roots([str(self.root)])
        with app.connect() as conn:
            row = conn.execute(
                "SELECT hidden_by_app,original_file_attributes FROM movies WHERE path=?",
                (str(video.resolve()),),
            ).fetchone()
        self.assertEqual(row["hidden_by_app"], 1)
        self.assertEqual(row["original_file_attributes"], 32)
        self.assertEqual(app.SCAN.snapshot()["hidden"], 1)

        video.unlink()
        app.scan_roots([str(self.root)])
        with app.connect() as conn:
            row = conn.execute("SELECT exists_now,file_status FROM movies").fetchone()
            self.assertEqual(row["exists_now"], 0)
            self.assertEqual(row["file_status"], "missing")
        self.assertEqual(app.SCAN.snapshot()["removed"], 1)

    def test_scan_hides_direct_movie_folder_but_never_scan_root(self):
        movie_folder = self.root / "影片分类"
        movie_folder.mkdir()
        video = movie_folder / "目录隐藏.mp4"
        video.write_bytes(b"video")
        with mock.patch.object(app, "hide_media_file", return_value=(True, 32)) as hide:
            app.scan_roots([str(self.root)])
        hidden_targets = [Path(call.args[0]) for call in hide.call_args_list]
        self.assertTrue(any(path.name == video.name for path in hidden_targets))
        self.assertTrue(any(path.name == movie_folder.name for path in hidden_targets))
        self.assertFalse(any(path == self.root for path in hidden_targets))
        with app.connect() as conn:
            row = conn.execute("SELECT path,original_file_attributes FROM hidden_folders").fetchone()
        self.assertEqual(Path(row["path"]).name, movie_folder.name)
        self.assertEqual(row["original_file_attributes"], 32)
        self.assertEqual(app.SCAN.snapshot()["folders_hidden"], 1)

    def test_recycling_last_movie_restores_hidden_folder(self):
        movie_folder = self.root / "待恢复目录"
        movie_folder.mkdir()
        video = movie_folder / "待删除.mp4"
        video.write_bytes(b"video")
        timestamp = app.now_iso()
        app.register_scan_root(str(self.root))
        with app.connect() as conn:
            movie_id = conn.execute(
                "INSERT INTO movies(path,filename,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(video.resolve()), video.name, "待删除", timestamp, timestamp),
            ).lastrowid
            conn.execute(
                "INSERT INTO hidden_folders(path,root_path,original_file_attributes,created_at) VALUES(?,?,?,?)",
                (str(movie_folder.resolve()), str(self.root.resolve()), 32, timestamp),
            )
        with mock.patch.object(app, "restore_media_file", return_value=True) as restore:
            app.recycle_movie_file(movie_id, recycler=lambda path: path.unlink())
        restore.assert_called_once()
        restored_path, restored_mask = restore.call_args.args
        self.assertEqual(Path(restored_path).name, movie_folder.name)
        self.assertEqual(restored_mask, 32)
        with app.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM hidden_folders").fetchone()[0], 0)

    def test_disabling_privacy_restores_app_hidden_movie(self):
        video = self.root / "恢复显示.mp4"
        video.write_bytes(b"video")
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('hide_files_after_import','false')")
            conn.execute(
                """INSERT INTO movies(
                    path,filename,title,file_size,modified_at,hidden_by_app,original_file_attributes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (str(video.resolve()), video.name, "恢复显示", video.stat().st_size, video.stat().st_mtime, 1, 32, timestamp, timestamp),
            )
        with mock.patch.object(app, "restore_media_file", return_value=True) as restore:
            app.scan_roots([str(self.root)])
        restore.assert_called_once_with(video, 32)
        with app.connect() as conn:
            row = conn.execute("SELECT hidden_by_app,original_file_attributes FROM movies").fetchone()
        self.assertEqual(row["hidden_by_app"], 0)
        self.assertEqual(row["original_file_attributes"], -1)

    def test_removing_source_restores_only_files_hidden_by_app(self):
        owned = self.root / "由影库隐藏.mp4"
        prehidden = self.root / "原本隐藏.mp4"
        owned.write_bytes(b"one")
        prehidden.write_bytes(b"two")
        timestamp = app.now_iso()
        app.register_scan_root(str(self.root))
        with app.connect() as conn:
            conn.executemany(
                """INSERT INTO movies(
                    path,filename,title,hidden_by_app,original_file_attributes,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (str(owned.resolve()), owned.name, "Owned", 1, 32, timestamp, timestamp),
                    (str(prehidden.resolve()), prehidden.name, "Prehidden", 0, -1, timestamp, timestamp),
                ],
            )
        with mock.patch.object(app, "restore_media_file", return_value=True) as restore:
            removed = app.remove_scan_root(str(self.root))
        self.assertEqual(removed, 2)
        restore.assert_called_once()
        restored_path, restored_mask = restore.call_args.args
        self.assertEqual(Path(restored_path).name, owned.name)
        self.assertEqual(restored_mask, 32)

    def test_movie_is_hidden_only_after_recycler_succeeds(self):
        video = self.root / "待删除.mp4"
        video.write_bytes(b"video")
        timestamp = app.now_iso()
        with app.connect() as conn:
            movie_id = conn.execute(
                "INSERT INTO movies(path,filename,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(video), video.name, "待删除", timestamp, timestamp),
            ).lastrowid
        app.recycle_movie_file(movie_id, recycler=lambda path: path.unlink())
        self.assertFalse(video.exists())
        with app.connect() as conn:
            row = conn.execute("SELECT exists_now,disposition FROM movies WHERE id=?", (movie_id,)).fetchone()
        self.assertEqual(row["exists_now"], 0)
        self.assertEqual(row["disposition"], "delete")

    def test_actor_like_and_rating_filters_are_composable(self):
        timestamp = app.now_iso()
        cast_a = '[{"name": "演员甲", "avatar": ""}]'
        cast_b = '[{"name": "演员乙", "avatar": ""}]'
        with app.connect() as conn:
            conn.executemany(
                "INSERT INTO movies(path,filename,title,cast_json,favorite,personal_rating,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                [
                    (str(self.root / "a.mp4"), "a.mp4", "A", cast_a, 1, 9, timestamp, timestamp),
                    (str(self.root / "b.mp4"), "b.mp4", "B", cast_a, 0, 9, timestamp, timestamp),
                    (str(self.root / "c.mp4"), "c.mp4", "C", cast_b, 1, 9, timestamp, timestamp),
                ],
            )
        movies = desktop.query_movies(actor="演员甲", favorite_filter="liked", rating_filter="high")
        self.assertEqual([movie["title"] for movie in movies], ["A"])

    def test_existing_actor_avatar_is_reused_across_movies(self):
        timestamp = app.now_iso()
        cast_with = '[{"name": "演员甲", "avatar": "https://example.test/a.jpg"}]'
        cast_without = '[{"name": "演员甲", "avatar": ""}]'
        with app.connect() as conn:
            conn.executemany(
                "INSERT INTO movies(path,filename,title,cast_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                [
                    (str(self.root / "one.mp4"), "one.mp4", "One", cast_with, timestamp, timestamp),
                    (str(self.root / "two.mp4"), "two.mp4", "Two", cast_without, timestamp, timestamp),
                ],
            )
        app.discover_actor_profiles()
        with app.connect() as conn:
            cast = app.json_value(conn.execute("SELECT cast_json FROM movies WHERE title='Two'").fetchone()[0], [])
            profile = conn.execute("SELECT avatar_url,status FROM actor_profiles").fetchone()
        self.assertEqual(cast[0]["avatar"], "https://example.test/a.jpg")
        self.assertEqual(profile["status"], "matched")

    def test_new_actor_avatar_is_enriched_and_written_back(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO movies(path,filename,title,cast_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (str(self.root / "new.mp4"), "new.mp4", "New", '[{"name": "新演员", "avatar": ""}]', timestamp, timestamp),
            )
        found = ({"avatar": "https://example.test/new.jpg", "source": "tmdb", "source_id": "7"}, [])
        with mock.patch.object(app, "lookup_actor_avatar", return_value=found):
            result = app.enrich_actor_avatars()
        self.assertEqual(result["matched"], 1)
        with app.connect() as conn:
            cast = app.json_value(conn.execute("SELECT cast_json FROM movies").fetchone()[0], [])
            profile = conn.execute("SELECT avatar_url,source,status FROM actor_profiles").fetchone()
        self.assertEqual(cast[0]["avatar"], "https://example.test/new.jpg")
        self.assertEqual(profile["source"], "tmdb")
        self.assertEqual(profile["status"], "matched")

    def test_forced_actor_refresh_replaces_only_successful_results(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute(
                "INSERT INTO movies(path,filename,title,cast_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (str(self.root / "actor.mp4"), "actor.mp4", "Actor", '[{"name":"Actor A","avatar":"old.jpg"}]', timestamp, timestamp),
            )
        app.discover_actor_profiles()
        fresh = ({"avatar": "new.jpg", "display_name": "演员甲", "source": "tmdb", "source_id": "1"}, [])
        with mock.patch.object(app, "lookup_actor_avatar", return_value=fresh), mock.patch.object(
            app, "collect_actor_photo_candidates", return_value=([fresh[0]], [])
        ):
            result = app.refresh_actor_profiles(["actor a"])
        self.assertEqual(result["avatar_updated"], 1)
        with app.connect() as conn:
            cast = app.json_value(conn.execute("SELECT cast_json FROM movies").fetchone()[0], [])
            profile = conn.execute("SELECT avatar_url,display_name FROM actor_profiles").fetchone()
        self.assertEqual(cast[0]["avatar"], "new.jpg")
        self.assertEqual(profile["display_name"], "演员甲")

        with mock.patch.object(app, "lookup_actor_avatar", return_value=(None, ["offline"])), mock.patch.object(
            app, "collect_actor_photo_candidates", return_value=([], ["offline"])
        ):
            app.refresh_actor_profiles(["actor a"])
        with app.connect() as conn:
            profile = conn.execute("SELECT avatar_url,display_name FROM actor_profiles").fetchone()
        self.assertEqual(profile["avatar_url"], "new.jpg")
        self.assertEqual(profile["display_name"], "演员甲")

    def test_normal_actor_refresh_skips_profiles_with_existing_avatar(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute(
                """INSERT INTO actor_profiles(name_key,name,avatar_url,status,updated_at)
                VALUES(?,?,?,?,?)""",
                ("actor a", "Actor A", "saved.jpg", "matched", timestamp),
            )
        with mock.patch.object(app, "lookup_actor_avatar") as lookup:
            result = app.refresh_actor_profiles(["actor a"], only_missing=True)
        lookup.assert_not_called()
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_right_click_force_refresh_still_queries_existing_avatar(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute(
                """INSERT INTO actor_profiles(name_key,name,avatar_url,status,updated_at)
                VALUES(?,?,?,?,?)""",
                ("actor a", "Actor A", "saved.jpg", "matched", timestamp),
            )
        found = ({"avatar": "fresh.jpg", "source": "tmdb", "source_id": "9"}, [])
        with mock.patch.object(app, "lookup_actor_avatar", return_value=found) as lookup, mock.patch.object(
            app, "collect_actor_photo_candidates", return_value=([found[0]], [])
        ):
            result = app.refresh_actor_profiles(["actor a"])
        lookup.assert_called_once()
        self.assertEqual(result["processed"], 1)
        with app.connect() as conn:
            avatar = conn.execute("SELECT avatar_url FROM actor_profiles").fetchone()[0]
        self.assertEqual(avatar, "fresh.jpg")

    def test_actor_match_result_is_persisted_and_not_retried_each_start(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute(
                """INSERT INTO actor_profiles(name_key,name,display_name,avatar_url,source,status,last_attempt,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("actor a", "Actor A", "", "photo.jpg", "tmdb", "matched", timestamp, timestamp),
            )
        self.assertEqual(app.pending_actor_profiles(), [])

    def test_actor_photo_library_caps_at_50_and_switches_selected_photo(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute(
                """INSERT INTO actor_profiles(name_key,name,display_name,avatar_url,source,status,updated_at)
                VALUES(?,?,?,?,?,?,?)""",
                ("actor a", "Actor A", "演员甲", "", "", "new", timestamp),
            )
        photos = [
            {"avatar": f"https://example.test/{index}.jpg", "source": "test", "source_id": str(index)}
            for index in range(60)
        ]
        app.store_actor_photos("actor a", photos)
        choices = app.actor_photo_choices("actor a")
        self.assertEqual(len(choices), 50)
        selected = choices[10]["photo_url"]
        app.select_actor_photo("actor a", selected)
        with app.connect() as conn:
            profile = conn.execute("SELECT avatar_url FROM actor_profiles WHERE name_key='actor a'").fetchone()
            selected_count = conn.execute(
                "SELECT COUNT(*) FROM actor_photos WHERE name_key='actor a' AND selected=1"
            ).fetchone()[0]
        self.assertEqual(profile["avatar_url"], selected)
        self.assertEqual(selected_count, 1)

    def test_startup_duration_filter_removes_only_short_records(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.execute("INSERT INTO settings(key,value) VALUES('min_duration_minutes','10')")
            conn.executemany(
                "INSERT INTO movies(path,filename,title,duration_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                [
                    (str(self.root / "short.mp4"), "short.mp4", "Short", 599, timestamp, timestamp),
                    (str(self.root / "long.mp4"), "long.mp4", "Long", 600, timestamp, timestamp),
                ],
            )
        result = app.enforce_duration_threshold()
        self.assertEqual(result["removed"], 1)
        with app.connect() as conn:
            titles = [row["title"] for row in conn.execute("SELECT title FROM movies")]
        self.assertEqual(titles, ["Long"])

    def test_play_count_and_last_played_are_recorded(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            movie_id = conn.execute(
                "INSERT INTO movies(path,filename,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(self.root / "play.mp4"), "play.mp4", "Play", timestamp, timestamp),
            ).lastrowid
        self.assertEqual(desktop.record_movie_play(movie_id), 1)
        self.assertEqual(desktop.record_movie_play(movie_id), 2)
        with app.connect() as conn:
            row = conn.execute("SELECT play_count,last_played_at,watch_status FROM movies WHERE id=?", (movie_id,)).fetchone()
        self.assertEqual(row["play_count"], 2)
        self.assertTrue(row["last_played_at"])
        self.assertEqual(row["watch_status"], "watching")

    def test_wikidata_actor_image_builds_direct_commons_thumbnail(self):
        search_result = {
            "search": [{
                "id": "Q7", "label": "Kaho Hamabe", "description": "Japanese AV idol",
                "match": {"text": "Kaho Hamabe"},
            }]
        }
        entity_result = {
            "entities": {"Q7": {"claims": {"P18": [{
                "rank": "normal",
                "mainsnak": {"datavalue": {"value": "Actor Portrait.jpg"}},
            }]}}}
        }
        with mock.patch.object(app, "http_json", side_effect=[search_result, entity_result]):
            result = app.search_wikidata_actor_avatar("Kaho Hamabe")
        self.assertEqual(result["source"], "wikidata")
        self.assertEqual(result["source_id"], "Q7")
        self.assertIn("upload.wikimedia.org/wikipedia/commons/thumb/", result["avatar"])
        self.assertIn("330px-Actor_Portrait.jpg", result["avatar"])

    def test_bangumi_person_search_matches_alias_and_keeps_chinese_profile(self):
        payload = {
            "data": [{
                "id": 3862, "name": "田中理恵", "summary": "日本声优。",
                "images": {"large": "https://example.test/rie.jpg"},
                "infobox": [
                    {"key": "简体中文名", "value": "田中理惠"},
                    {"key": "别名", "value": [{"k": "罗马字", "v": "Tanaka Rie"}]},
                    {"key": "生日", "value": "1979-01-03"},
                ],
                "stat": {"collects": 500},
            }]
        }
        with mock.patch.object(app, "http_json", return_value=payload):
            result = app.search_bangumi_actor_profile("Tanaka Rie")
        self.assertEqual(result["display_name"], "田中理惠")
        self.assertIn("Tanaka Rie", result["aliases"])
        self.assertEqual(result["info"]["生日"], "1979-01-03")

    def test_catalog_movie_actor_link_and_profile_are_preserved(self):
        movie_html = """<p><b>Idol(s)/Actress(es):</b>
        <a href=\"https://www.javdatabase.com/idols/tsukasa-aoi/\">Tsukasa Aoi</a></p>"""
        profile_html = """
        <h1 class=\"idol-name\">Tsukasa Aoi - JAV Profile</h1>
        <img src=\"https://www.javdatabase.com/idolimages/full/tsukasa-aoi.webp\" alt=\"Tsukasa Aoi\">
        <b>DOB:</b> <a>1990-08-14</a> - <b>Birthplace:</b> <a>Osaka</a><br>
        <b>Height:</b> <a>163 cm</a> - <b>Cup:</b> <a>E</a><br>
        <b>JP:</b> 葵つかさ <br>
        """
        with mock.patch("javdb.__main__._fetch_html", return_value=movie_html):
            actors = app.fetch_catalog_movie_actors("https://example.test/movie")
        self.assertEqual(actors[0]["name"], "Tsukasa Aoi")
        self.assertEqual(actors[0]["source"], "catalog-actor")
        with mock.patch("javdb.__main__._fetch_html", return_value=profile_html):
            profile = app.search_catalog_actor_profile("Tsukasa Aoi", actors[0]["profile_url"])
        self.assertEqual(profile["avatar"], "https://www.javdatabase.com/idolimages/full/tsukasa-aoi.webp")
        self.assertIn("葵つかさ", profile["aliases"])
        self.assertEqual(profile["info"]["生日"], "1990-08-14")

    def test_screenshot_generation_status_is_persisted(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            movie_id = conn.execute(
                "INSERT INTO movies(path,filename,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (str(self.root / "shot.mp4"), "shot.mp4", "Shot", timestamp, timestamp),
            ).lastrowid
        desktop.update_screenshot_status(movie_id, "failed")
        movie = desktop.get_movie(movie_id)
        self.assertEqual(movie["screenshots_status"], "failed")
        self.assertTrue(movie["screenshots_attempted_at"])

    def test_actor_facets_use_chinese_name_and_favorite_count(self):
        timestamp = app.now_iso()
        cast = '[{"name": "Kaho Hamabe", "avatar": ""}]'
        with app.connect() as conn:
            conn.executemany(
                "INSERT INTO movies(path,filename,title,cast_json,favorite,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                [
                    (str(self.root / "liked.mp4"), "liked.mp4", "Liked", cast, 1, timestamp, timestamp),
                    (str(self.root / "plain.mp4"), "plain.mp4", "Plain", cast, 0, timestamp, timestamp),
                ],
            )
            conn.execute(
                """INSERT INTO actor_profiles(name_key,name,display_name,avatar_url,status,updated_at)
                VALUES(?,?,?,?,?,?)""",
                ("kaho hamabe", "Kaho Hamabe", "滨部花穗", "https://example.test/kaho.jpg", "matched", timestamp),
            )
        actor = desktop.query_actor_facets()[0]
        self.assertEqual(actor["display_name"], "滨部花穗")
        self.assertEqual(actor["count"], 2)
        self.assertEqual(actor["favorite_count"], 1)

    def test_actor_aliases_merge_facets_and_filter_all_name_variants(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.executemany(
                "INSERT INTO movies(path,filename,title,cast_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                [
                    (str(self.root / "roman.mp4"), "roman.mp4", "Roman", '[{"name":"Tsukasa Aoi"}]', timestamp, timestamp),
                    (str(self.root / "native.mp4"), "native.mp4", "Native", '[{"name":"葵つかさ"}]', timestamp, timestamp),
                ],
            )
            conn.execute(
                """INSERT INTO actor_profiles(name_key,name,display_name,aliases_json,status,updated_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    "tsukasa aoi", "Tsukasa Aoi", "葵つかさ",
                    '["Tsukasa Aoi","葵つかさ"]', "matched", timestamp,
                ),
            )
        facets = desktop.query_actor_facets()
        self.assertEqual(len(facets), 1)
        self.assertEqual(facets[0]["count"], 2)
        self.assertEqual({movie["title"] for movie in desktop.query_movies(actor="Tsukasa Aoi")}, {"Roman", "Native"})

    def test_expanded_actor_strip_wraps_and_uses_vertical_scroll(self):
        timestamp = app.now_iso()
        with app.connect() as conn:
            conn.executemany(
                "INSERT INTO movies(path,filename,title,cast_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                [
                    (
                        str(self.root / f"actor-{index}.mp4"), f"actor-{index}.mp4", f"Actor {index}",
                        f'[{json.dumps({"name": f"演员{index}"}, ensure_ascii=False)}]', timestamp, timestamp,
                    )
                    for index in range(30)
                ],
            )
        qt_app = QApplication.instance() or QApplication([])
        with mock.patch.dict(os.environ, {
            "YINGKU_SETTINGS_PATH": str(self.root / "ui.ini"),
            "YINGKU_DISABLE_STARTUP_TASKS": "1",
        }):
            window = desktop.MainWindow()
            window.actor_scroll.resize(700, 166)
            self.assertEqual(len(window.actor_chips), 30)
            self.assertEqual(window.actor_layout.rowCount(), 1)
            import sys as _sys
            expected_policy = (
                Qt.ScrollBarPolicy.ScrollBarAlwaysOn if _sys.platform == 'win32'
                else Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            self.assertEqual(
                window.actor_scroll.horizontalScrollBarPolicy(),
                expected_policy,
            )
            window.actors_expanded = True
            window.load_actor_strip()
            window.reflow_actor_chips()
            self.assertGreater(window.actor_layout.rowCount(), 1)
            self.assertEqual(
                window.actor_scroll.horizontalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertEqual(
                window.actor_scroll.verticalScrollBarPolicy(),
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
            )
            self.assertGreater(window.actor_scroll.height(), 184)
            self.assertIs(window.scroll, window.library_scroll)
            app.ACTOR_ENRICH.reset()
            app.ACTOR_ENRICH.update(running=True, total=30, processed=4, current="演员5", matched=2, last_result="演员4：资料无变化")
            window.update_actor_enrichment_progress()
            self.assertIn("演员5", window.actor_update_status.text())
            self.assertIn("演员4：资料无变化", window.actor_update_status.text())
            window.close()
        self.assertIsNotNone(qt_app)

    def test_portrait_and_landscape_layouts_are_saved_separately(self):
        qt_app = QApplication.instance() or QApplication([])
        settings_path = self.root / "orientation.ini"
        with mock.patch.dict(os.environ, {
            "YINGKU_SETTINGS_PATH": str(settings_path),
            "YINGKU_DISABLE_STARTUP_TASKS": "1",
        }):
            window = desktop.MainWindow()
            window.show()
            window.resize(900, 1200)
            qt_app.processEvents()
            self.assertEqual(window.layout_mode, "portrait")
            self.assertEqual(window.splitter.orientation(), Qt.Orientation.Vertical)
            window.detail_poster.setFixedHeight(333)
            window.splitter.setSizes([640, 480])
            window.save_ui_state()
            self.assertTrue(window.ui_settings.contains("windowGeometry_portrait"))
            self.assertTrue(window.ui_settings.contains("mainSplitter_portrait"))
            self.assertEqual(window.ui_settings.value("detailHeroHeight_portrait", type=int), 333)

            window.resize(1400, 800)
            qt_app.processEvents()
            self.assertEqual(window.layout_mode, "landscape")
            self.assertEqual(window.splitter.orientation(), Qt.Orientation.Horizontal)
            window.detail_poster.setFixedHeight(444)
            window.splitter.setSizes([760, 520])
            window.save_ui_state()
            self.assertTrue(window.ui_settings.contains("windowGeometry_landscape"))
            self.assertTrue(window.ui_settings.contains("mainSplitter_landscape"))
            self.assertEqual(window.ui_settings.value("detailHeroHeight_landscape", type=int), 444)

            window.resize(900, 1200)
            qt_app.processEvents()
            self.assertEqual(window.detail_poster.height(), 333)
            window.close()
            restored = desktop.MainWindow()
            restored.show()
            qt_app.processEvents()
            self.assertEqual(restored.layout_mode, "portrait")
            self.assertEqual(restored.splitter.orientation(), Qt.Orientation.Vertical)
            self.assertEqual(restored.detail_poster.height(), 333)
            restored.close()

    def test_screenshot_preview_supports_buttons_and_arrow_keys(self):
        qt_app = QApplication.instance() or QApplication([])
        images = mock.Mock()
        dialog = desktop.PreviewDialog(["one.jpg", "two.jpg", "three.jpg"], "two.jpg", images)
        self.assertEqual(dialog.index, 1)
        self.assertEqual(dialog.counter.text(), "2 / 3  ·  可使用键盘 ← → 切换")
        dialog.next_button.click()
        self.assertEqual(dialog.index, 2)
        self.assertFalse(dialog.next_button.isEnabled())
        dialog.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(dialog.index, 1)
        dialog.previous_button.click()
        self.assertEqual(dialog.index, 0)
        self.assertFalse(dialog.previous_button.isEnabled())
        self.assertGreaterEqual(images.load.call_count, 4)
        dialog.close()
        self.assertIsNotNone(qt_app)


if __name__ == "__main__":
    unittest.main()
