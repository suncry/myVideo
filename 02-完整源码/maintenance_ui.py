"""Native dialogs for reviewable local maintenance actions."""
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QHeaderView)
import app as core
import maintenance as ops
from background_tasks import TaskRunner


class MaintenanceDialog(QDialog):
    restore_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('备份与文件关联')
        self.resize(560, 410)
        self.changed = False
        self.busy = False
        self.tasks = TaskRunner(self)
        layout = QVBoxLayout(self)
        title = QLabel('保护你的整理成果')
        title.setObjectName('dialogTitle')
        layout.addWidget(title)
        lead = QLabel('完整备份评分、喜欢、演员资料、头像、截图与窗口布局。影片原文件留在原来的硬盘。')
        lead.setWordWrap(True)
        layout.addWidget(lead)
        self.buttons = []
        for text, action in [('创建完整备份…', self.backup), ('从备份恢复…', self.restore),
                             ('重新关联影片文件夹…', self.relink)]:
            button = QPushButton(text)
            button.clicked.connect(action)
            self.buttons.append(button)
            layout.addWidget(button)
        self.status = QLabel('备份保存在你选择的位置；恢复前会自动备份当前数据。')
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0,0)
        self.progress.hide()
        layout.addWidget(self.progress)
        layout.addStretch()
        self.close_button = QPushButton('关闭')
        self.close_button.clicked.connect(self.reject)
        layout.addWidget(self.close_button)

    def reject(self):
        if not self.busy:
            super().reject()

    def set_busy(self, value):
        self.busy = value
        for button in self.buttons + [self.close_button]:
            button.setEnabled(not value)
        self.progress.setVisible(value)

    def job(self, operation, on_result):
        self.set_busy(True)
        def finished(result):
            self.set_busy(False)
            on_result(result)
        def failed(message):
            self.set_busy(False)
            self.status.setText('操作未完成：' + message)
            QMessageBox.warning(self, '影库', message)
        self.tasks.start(operation, finished, failed)

    def backup(self):
        folder = QFileDialog.getExistingDirectory(self, '选择备份保存位置')
        if folder:
            self.status.setText('正在备份数据库与缓存，请稍候…')
            self.job(lambda: ops.create_backup(folder), lambda path: self.status.setText('完整备份已保存：\n' + path))

    def restore(self):
        folder = QFileDialog.getExistingDirectory(self, '选择包含“影库备份.json”的备份文件夹')
        if folder:
            self.status.setText('正在核对备份完整性…')
            self.job(lambda: ops.inspect_backup(folder), self.confirm_restore)

    def confirm_restore(self, info):
        message = (f"备份时间：{info['created_at']}\n影片记录：{info['movies']}\n\n"
                   '恢复会替换当前影库资料，并自动备份当前数据。影片原文件不会移动或删除。\n'
                   '影库将关闭并重新打开；恢复后先核对扫描源，再手工扫描。继续恢复？')
        if QMessageBox.question(self,'恢复预览',message,
             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
             QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Yes:
            self.restore_requested.emit(info['folder'])
            self.accept()

    def relink(self):
        dialog = RelinkDialog(self)
        dialog.exec()
        self.changed = self.changed or dialog.changed
        if dialog.changed:
            self.status.setText(dialog.summary)


class RelinkDialog(MaintenanceDialog):
    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle('重新关联影片文件夹')
        self.resize(980, 610)
        self.changed = False
        self.busy = False
        self.tasks = TaskRunner(self)
        self.plan = None
        self.summary = ''
        layout = QVBoxLayout(self)
        lead = QLabel('先选择旧扫描源与新位置，再预览关联。评分、喜欢、观看记录与演员资料随原记录保留；影片文件不移动。')
        lead.setWordWrap(True)
        layout.addWidget(lead)
        self.old_root = QComboBox()
        for source in core.list_scan_roots():
            self.old_root.addItem(source['path'], source['path'])
        layout.addWidget(QLabel('原扫描源（离线来源也可选择）'))
        layout.addWidget(self.old_root)
        row = QHBoxLayout()
        self.new_root = QLineEdit()
        self.new_root.setReadOnly(True)
        self.new_root.setPlaceholderText('选择影片现在所在的文件夹')
        choose = QPushButton('选择新位置…')
        choose.clicked.connect(self.choose_new_root)
        row.addWidget(self.new_root,1)
        row.addWidget(choose)
        layout.addLayout(row)
        preview = QPushButton('检查并预览关联')
        preview.clicked.connect(self.preview)
        layout.addWidget(preview)
        self.table = QTableWidget(0,4)
        self.table.setHorizontalHeaderLabels(['关联 / 状态','影片','旧位置','新位置'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table,1)
        self.status = QLabel('仅大小与文件指纹一致的唯一候选可以确认；冲突项会保留原记录。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0,0)
        self.progress.hide()
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.close_button = QPushButton('关闭')
        self.close_button.clicked.connect(self.reject)
        apply = QPushButton('确认关联勾选项')
        apply.setObjectName('primary')
        apply.clicked.connect(self.apply)
        actions.addStretch()
        actions.addWidget(self.close_button)
        actions.addWidget(apply)
        layout.addLayout(actions)
        self.buttons = [preview,choose,apply,self.old_root]
        self.old_root.currentIndexChanged.connect(self.invalidate_plan)

    def invalidate_plan(self):
        self.plan = None
        self.table.setRowCount(0)

    def choose_new_root(self):
        folder = QFileDialog.getExistingDirectory(self, '选择影片现在所在的文件夹')
        if folder:
            self.new_root.setText(folder)
            self.invalidate_plan()

    def preview(self):
        old, new = self.old_root.currentData(), self.new_root.text()
        if not old or not new:
            QMessageBox.information(self,'请选择文件夹','请先选择原扫描源与新位置。')
            return
        self.status.setText('正在核对文件大小与指纹，大型硬盘可能需要一些时间…')
        self.job(lambda: ops.plan_relink(old,new), self.show_plan)

    def show_plan(self, plan):
        self.plan = plan
        self.table.setRowCount(len(plan['items']))
        count = 0
        for i,item in enumerate(plan['items']):
            status = QTableWidgetItem(item['status'])
            if item['new_path']:
                status.setFlags(status.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                status.setCheckState(Qt.CheckState.Checked)
                count += 1
            self.table.setItem(i,0,status)
            for j,key in enumerate(('title','old_path','new_path'),1):
                cell = QTableWidgetItem(item[key])
                cell.setToolTip(item[key])
                self.table.setItem(i,j,cell)
        self.status.setText(f"检查 {len(plan['items'])} 条记录，可关联 {count} 条。确认后先自动备份，再更新关联。")

    def apply(self):
        if not self.plan:
            return
        selected = [item['id'] for i,item in enumerate(self.plan['items'])
                    if item['new_path'] and self.table.item(i,0).checkState() == Qt.CheckState.Checked]
        if not selected:
            QMessageBox.information(self,'没有选中影片','请勾选至少一条可以关联的记录。')
            return
        plan = self.plan
        def operation():
            backup = ops.create_backup(core.DATA_DIR.parent / 'YingKu-关联前备份')
            count = ops.apply_relink(plan,selected)
            return count,backup
        self.status.setText('正在备份当前数据并再次核对文件…')
        self.job(operation,self.applied)

    def applied(self, result):
        count,backup = result
        self.changed = True
        self.summary = f'已关联 {count} 部影片，个人整理已保留。\n关联前备份：{backup}'
        self.status.setText(self.summary)
        self.invalidate_plan()
