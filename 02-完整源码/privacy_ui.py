"""Batch local-library partitioning; never reads online catalogs."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLabel,QLineEdit,QListWidget,QListWidgetItem,QPushButton,QMessageBox
import app as core
import privacy

class PartitionDialog(QDialog):
    def __init__(self,parent):
        super().__init__(parent)
        self.changed=False
        self.pending=[]
        self.completed=0
        self.setWindowTitle('批量整理分区')
        self.resize(620,560)
        layout=QVBoxLayout(self)
        self.target='普通' if privacy.MODE=='private' else '私密'
        hint=QLabel(f'勾选要移入{self.target}模式的本地影片。收藏、评分和备注一起保留，视频原文件位置不变。')
        hint.setWordWrap(True);layout.addWidget(hint)
        self.search=QLineEdit();self.search.setPlaceholderText('筛选片名…');layout.addWidget(self.search)
        self.list=QListWidget();layout.addWidget(self.list)
        with core.connect() as c:
            for row in c.execute("SELECT id,title FROM movies WHERE file_status<>'trashed' ORDER BY title"):
                item=QListWidgetItem(row['title']);item.setData(Qt.ItemDataRole.UserRole,row['id'])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Unchecked)
                self.list.addItem(item)
        self.search.textChanged.connect(self.filter)
        buttons=QHBoxLayout();layout.addLayout(buttons)
        select=QPushButton('全选筛选结果');select.clicked.connect(self.select_visible);buttons.addWidget(select)
        clear=QPushButton('清空勾选');clear.clicked.connect(self.clear_checked);buttons.addWidget(clear)
        self.move=QPushButton(f'移入{self.target}模式');self.move.clicked.connect(self.apply);buttons.addWidget(self.move)
        self.status=QLabel('只显示当前模式的影片。');layout.addWidget(self.status)
        close=QPushButton('关闭');close.clicked.connect(self.reject);layout.addWidget(close)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.next_movie)

    def filter(self,text):
        for i in range(self.list.count()):
            item=self.list.item(i);item.setHidden(text.casefold() not in item.text().casefold())

    def select_visible(self):
        for i in range(self.list.count()):
            item=self.list.item(i)
            if not item.isHidden():item.setCheckState(Qt.CheckState.Checked)

    def clear_checked(self):
        for i in range(self.list.count()):self.list.item(i).setCheckState(Qt.CheckState.Unchecked)

    def apply(self):
        self.pending=[self.list.item(i) for i in range(self.list.count()) if self.list.item(i).checkState()==Qt.CheckState.Checked]
        if not self.pending:return
        if privacy.MODE=='public' and not privacy.authenticate(self):return
        if self.parent().privacy_locking:return
        if QMessageBox.question(self,'确认移动',f'将勾选的 {len(self.pending)} 部影片移入{self.target}模式？')!=QMessageBox.StandardButton.Yes:return
        self.move.setEnabled(False);self.list.setEnabled(False);self.completed=0
        self.timer.start(0)

    def next_movie(self):
        if self.parent().privacy_locking or not self.isVisible():
            self.pending=[];return
        if not self.pending:
            self.move.setEnabled(True);self.list.setEnabled(True);return
        item=self.pending.pop(0)
        try:
            privacy.move_movie(item.data(Qt.ItemDataRole.UserRole))
        except Exception as exc:
            self.pending=[];self.move.setEnabled(True);self.list.setEnabled(True)
            self.status.setText(f'已移动 {self.completed} 部；其余保持原分区。{exc}')
            return
        self.changed=True;self.completed+=1
        self.list.takeItem(self.list.row(item))
        self.status.setText(f'已移动 {self.completed} 部到{self.target}模式。')
        self.timer.start(10)

    def reject(self):
        self.timer.stop();self.pending=[]
        super().reject()
