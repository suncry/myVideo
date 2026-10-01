"""Render the existing SVG into a Windows ICO; no image library dependency."""
import struct
from pathlib import Path
from PySide6.QtCore import QBuffer,QIODevice,Qt
from PySide6.QtGui import QImage,QPainter
from PySide6.QtSvg import QSvgRenderer


def build():
    root=Path(__file__).resolve().parents[1];renderer=QSvgRenderer(str(root/'assets/app-icon.svg'));images=[]
    for size in (16,32,48,64,128,256):
        image=QImage(size,size,QImage.Format.Format_ARGB32);image.fill(Qt.GlobalColor.transparent)
        painter=QPainter(image);renderer.render(painter);painter.end();buffer=QBuffer();buffer.open(QIODevice.OpenModeFlag.WriteOnly);assert image.save(buffer,'PNG');images.append((size,bytes(buffer.data())))
    offset=6+16*len(images);entries=[]
    for size,data in images:
        entries.append(struct.pack('<BBBBHHII',size if size<256 else 0,size if size<256 else 0,0,0,1,32,len(data),offset));offset+=len(data)
    (root/'assets/app-icon.ico').write_bytes(struct.pack('<HHH',0,1,len(images))+b''.join(entries)+b''.join(data for size,data in images))


if __name__=='__main__':build()
