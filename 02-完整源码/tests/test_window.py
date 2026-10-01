import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton
import app as core
import desktop
import privacy

class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='yingku-window-')
        path=Path(self.temp.name)
        self.patches=[mock.patch.object(core,'DATA_DIR',path),mock.patch.object(core,'DB_PATH',path/'film_library.db'),mock.patch.object(privacy,'MODE','public'),mock.patch.dict(os.environ,{'YINGKU_SETTINGS_PATH':str(path/'ui.ini'),'YINGKU_DISABLE_STARTUP_TASKS':'1'})]
        for p in self.patches:p.start()
        self.window=desktop.MainWindow();self.window.show();self.qt.processEvents()
    def tearDown(self):
        self.window.close();self.window.deleteLater();self.qt.processEvents()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_title_mouse_and_keyboard_use_original_flip(self):
        with mock.patch.object(self.window,'begin_flip') as flip:
            QTest.mouseClick(self.window.brand,Qt.MouseButton.LeftButton)
            flip.assert_called_once_with(True)
            flip.reset_mock()
            self.window.brand.setFocus()
            QTest.keyClick(self.window.brand,Qt.Key.Key_Space)
            flip.assert_called_once_with(True)
        self.assertFalse(any('翻转' in b.text() for b in self.window.findChildren(QPushButton)))
        self.assertFalse(hasattr(self.window,'mode_label'))
    def test_window_can_shrink_and_grow_vertically(self):
        for width,height in [(1280,600),(900,480),(1420,960),(900,1200),(1280,600)]:
            self.window.resize(width,height);self.qt.processEvents()
            self.assertEqual((self.window.width(),self.window.height()),(width,height))
            self.assertTrue(self.window.brand.isVisible())
    def test_fullscreen_toggle_returns_to_previous_normal_geometry(self):
        self.window.resize(1280,600);self.qt.processEvents()
        original=self.window.size()
        self.window.toggle_fullscreen();self.qt.processEvents()
        self.assertTrue(self.window.isFullScreen())
        self.assertEqual(self.window.fullscreen_action.text(),'退出全屏')
        self.window.toggle_fullscreen();self.qt.processEvents()
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(self.window.size(),original)
    def test_short_window_preserves_access_to_sidebar_and_content(self):
        self.window.actor_scroll.show()
        self.window.resize(1000,480);self.qt.processEvents()
        bar=self.window.sidebar_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(),0)
        bar.setValue(bar.maximum())
        self.assertGreater(self.window.library_scroll.verticalScrollBar().maximum(),0)
        if sys.platform=='darwin':
            self.assertTrue(self.window.windowFlags() & Qt.WindowType.WindowFullscreenButtonHint)
        else:
            self.assertTrue(self.window.fullscreen_action.isEnabled())
