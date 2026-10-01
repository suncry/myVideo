"""Exercise normal startup with tasks enabled, using synthetic isolated data only."""
import json,os,sys,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QThread,QTimer
from PySide6.QtWidgets import QApplication
import app as core,desktop,privacy

base=Path(os.environ['YINGKU_DATA_DIR']).resolve()
assert 'yingku-startup-' in str(base)
assert os.environ.get('YINGKU_DISABLE_STARTUP_TASKS')!='1'
scenario=sys.argv[1];target=Path(sys.argv[2]);sys.argv=sys.argv[:1]
privacy.configure(base,scenario=="private");core.init_db();privacy.stamp_library()
with core.connect() as c:
    c.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',[('hide_files_after_import','false'),('auto_match_after_scan','false'),('min_duration_minutes','0')])
    if scenario=='scan':
        media=base.parent/'media';media.mkdir();(media/'demo.mp4').write_bytes(b'synthetic movie')
        c.execute('INSERT INTO scan_roots(path,enabled) VALUES(?,1)',(str(media),))
        core.probe_video_duration=lambda path:720
if scenario=='failure':
    def fail():raise RuntimeError('simulated worker failure')
    core.enrich_actor_avatars=fail
core.DATA_DIR=base;core.DB_PATH=base/'film_library.db'

auth_calls=[]
class FakeBridge:
    def set_parent_window(self,parent):pass
    def yingku_auth_begin(self):
        auth_calls.append(any(w.isVisible() and w.windowTitle()=="影库" for w in QApplication.topLevelWidgets()))
        return 0
    def yingku_auth_status(self):return 1 if scenario=="private" else -1
    def yingku_auth_cancel(self):pass
# Only this standalone test injects a native result. Production has no bypass.
privacy._load_auth_bridge=lambda:FakeBridge()

class ProbeWindow(desktop.MainWindow):
    def __init__(self):
        self.probe_results=[];self.probe_errors=[];self.probe_threads=[]
        super().__init__()
        QTimer.singleShot(4500,self.finish_probe)
    def run_task(self,fn,on_result=None,on_error=None):
        def result(value):
            self.probe_results.append(fn.__name__)
            self.probe_threads.append(QThread.currentThread()==QApplication.instance().thread())
            if on_result:on_result(value)
        def error(message):
            self.probe_errors.append(message)
            self.probe_threads.append(QThread.currentThread()==QApplication.instance().thread())
            if on_error:on_error(message)
        super().run_task(fn,result,error)
    def finish_probe(self):
        with core.connect() as c:
            count=c.execute('SELECT COUNT(*) FROM movies').fetchone()[0]
            integrity=c.execute('PRAGMA integrity_check').fetchone()[0]
        success=bool(self.probe_threads) and all(self.probe_threads) and self.tasks.pending_count==0 and integrity=='ok'
        success=success and auth_calls==[True] and privacy.MODE==('private' if scenario=='private' else 'public')
        if scenario=='scan':success=success and count==1
        if scenario=='failure':success=success and bool(self.probe_errors)
        target.write_text(json.dumps(dict(scenario=scenario,success=success,mode=privacy.MODE,auth_host_visible=auth_calls,callbacks_main_thread=self.probe_threads,results=self.probe_results,errors=self.probe_errors,movies=count,integrity=integrity),ensure_ascii=False,indent=2))
        self.close();QApplication.instance().exit(0 if success else 1)
desktop.iina_cleanup.request_recent_menu_access=lambda: True
desktop.cleanup_iina_on_exit=lambda window: None  # This fixture must never clear real user player history.
desktop.MainWindow=ProbeWindow
desktop.main()
