"""Capture only the synthetic fixture window, never the user's desktop or library."""
import os,sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import app as core,desktop,privacy
assert os.environ['YINGKU_DATA_DIR'].startswith('/tmp/yingku-')
privacy.configure(core.DATA_DIR,False)
app=QApplication([]);app.setStyle('Fusion');app.setStyleSheet(desktop.STYLE)
w=desktop.MainWindow();w.show()
if w.cards:w.show_movie(w.cards[0].movie['id'])
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
steps=[('常规',1420,880),('较低窗口',1280,600),('竖屏',900,1200),('全屏',0,0)]
report=[]
def next_step():
    if not steps:
        w.showNormal();w.close();(out/'尺寸验证.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));app.quit();return
    name,width,height=steps.pop(0)
    if width:w.resize(width,height)
    else:w.toggle_fullscreen()
    def capture():
        w.grab().save(str(out/(name+'.png')))
        report.append(dict(name=name,width=w.width(),height=w.height(),fullscreen=w.isFullScreen(),requested=[width,height]))
        QTimer.singleShot(100,next_step)
    QTimer.singleShot(1400,capture)
QTimer.singleShot(500,next_step)
app.exec()
