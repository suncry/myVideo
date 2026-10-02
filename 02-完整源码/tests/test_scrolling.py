import json,os,tempfile,unittest
from pathlib import Path
from unittest import mock
from PySide6.QtCore import QPoint,QPointF,Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication,QLabel,QStyle
from PySide6.QtTest import QTest
import app as core,desktop,privacy

class ScrollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='yingku-scrolling-test-');path=Path(self.temp.name)
        self.patches=[mock.patch.object(core,'DATA_DIR',path),mock.patch.object(core,'DB_PATH',path/'film_library.db'),mock.patch.object(privacy,'MODE','public'),mock.patch.dict(os.environ,{'YINGKU_SETTINGS_PATH':str(path/'ui.ini'),'YINGKU_DISABLE_STARTUP_TASKS':'1'})]
        for p in self.patches:p.start()
        core.init_db()
        with core.connect() as c:
            for i in range(60):
                c.execute('INSERT INTO movies(path,filename,title,cast_json,created_at,updated_at) VALUES(?,?,?,?,?,?)',(f'/example/{i}.mp4',f'{i}.mp4',f'影片{i}',json.dumps([{'name':f'演员{i%5}'}],ensure_ascii=False),'2026','2026'))
        self.w=desktop.MainWindow();self.w.resize(1440,900);self.w.show();QTest.qWait(50)
    def tearDown(self):
        self.w.close();self.w.deleteLater();self.qt.processEvents()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_one_scroll_moves_entire_panel_and_pins_same_filters(self):
        w=self.w;bar=w.library_scroll.verticalScrollBar()
        self.assertIs(w.scroll,w.library_scroll)
        y=w.library_panel.y();bar.setValue(600);self.qt.processEvents()
        self.assertLess(w.library_panel.y(),y)
        self.assertTrue(w.is_sticky)
        self.assertEqual(w.sticky_header.y(),0)
        self.assertIs(w.filter_bar.parentWidget(),w.sticky_header)
        for chip in w.compact_actor_chips:
            self.assertEqual(chip.avatar.size().width(),48)
        bar.setValue(0);self.qt.processEvents()
        self.assertFalse(w.is_sticky)
        self.assertIs(w.filter_bar.parentWidget(),w.filter_slot)
    def test_compact_actor_filters_and_resets_using_same_state(self):
        w=self.w;w.library_scroll.verticalScrollBar().setValue(600);self.qt.processEvents()
        chip=w.compact_actor_chips[0];name=chip.actor_name
        QTest.mouseClick(chip.avatar,Qt.MouseButton.LeftButton);self.qt.processEvents()
        self.assertEqual(w.actor_filter,name)
        self.assertEqual(len(w.cards),12)
        self.assertEqual(sum(bool(c.property('selected')) for c in w.compact_actor_chips),1)
        w.clear_actor_filter();self.qt.processEvents()
        self.assertEqual(len(w.cards),60)
    def test_overlay_scrollbars_do_not_reserve_a_gutter(self):
        w=self.w
        w.library_scroll.verticalScrollBar().setValue(600)
        self.qt.processEvents()
        import sys
        transient=1 if sys.platform=='darwin' else 0
        for area in [w.actor_scroll,w.library_scroll,w.compact_actor_scroll]:
            style=area.verticalScrollBar().style()
            self.assertEqual(style.styleHint(QStyle.StyleHint.SH_ScrollBar_Transient,None,area.verticalScrollBar()),transient)
    def test_vertical_wheel_over_actor_strip_scrolls_library(self):
        import sys
        area=self.w.actor_scroll;pos=QPoint(30,30);global_pos=area.viewport().mapToGlobal(pos)
        event=QWheelEvent(QPointF(pos),QPointF(global_pos),QPoint(0,-160),QPoint(0,-120),Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.ScrollUpdate,False)
        QApplication.sendEvent(area.viewport(),event);self.qt.processEvents()
        if sys.platform=='win32':
            # On Windows the vertical wheel scrolls the actor strip horizontally.
            self.assertGreater(area.horizontalScrollBar().value(),0)
        else:
            self.assertGreater(self.w.library_scroll.verticalScrollBar().value(),0)
            self.assertEqual(area.horizontalScrollBar().value(),0)
    def test_empty_filtered_result_keeps_compact_context_until_scrolled_back(self):
        w=self.w;bar=w.library_scroll.verticalScrollBar()
        bar.setValue(600);self.qt.processEvents()
        w.favorite_filter_combo.setCurrentIndex(1);self.qt.processEvents()
        self.assertEqual(len(w.cards),0)
        self.assertTrue(w.is_sticky)
        self.assertTrue(w.filter_bar.isVisible())
        bar.setValue(0);self.qt.processEvents()
        self.assertFalse(w.is_sticky)

    def test_resizing_pinned_header_does_not_detach_filters(self):
        self.w.library_scroll.verticalScrollBar().setValue(600);self.qt.processEvents()
        for size in [(1200,750),(900,480),(1400,950)]:
            self.w.resize(*size);QTest.qWait(30)
            self.assertEqual(self.w.sticky_header.width(),self.w.library_scroll.viewport().width())
            self.assertIs(self.w.filter_bar.parentWidget(),self.w.sticky_header)

    def test_reflow_keeps_single_row_indicator(self):
        w=self.w
        for _ in range(3):
            w.reflow_filters();self.qt.processEvents()
        indicators=[label for label in w.filter_bar.findChildren(QLabel) if label.objectName()=='rowIndicator']
        self.assertEqual(len(indicators),1)
        self.assertIs(indicators[0],w.row_indicator)

    def test_actor_strip_wheel_glides_smoothly_to_target(self):
        import sys
        if sys.platform!='win32':return
        area=self.w.actor_scroll;bar=area.horizontalScrollBar()
        if bar.maximum()<24:self.skipTest('actor strip fits without overflow')
        target=max(bar.minimum(),min(bar.maximum(),bar.value()+160))
        pos=QPoint(30,30);global_pos=area.viewport().mapToGlobal(pos)
        event=QWheelEvent(QPointF(pos),QPointF(global_pos),QPoint(0,-160),QPoint(0,-120),Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.ScrollUpdate,False)
        QApplication.sendEvent(area.viewport(),event);self.qt.processEvents()
        self.assertGreater(bar.value(),0)
        self.assertLess(bar.value(),target)
        QTest.qWait(700)
        self.assertEqual(bar.value(),target)

    def test_expanded_actor_grid_wheel_scrolls_the_page(self):
        import sys
        if sys.platform!='win32':return
        w=self.w
        w.actors_expanded=True;w.reflow_actor_chips();self.qt.processEvents()
        self.assertEqual(w.actor_scroll.horizontalScrollBar().maximum(),0)
        area=w.actor_scroll;pos=QPoint(30,30);global_pos=area.viewport().mapToGlobal(pos)
        event=QWheelEvent(QPointF(pos),QPointF(global_pos),QPoint(0,-160),QPoint(0,-120),Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.ScrollUpdate,False)
        QApplication.sendEvent(area.viewport(),event);self.qt.processEvents()
        self.assertGreater(w.library_scroll.verticalScrollBar().value(),0)
        self.assertEqual(area.horizontalScrollBar().value(),0)

    def test_actor_nav_button_matches_other_menu_icons(self):
        for button in self.w.nav_group.buttons():
            if button.property('viewKey')=='actors':
                text=button.text()
                self.assertTrue(text.endswith('演员'))
                self.assertFalse(text.startswith('演员'))
                return
        self.fail('actors nav button missing')
