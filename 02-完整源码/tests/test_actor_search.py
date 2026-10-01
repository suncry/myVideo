import os, tempfile, unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import app as core, actor_search, desktop, privacy

class SearchTemplateTests(unittest.TestCase):
    def test_names_stay_a_single_parameter(self):
        for name in ['张译', 'Tom Hanks', 'A&B /?#=+%李']:
            url=actor_search.search_url('https://example.com/find?q=<name>&type=person', name)
            self.assertEqual(parse_qs(urlsplit(url).query), {'q':[name], 'type':['person']})
            self.assertEqual(urlsplit(url).fragment, '')
    def test_custom_path_and_multiple_placeholders(self):
        self.assertEqual(actor_search.search_url('https://example.com/<name>?q=<name>', 'A/B'), 'https://example.com/A%2FB?q=A%2FB')
    def test_invalid_templates(self):
        for template in ['https://example.com/', 'file:///<name>', 'javascript:<name>', 'https://<name>.com/', 'https://example.com:bad/?q=<name>', 'https://example.com/ q=<name>', 'https://example.com/?q=<NAME>']:
            with self.subTest(template=template), self.assertRaises(ValueError):
                actor_search.search_url(template, '张译')

class ActorSearchUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='yingku-search-');path=Path(self.temp.name)
        self.patches=[mock.patch.object(core,'DATA_DIR',path),mock.patch.object(core,'DB_PATH',path/'film_library.db'),mock.patch.object(privacy,'MODE','public'),mock.patch.dict(os.environ,{'YINGKU_DISABLE_STARTUP_TASKS':'1','YINGKU_SETTINGS_PATH':str(path/'ui.ini')})]
        for p in self.patches:p.start()
        core.init_db()
    def tearDown(self):
        self.qt.processEvents()
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def test_settings_persist_cancel_and_invalid_input(self):
        d=desktop.SettingsDialog();d.search_template.setText('https://example.com/?query=<name>');d.save();d.close()
        restored=desktop.SettingsDialog()
        self.assertEqual(restored.search_template.text(),'https://example.com/?query=<name>')
        restored.search_template.setText('https://example.org/')
        self.assertFalse(restored.save_button.isEnabled());restored.accept()
        self.assertNotEqual(restored.result(),desktop.QDialog.DialogCode.Accepted)
        restored.reject()
        self.assertEqual(actor_search.load_template(),'https://example.com/?query=<name>')
        d.deleteLater();restored.deleteLater()
    def test_save_button_commits_before_accepting_dialog(self):
        d=desktop.SettingsDialog();d.show()
        d.search_template.setText('https://example.org/find?q=<name>')
        QTest.mouseClick(d.save_button,Qt.MouseButton.LeftButton)
        self.assertEqual(d.result(),desktop.QDialog.DialogCode.Accepted)
        self.assertEqual(actor_search.load_template(),'https://example.org/find?q=<name>')
        reopened=desktop.SettingsDialog()
        self.assertEqual(reopened.search_template.text(),'https://example.org/find?q=<name>')
        d.deleteLater();reopened.deleteLater()

    def test_button_uses_display_name_and_latest_template_without_filtering(self):
        window=desktop.MainWindow()
        chip=desktop.ActorChip({'name':'Original Name','display_name':'张译','count':1,'favorite_count':0},window.images)
        chip.search_requested.connect(window.search_actor)
        selected=[];chip.clicked.connect(selected.append);chip.show();self.qt.processEvents()
        with mock.patch.object(desktop.QDesktopServices,'openUrl',return_value=True) as opened:
            QTest.mouseClick(chip.search_button,Qt.MouseButton.LeftButton)
            self.assertEqual(parse_qs(urlsplit(opened.call_args.args[0].toString()).query)['wd'],['张译'])
            actor_search.save_template('https://example.com/people?q=<name>')
            QTest.mouseClick(chip.search_button,Qt.MouseButton.LeftButton)
            self.assertEqual(opened.call_args.args[0].host(),'example.com')
            self.assertEqual(parse_qs(urlsplit(opened.call_args.args[0].toString()).query)['q'],['张译'])
        self.assertEqual(selected,[])
        window.close();window.deleteLater();chip.close();chip.deleteLater()
