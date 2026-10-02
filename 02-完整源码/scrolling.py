"""Native macOS overlay scrollbars and nested horizontal gesture routing."""
import sys
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPainter, QColor, QPalette, QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QProxyStyle, QScrollArea, QScrollBar, QStyle, QStyleFactory, QStyleOptionSlider


def set_background(widget, color):
    # Unscoped background QSS cascades into scrollbar rules and disables macOS overlays.
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(color))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


def forward_wheel(widget, event):
    ancestor = widget.parentWidget()
    while ancestor is not None and not isinstance(ancestor, QScrollArea):
        ancestor = ancestor.parentWidget()
    if ancestor is None:
        event.ignore()
        return
    position = ancestor.viewport().mapFromGlobal(event.globalPosition().toPoint())
    forwarded = QWheelEvent(QPointF(position), event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                            event.buttons(), event.modifiers(), event.phase(), event.inverted())
    QApplication.sendEvent(ancestor.viewport(), forwarded)
    event.accept()


class OverlayScrollStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_ScrollBar_Transient:
            return 0 if sys.platform == 'win32' else 1
        return super().styleHint(hint, option, widget, returnData)

    def pixelMetric(self, metric, option=None, widget=None):
        if sys.platform=='win32' and metric==QStyle.PixelMetric.PM_ScrollBarExtent:return 14
        if metric == QStyle.PixelMetric.PM_ScrollView_ScrollBarOverlap:
            return 0 if sys.platform == 'win32' else self.pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent, option, widget)
        return super().pixelMetric(metric, option, widget)


class NativeOverlayScrollBar(QScrollBar):
    def __init__(self, orientation, style, parent=None):
        super().__init__(orientation, parent)
        self.native_style = style
        self.setStyle(style)
        self.setAutoFillBackground(False)

    def paintEvent(self, event):
        # App-wide color QSS would otherwise replace macOS drawing with a boxy
        # stylesheet scrollbar. Draw through the native style directly.
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        painter = QPainter(self)
        self.native_style.drawComplexControl(QStyle.ComplexControl.CC_ScrollBar, option, painter, self)


class NativeScrollArea(QScrollArea):
    def __init__(self, parent=None, horizontal_only=False):
        super().__init__(parent)
        self.horizontal_only = horizontal_only
        if sys.platform in ('darwin','win32'):
            app = QApplication.instance()
            if not hasattr(app, '_yingku_overlay_style'):
                base=QStyleFactory.create('macOS' if sys.platform=='darwin' else 'WindowsVista') or QStyleFactory.create('Fusion')
                style = OverlayScrollStyle(base)
                style.setParent(app)
                app._yingku_overlay_style = style
            self.setHorizontalScrollBar(NativeOverlayScrollBar(Qt.Orientation.Horizontal, app._yingku_overlay_style, self))
            self.setVerticalScrollBar(NativeOverlayScrollBar(Qt.Orientation.Vertical, app._yingku_overlay_style, self))

    def wheelEvent(self, event):
        delta = event.pixelDelta() if not event.pixelDelta().isNull() else event.angleDelta()
        if self.horizontal_only:
            if sys.platform == 'win32':
                # On Windows, vertical wheel scrolls the actor strip horizontally so users can scroll without Shift.
                if abs(delta.y()) >= abs(delta.x()) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    bar = self.horizontalScrollBar()
                    bar.setValue(bar.value() - delta.y())
                    event.accept()
                    return
            else:
                if abs(delta.y()) >= abs(delta.x()) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    forward_wheel(self, event)
                    return
        super().wheelEvent(event)


class ScrollSafeComboBox(QComboBox):
    def wheelEvent(self, event):
        # Scrolling over a frozen filter moves the page, not the selection.
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            forward_wheel(self, event)
