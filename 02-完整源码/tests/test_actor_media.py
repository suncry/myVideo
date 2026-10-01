import json,tempfile,unittest
from pathlib import Path
from unittest import mock
from PySide6.QtCore import Qt,QSize
from PySide6.QtWidgets import QApplication,QWidget,QLabel,QHBoxLayout
import app as core,privacy,discovery,actor_media,desktop
from media_ui import ThumbnailRail,ImageViewer

class ActorMediaTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.saved=(core.DATA_DIR,core.DB_PATH,privacy.MODE,privacy.BASE)
  privacy.configure(Path(self.tmp.name)/'library',True);core.init_db()
 def tearDown(self):core.DATA_DIR,core.DB_PATH,privacy.MODE,privacy.BASE=self.saved;self.tmp.cleanup()
 def profile(self):
  return {'name':'演员','source':'wikidata','source_id':'Q1','avatar':'https://example.test/chosen.jpg','photos':[], 'works':[{'id':'Q2','media_type':'work','title':'作品','genres':['剧情']}],'sources':[],'fetched_at':core.now_iso()}
 def claim(self,value):return {'mainsnak':{'datavalue':{'value':value}}}
 def test_gallery_uses_confirmed_commons_category_and_ignores_non_photographic_files(self):
  c=mock.Mock();c.get.side_effect=[{'query':{'categorymembers':[]}}, {'query':{'pages':[{'title':'File:Photo.jpg','imageinfo':[{'mime':'image/jpeg','width':1000,'height':1200,'thumburl':'https://example.test/photo.jpg','descriptionurl':'https://commons.wikimedia.org/wiki/File:Photo.jpg','extmetadata':{'LicenseShortName':{'value':'CC BY-SA 4.0'},'Artist':{'value':'<a>摄影师</a>'}}}]},{'title':'File:Logo.svg','imageinfo':[{'mime':'image/svg+xml','width':1000,'height':1000,'url':'https://example.test/logo.svg'}]}]}}]
  entity={'claims':{'P373':[self.claim('Confirmed Actor')]}}
  photos,warnings=actor_media.commons_photos(c,entity,'Q1');self.assertEqual(len(photos),1);self.assertEqual(photos[0]['credit'],'摄影师');self.assertEqual(photos[0]['license'],'CC BY-SA 4.0');self.assertIn('Confirmed+Actor',c.get.call_args.args[0]);self.assertFalse(warnings)
  empty=mock.Mock();self.assertEqual(actor_media.commons_photos(empty,{},'Q1'),([],[]));empty.get.assert_not_called()
 def test_exact_linked_cover_identity_redirect_and_mismatch(self):
  works=[{'id':'Q2','title':'作品'},{'id':'Q3','title':'同名作品'}];entities={'Q2':{'sitelinks':{'enwiki':{'title':'Film alias'}}},'Q3':{'sitelinks':{'enwiki':{'title':'Namesake'}}}}
  c=mock.Mock();c.get.return_value={'query':{'redirects':[{'from':'Film alias','to':'Film (2000)'}],'pages':[{'title':'Film (2000)','pageprops':{'wikibase_item':'Q2'},'thumbnail':{'source':'https://example.test/poster.jpg','width':200,'height':300}},{'title':'Namesake','pageprops':{'wikibase_item':'Q999'},'thumbnail':{'source':'https://example.test/wrong.jpg','width':200,'height':300}}]}}
  result,warnings=actor_media.wikipedia_covers(c,works,entities);self.assertEqual(result[0]['poster'],'https://example.test/poster.jpg');self.assertNotIn('poster',result[1]);self.assertNotIn('poster',works[0]);self.assertIn('pilicense=any',c.get.call_args.args[0])
 def test_landscape_work_image_is_retained_without_becoming_a_fake_poster(self):
  c=mock.Mock();c.get.return_value={'query':{'pages':[{'title':'Film','thumbnail':{'source':'https://example.test/still.jpg','width':800,'height':450}}]}}
  result,_=actor_media.wikipedia_covers(c,[{'id':'Q2','title':'影片'}],{'Q2':{'sitelinks':{'enwiki':{'title':'Film'}}}})
  self.assertNotIn('poster',result[0]);self.assertEqual(len(result[0]['images']),1)
 def test_media_refresh_preserves_favorite_selected_portrait_and_actor_identity(self):
  p=self.profile();discovery.save_public_actor(p);discovery.set_actor_favorite('演员',True)
  gallery={**p,'gallery_version':1,'photos':[{'photo_url':'https://example.test/photo.jpg','source':'Wikimedia Commons','source_id':'File:Photo.jpg'}],'works':[{**p['works'][0],'poster':'https://example.test/poster.jpg'}]}
  with mock.patch.object(actor_media,'fetch_gallery',return_value=gallery):discovery.refresh_actor_media('演员')
  profile=discovery.actor_profile('演员');self.assertTrue(profile['favorite']);self.assertEqual(profile['avatar'],p['avatar']);self.assertEqual(profile['works'][0]['poster'],'https://example.test/poster.jpg')
  core.select_actor_photo(profile['name_key'],'https://example.test/photo.jpg');profile=discovery.actor_profile('演员');self.assertEqual(profile['source'],'wikidata');self.assertEqual(profile['source_id'],'Q1')
 def test_metadata_refresh_keeps_artwork_for_same_identity_only(self):
  p=self.profile();old={**p,'gallery_version':1,'photos':[{'photo_url':'https://example.test/photo.jpg'}],'works':[{**p['works'][0],'poster':'https://example.test/poster.jpg'}]};discovery.save_public_actor(old);discovery.save_public_actor(p)
  profile=discovery.actor_profile('演员');self.assertEqual(profile['works'][0]['poster'],'https://example.test/poster.jpg');self.assertEqual(profile['insights']['gallery_version'],1)
  result=actor_media.merge_gallery(old,{**p,'source_id':'Q999'});self.assertNotIn('gallery_version',result)
 def test_thumbnail_variants_deduplicate(self):
  a='https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Actor.jpg/500px-Actor.jpg'
  b='https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Actor.jpg/960px-Actor.jpg?utm_source=commons'
  self.assertEqual(len(actor_media.unique_images([{'photo_url':a},{'photo_url':b}])),1)
 def test_offline_keeps_existing_photos_and_posters(self):
  p={**self.profile(),'photos':[{'photo_url':'https://example.test/photo.jpg'}]};c=mock.Mock();c.entities.side_effect=OSError('offline')
  result=actor_media.fetch_gallery(p,c);self.assertEqual(result['photos'],p['photos']);self.assertEqual(result['works'],p['works']);self.assertTrue(result['media_warnings'])
 def test_tmdb_work_gallery_has_posters_and_backdrops(self):
  c=mock.Mock();c.tmdb.return_value={'posters':[{'file_path':'/poster.jpg'}],'backdrops':[{'file_path':'/still.jpg'}]}
  result,_=actor_media.tmdb_artwork(c,[{'id':'7','media_type':'movie','title':'影片'}]);self.assertEqual(len(result[0]['images']),2);self.assertIn('poster.jpg',result[0]['poster']);self.assertIn('/images',c.tmdb.call_args.args[0])
 def test_preview_does_not_change_avatar_until_explicit_button(self):
  owner=QWidget();owner.images=mock.Mock();choose=mock.Mock();items=[{'photo_url':'https://example.test/1.jpg'},{'photo_url':'https://example.test/2.jpg'}]
  viewer=ImageViewer(owner,items,set_avatar=choose);viewer.show();self.qt.processEvents();viewer.navigate(1);choose.assert_not_called();self.assertFalse(owner.images.load.call_args.args[4]);viewer.avatar.click();choose.assert_called_once_with(items[1]['photo_url']);self.assertEqual(viewer.position.text(),'2 / 2');self.assertFalse(viewer.next.isEnabled());viewer.close();owner.close();viewer.deleteLater();owner.deleteLater();self.qt.processEvents()
 def test_unavailable_public_works_display_existing_library_covers(self):
  from PySide6.QtWidgets import QPushButton
  p=self.profile();p['works']=[];discovery.save_public_actor(p)
  window=desktop.MainWindow();profile=discovery.actor_profile('演员');profile['local_works']=[{'id':1,'title':'已有作品','poster_path':'/local/cover.jpg','year':'2024'}]
  try:
   with mock.patch.object(discovery,'actor_profile',return_value=profile),mock.patch.object(window.actor_library,'work_strip',return_value=QWidget()) as strip:
    window.actor_library.show_actor('演员');strip.assert_called_once_with(profile['local_works'],local=True)
    labels=[label.text() for label in window.actor_library.body.findChildren(QLabel)];self.assertTrue(any('公开代表作尚未确认' in label for label in labels))
    self.assertTrue(any(b.text()=='按此演员筛选影片' for b in window.actor_library.body.findChildren(QPushButton)))
  finally:window.close();window.deleteLater();self.qt.processEvents()
 def test_rail_requests_only_visible_thumbnails_and_loads_more_when_scrolled(self):
  rail=ThumbnailRail();rail.resize(400,180);container=QWidget();line=QHBoxLayout(container);line.setContentsMargins(0,0,0,0);manager=mock.Mock()
  for i in range(20):
   label=QLabel();label.setFixedSize(154,150);line.addWidget(label);rail.add_image(manager,label,'https://example.test/'+str(i),'图')
  container.setFixedSize(3300,160);rail.setWidget(container);rail.show();self.qt.processEvents();rail.load_visible();first=manager.load.call_count;self.assertGreater(first,0);self.assertLess(first,20)
  rail.horizontalScrollBar().setValue(2000);self.qt.processEvents();self.assertGreater(manager.load.call_count,first);rail.close();rail.deleteLater();self.qt.processEvents()
 def test_image_rate_limit_retries_once_with_context_timer(self):
  manager=desktop.ImageManager();label=QLabel();label.setProperty('imageSource','https://example.test/photo.jpg');reply=mock.Mock();reply.attribute.return_value=429;reply.rawHeader.return_value=b'3';reply.readAll.return_value=b''
  with mock.patch.object(desktop.QTimer,'singleShot') as timer:
   manager._finished(reply,'https://example.test/photo.jpg',label,QSize(100,100),True,False,False)
   self.assertEqual(timer.call_args.args[:2],(3000,manager))
   manager._finished(reply,'https://example.test/photo.jpg',label,QSize(100,100),True,False,False,1);self.assertEqual(timer.call_count,1)
  label.deleteLater();manager.deleteLater();self.qt.processEvents()
 def test_real_qt_network_recovers_after_rate_limit(self):
  import threading
  from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
  from PySide6.QtCore import QEventLoop,QTimer
  from PySide6.QtGui import QImage
  from PySide6.QtCore import QBuffer,QIODevice
  image=QImage(12,16,QImage.Format.Format_RGB32);image.fill(0xff326496);buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly);image.save(buffer,'PNG');payload=bytes(buffer.data());requests=[]
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):
    requests.append(self.headers.get('User-Agent',''))
    if len(requests)==1:self.send_response(429);self.send_header('Retry-After','0');self.end_headers();return
    self.send_response(200);self.send_header('Content-Type','image/png');self.end_headers();self.wfile.write(payload)
   def log_message(self,*args):pass
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  manager=desktop.ImageManager();label=QLabel();url=f'http://127.0.0.1:{server.server_port}/image.png';manager.load(url,label,QSize(60,80));loop=QEventLoop();QTimer.singleShot(4200,loop.quit);loop.exec()
  try:self.assertEqual(len(requests),2);self.assertIn('YingKu/2.8.2',requests[0]);self.assertIn(url,manager.cache);self.assertEqual(manager.active_downloads,0)
  finally:server.shutdown();server.server_close();label.deleteLater();manager.deleteLater();self.qt.processEvents()
 def test_shutdown_cancels_inflight_images_without_late_callbacks(self):
  import gc,threading
  from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
  from PySide6.QtCore import QCoreApplication,QEvent,QEventLoop,QTimer
  release=threading.Event()
  class Handler(BaseHTTPRequestHandler):
   def do_GET(self):release.wait(3);self.send_response(200);self.end_headers()
   def log_message(self,*args):pass
  server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
  owner=QWidget();manager=desktop.ImageManager(owner);label=QLabel(owner);manager.load(f'http://127.0.0.1:{server.server_port}/image',label,QSize(50,50));loop=QEventLoop();QTimer.singleShot(700,loop.quit);loop.exec()
  try:
   self.assertEqual(manager.active_downloads,1);manager.shutdown();self.assertEqual(len(manager.replies),0);self.assertEqual(manager.active_downloads,0)
   manager._request('http://127.0.0.1/unwanted',label,QSize(50,50),True,False,False);self.assertFalse(manager.download_queue)
   owner.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);gc.collect();self.qt.processEvents()
  finally:release.set();server.shutdown();server.server_close()
