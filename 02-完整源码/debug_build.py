import sys,os,tempfile,traceback
sys.path.insert(0,'.')
from pathlib import Path
tmp=Path(tempfile.mkdtemp())
os.environ.update(QT_QPA_PLATFORM='offscreen:configfile=tests/offscreen-screen.json',YINGKU_DATA_DIR=str(tmp/'lib'),YINGKU_SETTINGS_PATH=str(tmp/'ui.ini'),YINGKU_DISABLE_STARTUP_TASKS='1')
from PySide6.QtWidgets import QApplication
app=QApplication([])
import app as core,privacy,desktop
privacy.configure(tmp/'lib',True)
core.init_db()
try:
    w=desktop.MainWindow()
    print('OK')
except SystemExit:
    print('SystemExit (app quit)')
except Exception:
    traceback.print_exc()
app.quit()
