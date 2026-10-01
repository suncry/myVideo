import os,sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer,Qt,QPointF,QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication
import app as core,desktop,privacy
assert os.environ['YINGKU_DATA_DIR'].startswith('/tmp/yingku-')
privacy.configure(core.DATA_DIR,False)
app=QApplication([]);app.setStyle('Fusion');app.setStyleSheet(desktop.STYLE)
w=desktop.MainWindow();w.resize(1420,880);w.show()
w.show_movie(w.cards[0].movie['id'])
w.detail_title.setText('山海之间的漫长旅途：那些沿着海岸线寻找旧日记忆的人们，以及一封迟到了很多年的信 · 完整标题展示示例')
w.detail_poster.setFixedHeight(250)
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
report={}
def capture(name):
    w.grab().save(str(out/(name+'.png')))
    report[name]={'cover_height':w.detail_poster.height(),'title_height':w.detail_title.height(),'expanded':w.detail_title.expanded,'visible_text':w.detail_title.label.text()}
def collapsed():
    capture('单行标题');w.detail_title.toggle.click();QTimer.singleShot(600,expanded)
def expanded():
    capture('展开完整标题');w.detail_title.toggle.click()
    cover=w.detail_poster
    for kind,ly,gy,button,buttons in [(QEvent.Type.MouseButtonPress,240,500,Qt.MouseButton.LeftButton,Qt.MouseButton.LeftButton),(QEvent.Type.MouseMove,360,620,Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton),(QEvent.Type.MouseButtonRelease,360,620,Qt.MouseButton.LeftButton,Qt.MouseButton.NoButton)]:
        QApplication.sendEvent(cover,QMouseEvent(kind,QPointF(200,ly),QPointF(500,gy),button,buttons,Qt.KeyboardModifier.NoModifier))
    QTimer.singleShot(600,resized)
def resized():
    capture('拖动后的封面');report['saved_height']=w.ui_settings.value('detailHeroHeight_landscape',type=int)
    w.close();(out/'验证结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));app.quit()
QTimer.singleShot(700,collapsed)
app.exec()
