"""macOS overlay scrollbars; Windows always-visible standard scrollbars with QSS styling."""
import sys
from PySide6.QtCore import Qt, QPointF, QEasingCurve, QVariantAnimation
from PySide6.QtGui import QPainter, QColor, QPalette, QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QProxyStyle, QScrollArea, QScrollBar, QStyle, QStyleFactory, QStyleOptionSlider


def set_background(widget, color):
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
            return 0
        return super().styleHint(hint, option, widget, returnData)


class NativeOverlayScrollBar(QScrollBar):
    def __init__(self, orientation, style, parent=None):
        super().__init__(orientation, parent)
        self.native_style = style
        self.setStyle(style)
        self.setAutoFillBackground(False)

    def paintEvent(self, event):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        painter = QPainter(self)
        self.native_style.drawComplexControl(QStyle.ComplexControl.CC_ScrollBar, option, painter, self)


class NativeScrollArea(QScrollArea):
    def __init__(self, parent=None, horizontal_only=False):
        super().__init__(parent)
        self.horizontal_only = horizontal_only
        if sys.platform == 'win32':
            # Windows: standard scrollbars, always visible, styled by global QSS.
            self.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOn if horizontal_only else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            self.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff if horizontal_only else Qt.ScrollBarPolicy.ScrollBarAlwaysOn
            )
        elif sys.platform == 'darwin':
            app = QApplication.instance()
            if not hasattr(app, '_yingku_overlay_style'):
                base = QStyleFactory.create('macOS') or QStyleFactory.create('Fusion')
                style = OverlayScrollStyle(base)
                style.setParent(app)
                app._yingku_overlay_style = style
            self.setHorizontalScrollBar(NativeOverlayScrollBar(Qt.Orientation.Horizontal, app._yingku_overlay_style, self))
            self.setVerticalScrollBar(NativeOverlayScrollBar(Qt.Orientation.Vertical, app._yingku_overlay_style, self))

    def smooth_scroll_to(self, bar, target):
        animation = getattr(self, "_scroll_animation", None)
        if animation is None:
            animation = QVariantAnimation(self)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.valueChanged.connect(lambda value: bar.setValue(int(value)))
            bar.sliderPressed.connect(animation.stop)
            self._scroll_animation = animation
        animation.stop()
        start = bar.value()
        if abs(target - start) <= 1:
            bar.setValue(target)
            return
        # Jump a quarter of the distance at once so the strip answers the wheel
        # immediately, then glide the remaining distance with easing.
        instant = start + (target - start) // 4
        bar.setValue(instant)
        animation.setStartValue(instant)
        animation.setEndValue(target)
        animation.setDuration(max(140, min(420, abs(target - instant) * 3)))
        animation.start()

    def wheelEvent(self, event):
        delta = event.pixelDelta() if not event.pixelDelta().isNull() else event.angleDelta()
        if self.horizontal_only:
            if sys.platform == 'win32':
                # Windows: vertical wheel scrolls horizontally so users can browse actors without Shift.
                if abs(delta.y()) >= abs(delta.x()) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    bar = self.horizontalScrollBar()
                    if bar.maximum() <= bar.minimum():
                        # Nothing to scroll sideways (the expanded actor grid
                        # fits its columns): hand the wheel to the page so the
                        # strip never turns into a dead zone.
                        forward_wheel(self, event)
                        return
                    target = max(bar.minimum(), min(bar.maximum(), bar.value() - delta.y()))
                    self.smooth_scroll_to(bar, target)
                    event.accept()
                    return
            else:
                if abs(delta.y()) >= abs(delta.x()) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    forward_wheel(self, event)
                    return
        super().wheelEvent(event)


class ScrollSafeComboBox(QComboBox):
    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            forward_wheel(self, event)
