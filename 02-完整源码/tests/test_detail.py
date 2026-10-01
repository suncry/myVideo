import os,tempfile,unittest
from pathlib import Path
from unittest import mock
from PySide6.QtCore import Qt,QPointF,QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QVBoxLayout,QWidget
import app as core, desktop, privacy

class DetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def test_title_elides_expands_and_collapses_without_widening(self):
        box=QWidget();layout=QVBoxLayout(box);title=desktop.ExpandableTitle();layout.addWidget(title)
        box.resize(400,100);box.show();self.qt.processEvents()
        text='这是一段很长的影片标题 · '+'山川与旅途 '*30
        title.setText(text);self.qt.processEvents()
        before=box.width();collapsed=title.height()
        self.assertFalse(title.label.wordWrap());self.assertTrue(title.toggle.isVisible())
        self.assertIn('…',title.label.text());self.assertEqual(title.full_text,text)
        QTest.mouseClick(title.toggle,Qt.MouseButton.LeftButton);self.qt.processEvents()
        self.assertTrue(title.expanded);self.assertEqual(title.label.text(),text)
        self.assertGreater(title.height(),collapsed);self.assertEqual(box.width(),before)
        QTest.mouseClick(title.toggle,Qt.MouseButton.LeftButton);self.qt.processEvents()
        self.assertFalse(title.expanded);self.assertFalse(title.label.wordWrap())
        title.setText('短标题');self.qt.processEvents();self.assertFalse(title.toggle.isVisible())
        box.close();box.deleteLater()
    def test_title_treats_markup_as_text_and_collapses_new_movie(self):
        title=desktop.ExpandableTitle();title.resize(220,70);title.show()
        text='<b>真实片名</b>\n第二行 '+'长片名'*50
        title.setText(text);self.qt.processEvents();title.toggle_expanded()
        self.assertEqual(title.label.textFormat(),Qt.TextFormat.PlainText)
        title.setText(text,reset=True)
        self.assertFalse(title.expanded);self.assertNotIn('\n',title.label.text())
        title.close();title.deleteLater()
    def test_drag_changes_cover_height_and_commits_on_release(self):
        cover=desktop.ResizableHeroLabel();cover.resize(400,250);cover.show()
        committed=[];cover.height_committed.connect(committed.append)
        def event(kind,local_y,global_y,button,buttons):
            e=QMouseEvent(kind,QPointF(200,local_y),QPointF(500,global_y),button,buttons,Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(cover,e)
        event(QEvent.Type.MouseButtonPress,240,500,Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton)
        event(QEvent.Type.MouseMove,350,610,Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton)
        self.assertEqual(cover.height(),360);self.assertEqual(committed,[])
        event(QEvent.Type.MouseButtonRelease,350,610,Qt.MouseButton.LeftButton,Qt.MouseButton.NoButton)
        self.assertEqual(committed,[360])
        QTest.keyClick(cover,Qt.Key.Key_Up);self.assertEqual(cover.height(),350)
        cover.close();cover.deleteLater()
    def test_cover_height_survives_movie_switch_and_window_reopen(self):
        with tempfile.TemporaryDirectory(prefix='yingku-detail-') as tmp:
            path=Path(tmp)
            with mock.patch.object(core,'DATA_DIR',path),mock.patch.object(core,'DB_PATH',path/'film_library.db'),mock.patch.object(privacy,'MODE',None),mock.patch.dict(os.environ,{'YINGKU_SETTINGS_PATH':str(path/'layout.ini'),'YINGKU_DISABLE_STARTUP_TASKS':'1'}):
                core.init_db()
                with core.connect() as c:
                    for i in range(2):
                        c.execute('INSERT INTO movies(path,filename,title,created_at,updated_at) VALUES(?,?,?,?,?)',(f'/example/{i}.mp4',f'{i}.mp4','标题'*50,str(i),str(i)))
                window=desktop.MainWindow();window.resize(1400,800);window.show();self.qt.processEvents()
                with mock.patch.object(window,'ensure_current_screenshots'):
                    window.show_movie(1);window.detail_poster.setFixedHeight(430)
                    window.detail_poster.height_committed.emit(430)
                    self.assertEqual(window.ui_settings.value('detailHeroHeight_landscape',type=int),430)
                    window.detail_title.toggle_expanded();window.show_movie(2)
                    self.assertFalse(window.detail_title.expanded)
                    self.assertEqual(window.detail_poster.height(),430)
                window.close();window.deleteLater();self.qt.processEvents()
                restored=desktop.MainWindow();self.assertEqual(restored.detail_poster.height(),430)
                restored.close();restored.deleteLater();self.qt.processEvents()
