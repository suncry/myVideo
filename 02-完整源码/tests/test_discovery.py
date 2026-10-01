import json,tempfile,unittest
from pathlib import Path
from unittest import mock
from PySide6.QtWidgets import QApplication
import app as core,discovery,public_sources,desktop,privacy


class DiscoveryTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.saved=(core.DATA_DIR,core.DB_PATH,privacy.MODE,privacy.BASE)
  privacy.configure(self.root/'library',True);core.init_db()
  with core.connect() as c:c.execute("INSERT OR REPLACE INTO settings VALUES('auto_match_after_scan','false')")
 def tearDown(self):core.DATA_DIR,core.DB_PATH,privacy.MODE,privacy.BASE=self.saved;self.temp.cleanup()
 def actor(self,name,genres=('剧情','悬疑')):
  return discovery.save_public_actor(dict(name=name,source='wikidata',source_id='Q123',biography='公开介绍',works=[dict(title='作品',genres=list(genres),source='测试公开源',url='https://example.test/work')],sources=[dict(label='测试公开源',url='https://example.test/person')]))
 def test_schema_upgrade_is_repeatable_and_preserves_data(self):
  self.actor('甲');discovery.set_actor_favorite('甲',True);core.init_db();core.init_db()
  self.assertTrue(discovery.actor_profile('甲')['favorite'])
 def test_favorite_external_actor_survives_movie_pruning(self):
  self.actor('甲');discovery.set_actor_favorite('甲',True)
  with core.connect() as c:core._prune_unused_actor_profiles(c)
  self.assertTrue(discovery.actor_profile('甲')['favorite']);self.assertEqual(len(discovery.actor_list(favorite_only=True)),1)
 def test_same_tags_match_and_unrelated_and_self_are_excluded(self):
  self.actor('甲');self.actor('乙');self.actor('丙',('纪录片',))
  rec=discovery.local_recommendations('甲');self.assertEqual([p['name'] for p in rec],['乙']);self.assertEqual(set(rec[0]['shared_keywords']),{'剧情','悬疑'})
 def test_private_actor_favorites_and_insights_do_not_leak_to_public(self):
  self.actor('私密演员');discovery.set_actor_favorite('私密演员',True)
  privacy.configure(self.root/'library',False);core.init_db();self.assertEqual(discovery.actor_list(),[])
 def test_keywords_have_evidence_and_do_not_invent_personality(self):
  tags=discovery.keywords('一段关于友情与成长的故事',['剧情'],[dict(label='资料',url='https://example.test')])
  self.assertEqual({t['label'] for t in tags},{'友情','成长','剧情'});self.assertTrue(all(t['sources'] and t['evidence'] for t in tags))
 def test_reviews_require_multiple_positive_samples_and_reject_negation(self):
  good=dict(content='excellent performance',author_details={'rating':9},url='https://example.test/review')
  self.assertEqual(discovery.review_keywords([good]),[])
  self.assertEqual(discovery.review_keywords([good,good])[0]['label'],'表演受好评')
  self.assertEqual(discovery.review_keywords([dict(good,content='not excellent performance'),good]),[])
 def test_source_failure_keeps_biography_and_favorite(self):
  self.actor('甲');discovery.set_actor_favorite('甲',True)
  with mock.patch.object(public_sources,'fetch_actor',side_effect=ValueError('offline')):
   with self.assertRaises(ValueError):discovery.refresh_actor('甲')
  p=discovery.actor_profile('甲');self.assertTrue(p['favorite']);self.assertEqual(p['biography'],'公开介绍')
 def test_review_refresh_stores_only_evidence_not_full_comments(self):
  with core.connect() as c:
   mid=c.execute('INSERT INTO movies(path,filename,title,created_at,updated_at) VALUES(?,?,?,?,?)',(str(self.root/'film.mp4'),'film.mp4','影片',core.now_iso(),core.now_iso())).lastrowid
  public={'keywords':[dict(label='剧情',kind='类型',sources=[],evidence='作品类型')],'review_sample_count':2}
  with mock.patch.object(public_sources,'fetch_movie',return_value=public):discovery.refresh_movie(mid)
  with core.connect() as c:data=c.execute('SELECT insights_json FROM movies WHERE id=?',(mid,)).fetchone()[0]
  self.assertEqual(json.loads(data)['review_sample_count'],2);self.assertNotIn('content',data)
 def test_actor_page_favorite_search_and_return_to_films(self):
  self.actor('测试演员');window=desktop.MainWindow()
  try:
   window.set_view('actors');self.assertEqual(window.content_stack.currentIndex(),1)
   page=window.actor_library;page.show_actor('测试演员');self.assertEqual(page.stack.currentIndex(),1)
   page.save.click();self.assertTrue(discovery.actor_profile('测试演员')['favorite'])
   page.go_back();page.scope.setCurrentIndex(1);self.qt.processEvents();self.assertEqual(len(page.tiles),1)
   window.view_actor_films('测试演员');self.assertEqual(window.content_stack.currentIndex(),0);self.assertEqual(window.actor_filter,'测试演员')
  finally:window.close();window.deleteLater();self.qt.processEvents()
 def test_title_keyword_strip_stays_compact_and_exposes_overflow(self):
  from insights_ui import KeywordStrip
  strip=KeywordStrip(limit=5);strip.set_keywords([dict(label='关键词'+str(i),kind='类型',evidence='资料',sources=[]) for i in range(10)])
  strip.resize(330,35);strip.show();self.qt.processEvents()
  self.assertEqual(strip.line.count(),7);self.assertLessEqual(strip.height(),40);strip.close();strip.deleteLater()
 def test_movie_public_sources_requires_confirmed_identity(self):
  with self.assertRaisesRegex(ValueError,'关联 TMDb'):public_sources.fetch_movie({'source':'local','source_id':'','title':'同名电影'})
 def test_tmdb_details_extracts_real_credits_and_filters_adult(self):
  data={'name':'测试演员','biography':'公开介绍','combined_credits':{'cast':[{'id':1,'title':'一般电影','genre_ids':[18],'media_type':'movie','vote_count':100,'popularity':5},{'id':2,'title':'排除条目','adult':True}]}}
  c=mock.Mock();c.tmdb.return_value=data;p=public_sources.tmdb_actor(c,'7');self.assertEqual([w['title'] for w in p['works']],['一般电影']);self.assertEqual(p['works'][0]['genres'],['剧情'])
 def test_exact_name_wins_over_ambiguous_alias_and_refresh_keeps_chosen_avatar(self):
  discovery.save_public_actor(dict(name='甲',aliases=['乙','共享'],avatar='https://example.test/chosen.jpg',works=[]))
  discovery.save_public_actor(dict(name='乙',aliases=['共享'],avatar='https://example.test/new.jpg',works=[]))
  self.assertEqual(discovery.actor_profile('乙')['name'],'乙');self.assertIsNone(discovery.actor_profile('共享'))
  discovery.save_public_actor(dict(name='甲',avatar='https://example.test/remote.jpg',info={'生日':'2000-01-01'},works=[]))
  p=discovery.actor_profile('甲');self.assertEqual(p['avatar'],'https://example.test/chosen.jpg');self.assertEqual(p['info']['生日'],'2000-01-01')
  with core.connect() as c:
   selected=c.execute('SELECT photo_url FROM actor_photos WHERE name_key=? AND selected=1',('甲',)).fetchone()
  self.assertEqual(selected[0],'https://example.test/chosen.jpg')
 def test_wikidata_candidates_survive_partial_genre_failure_and_exclude_self(self):
  def claim(q):return {'mainsnak':{'datavalue':{'value':{'id':q}}}}
  def entities(ids):
   if 'Qmovie' in ids:return {'Qmovie':{'claims':{'P161':[claim('Q1'),claim('Q2')]}}}
   return {'Q2':{'labels':{'zh':{'value':'候选演员'}},'claims':{'P31':[claim('Q5')]}}}
  client=mock.Mock();client.search_claims.side_effect=[OSError('temporary'),['Qmovie']];client.entities.side_effect=entities
  profile={'source_id':'Q1','insights':{'wikidata_genres':['Q10','Q20']}}
  with mock.patch.object(public_sources,'PublicClient',return_value=client):pool,warnings=public_sources.similar_candidates(profile)
  self.assertEqual([p['source_id'] for p in pool],['Q2']);self.assertTrue(warnings)
 def test_public_transport_retries_once_and_budget_is_bounded(self):
  client=public_sources.PublicClient()
  with mock.patch.object(core,'http_json',side_effect=[OSError('transient'),{'ok':True}]) as get:
   self.assertEqual(client.get('https://example.test'),{'ok':True});self.assertEqual(get.call_count,2)
  client.requests=60
  with self.assertRaisesRegex(ValueError,'限额'):client.get('https://example.test')

 def test_recommendation_refresh_reuses_confirmed_cached_actor(self):
  self.actor('甲');self.actor('乙')
  with core.connect() as c:
   p=discovery.actor_profile('甲')['insights'];p['fetched_at']='2026-09-28T00:00:00';c.execute('UPDATE actor_profiles SET insights_json=? WHERE name_key=?',(json.dumps(p),'甲'))
   p=discovery.actor_profile('乙')['insights'];p['fetched_at']='2026-09-28T00:00:00';c.execute('UPDATE actor_profiles SET insights_json=? WHERE name_key=?',(json.dumps(p),'乙'))
  candidate={'name':'乙','source':'wikidata','source_id':'Q123'}
  with mock.patch.object(public_sources,'similar_candidates',return_value=([candidate],[])),mock.patch.object(public_sources,'fetch_actor') as fetch:
   result=discovery.discover_similar('甲')
  fetch.assert_not_called();self.assertEqual(result['candidates_checked'],1);self.assertEqual(result['recommendations'][0]['name'],'乙')
