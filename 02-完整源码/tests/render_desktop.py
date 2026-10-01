"""Render the native Qt window offscreen for visual QA."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from desktop import MainWindow, STYLE


def main() -> int:
    output = Path(__file__).resolve().parents[1] / "artifacts" / "desktop-preview.png"
    output.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.resize(1420, 880)
    window.show()
    if window.cards:
        window.show_movie(window.cards[0].movie["id"])

    def render() -> None:
        window.grab().save(str(output), "PNG")
        print(output)
        window.close()
        app.quit()

    QTimer.singleShot(1200, render)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
