"""Lazy thumbnail rails and an in-app, uncropped image viewer."""
from PySide6.QtCore import Qt,QTimer,QPoint
from PySide6.QtGui import QShortcut,QKeySequence
from PySide6.QtWidgets import QDialog,QLabel,QPushButton,QWidget,QVBoxLayout,QHBoxLayout,QSizePolicy
from scrolling import NativeScrollArea,set_background


class ThumbnailRail(NativeScrollArea):
    def __init__(self,parent=None):
        super().__init__(parent,horizontal_only=True);self.entries=[];self.setWidgetResizable(False)
        set_background(self.viewport(),'#101115')
        self.horizontalScrollBar().valueChanged.connect(self.load_visible)
    def add_image(self,manager,label,source,fallback,crop=True,focus=False):
        self.entries.append([manager,label,source,fallback,crop,focus,False])
    def load_visible(self):
        visible=self.viewport().rect().adjusted(-160,0,160,0)
        for entry in self.entries:
            manager,label,source,fallback,crop,focus,loaded=entry
            if loaded:continue
            pos=label.mapTo(self.viewport(),QPoint(0,0))
            rect=label.rect().translated(pos)
            if not visible.intersects(rect):continue
            entry[-1]=True;manager.load(source,label,label.size(),fallback,crop,focus,True)
    def resizeEvent(self,event):
        super().resizeEvent(event);QTimer.singleShot(0,self.load_visible)
    def showEvent(self,event):
        super().showEvent(event);QTimer.singleShot(0,self.load_visible)


class ImageViewer(QDialog):
    def __init__(self,owner,items,index=0,*,title='',set_avatar=None):
        super().__init__(owner);self.owner=owner;self.items=items;self.index=max(0,min(index,len(items)-1));self.set_avatar=set_avatar
        self.setWindowTitle(title or '图片预览');self.resize(980,800);self.setMinimumSize(620,440);set_background(self,'#101115')
        layout=QVBoxLayout(self);layout.setContentsMargins(22,16,22,18);layout.setSpacing(12)
        header=QHBoxLayout();self.caption=QLabel();self.caption.setTextFormat(Qt.TextFormat.PlainText);self.caption.setWordWrap(True);self.caption.setObjectName('actorWorkTitle');self.caption.setMaximumHeight(50);self.caption.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred);header.addWidget(self.caption,1)
        close=QPushButton('关闭');close.clicked.connect(self.close);header.addWidget(close);layout.addLayout(header)
        self.image=QLabel();self.image.setAlignment(Qt.AlignmentFlag.AlignCenter);self.image.setMinimumSize(1,1);self.image.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Ignored);set_background(self.image,'#090a0b');layout.addWidget(self.image,1)
        self.credit=QLabel();self.credit.setTextFormat(Qt.TextFormat.PlainText);self.credit.setObjectName('mutedSmall');self.credit.setWordWrap(True);self.credit.setMaximumHeight(45);layout.addWidget(self.credit)
        actions=QHBoxLayout();self.previous=QPushButton('上一张');self.previous.clicked.connect(lambda:self.navigate(-1));actions.addWidget(self.previous);self.position=QLabel();self.position.setAlignment(Qt.AlignmentFlag.AlignCenter);actions.addWidget(self.position);self.next=QPushButton('下一张');self.next.clicked.connect(lambda:self.navigate(1));actions.addWidget(self.next);actions.addStretch()
        self.source=QPushButton('查看图片来源');self.source.clicked.connect(self.open_source);actions.addWidget(self.source)
        self.avatar=QPushButton('设为演员头像');self.avatar.setVisible(set_avatar is not None);self.avatar.clicked.connect(self.choose_avatar);actions.addWidget(self.avatar);layout.addLayout(actions)
        for key,step in [(Qt.Key.Key_Left,-1),(Qt.Key.Key_Right,1)]:
            shortcut=QShortcut(QKeySequence(key),self);shortcut.activated.connect(lambda s=step:self.navigate(s))
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.timeout.connect(self.render);self.render()
    def render(self):
        if not self.items:return
        item=self.items[self.index];self.caption.setText(str(item.get('caption') or self.windowTitle()))
        self.position.setText(f'{self.index+1} / {len(self.items)}');self.previous.setEnabled(self.index>0);self.next.setEnabled(self.index<len(self.items)-1)
        self.source.setEnabled(bool(item.get('source_url')));self.avatar.setText('已设为头像' if item.get('selected') else '设为演员头像')
        self.credit.setText(' · '.join(str(item[k]) for k in ['source','credit','license'] if item.get(k)))
        self.owner.images.load(item.get('photo_url') or item.get('url',''),self.image,self.image.size(),'图',False)
    def navigate(self,step):self.index=max(0,min(self.index+step,len(self.items)-1));self.render()
    def open_source(self):
        from insights_ui import open_source
        open_source(self.items[self.index].get('source_url',''))
    def choose_avatar(self):
        if self.set_avatar:
            self.set_avatar(self.items[self.index]['photo_url'])
            for index,item in enumerate(self.items):item['selected']=index==self.index
            self.avatar.setText('已设为头像')
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'debounce'):self.debounce.start(120)
    def showEvent(self,event):super().showEvent(event);QTimer.singleShot(0,self.render)
