"""Convert the unchanged original SVG icon into the macOS icon container."""
from pathlib import Path
import subprocess
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

root = Path(__file__).resolve().parents[1]
icons = root / 'build' / 'app-icon.iconset'
icons.mkdir(parents=True, exist_ok=True)
renderer = QSvgRenderer(str(root / 'assets' / 'app-icon.svg'))
for logical in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        size = logical * scale
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        suffix = '@2x' if scale == 2 else ''
        image.save(str(icons / f'icon_{logical}x{logical}{suffix}.png'))
subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(icons), '-o', str(root / 'assets' / 'app-icon.icns')], check=True)
