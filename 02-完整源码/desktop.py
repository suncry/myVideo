from __future__ import annotations

import json
from collections import deque
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QLockFile, QObject, QSettings, QSize, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QIcon, QImage, QKeySequence, QPainter,
    QPainterPath, QPixmap, QShortcut,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

import app as core
import privacy
import actor_search
import player
import iina_cleanup
import discovery
from process_utils import process_alive
from insights_ui import ActorLibrary, KeywordStrip
from scrolling import NativeScrollArea, ScrollSafeComboBox, set_background
from background_tasks import TaskRunner

UI_FONT = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI"
HERO_MIN_HEIGHT = 120
HERO_MAX_HEIGHT = 900


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative


def poster_source(movie: dict[str, Any]) -> str:
    local = movie.get("local_poster", "")
    if local and Path(local).is_file():
        return local
    return movie.get("poster_url", "") or ""


def actor_alias_names(name: str) -> list[str]:
    target = core.normalize_actor_name(name)
    with core.connect() as conn:
        profiles = conn.execute("SELECT name_key,name,aliases_json FROM actor_profiles").fetchall()
    for profile in profiles:
        aliases = core.unique_actor_names([profile["name"], *core.json_value(profile["aliases_json"], [])])
        if target == profile["name_key"] or any(core.normalize_actor_name(alias) == target for alias in aliases):
            return aliases
    return [name]


def query_movie_page(
    view: str = "all", query: str = "", sort: str = "updated", limit: int = 240,
    actor: str = "", favorite_filter: str = "any", rating_filter: str = "any", offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    where = ["exists_now=1"]
    args: list[Any] = []
    if query:
        where.append("(title LIKE ? OR original_title LIKE ? OR filename LIKE ? OR notes LIKE ? OR tags LIKE ? OR cast_json LIKE ?)")
        args.extend([f"%{query}%"] * 6)
    filters = {
        "favorite": "favorite=1",
        "unwatched": "watch_status='unwatched'",
        "unmatched": "match_status IN ('unmatched','review','no_match')",
        "duplicates": "fingerprint IN (SELECT fingerprint FROM movies WHERE fingerprint<>'' GROUP BY fingerprint HAVING COUNT(*)>1)",
        "delete": "disposition='delete'",
        "review": "disposition='review'",
    }
    if view in filters:
        where.append(filters[view])
    if actor:
        actor_names = actor_alias_names(actor)
        where.append("(" + " OR ".join("cast_json LIKE ?" for _ in actor_names) + ")")
        args.extend(f"%{name}%" for name in actor_names)
    favorite_filters = {"liked": "favorite=1", "not_liked": "favorite=0"}
    if favorite_filter in favorite_filters:
        where.append(favorite_filters[favorite_filter])
    rating_filters = {
        "unrated": "personal_rating<=0", "low": "personal_rating BETWEEN 1 AND 4",
        "mid": "personal_rating BETWEEN 5 AND 7", "high": "personal_rating>=8",
    }
    if rating_filter in rating_filters:
        where.append(rating_filters[rating_filter])
    ordering = {
        "updated": "updated_at DESC", "title": "title COLLATE NOCASE", "year": "year DESC",
        "rating": "personal_rating DESC, updated_at DESC", "size": "file_size DESC",
    }.get(sort, "updated_at DESC")
    with core.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM movies WHERE {' AND '.join(where)}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM movies WHERE {' AND '.join(where)} ORDER BY {ordering},id DESC LIMIT ? OFFSET ?", (*args, limit, offset)
        ).fetchall()
    result = [core.movie_dict(row) for row in rows]
    for movie in result:
        movie["poster"] = poster_source(movie)
    return result, total


def query_movies(
    view: str = "all", query: str = "", sort: str = "updated", limit: int = 240,
    actor: str = "", favorite_filter: str = "any", rating_filter: str = "any", offset: int = 0,
) -> list[dict[str, Any]]:
    return query_movie_page(view, query, sort, limit, actor, favorite_filter, rating_filter, offset)[0]


def get_movie(movie_id: int) -> dict[str, Any] | None:
    with core.connect() as conn:
        row = conn.execute("SELECT * FROM movies WHERE id=?", (movie_id,)).fetchone()
    if not row:
        return None
    movie = core.movie_dict(row)
    movie["poster"] = poster_source(movie)
    return movie


def update_movie(movie_id: int, **fields: Any) -> None:
    allowed = {
        "title", "original_title", "year", "overview", "personal_rating", "favorite",
        "watch_status", "disposition", "notes", "tags", "genres", "local_poster", "cast_json",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    for key in ("tags", "genres"):
        if key in values:
            values[key] = json.dumps(values[key], ensure_ascii=False)
    if "cast_json" in values and not isinstance(values["cast_json"], str):
        values["cast_json"] = json.dumps(values["cast_json"], ensure_ascii=False)
    if "favorite" in values:
        values["favorite"] = 1 if values["favorite"] else 0
    if not values:
        return
    values["updated_at"] = core.now_iso()
    with core.connect() as conn:
        assignment = ",".join(f"{key}=?" for key in values)
        conn.execute(f"UPDATE movies SET {assignment} WHERE id=?", (*values.values(), movie_id))


def update_screenshot_status(movie_id: int, status: str) -> None:
    with core.connect() as conn:
        conn.execute(
            "UPDATE movies SET screenshots_status=?,screenshots_attempted_at=? WHERE id=?",
            (status, core.now_iso(), movie_id),
        )


def record_movie_play(movie_id: int) -> int:
    with core.connect() as conn:
        conn.execute(
            """UPDATE movies SET play_count=COALESCE(play_count,0)+1,last_played_at=?,
            watch_status=CASE WHEN watch_status='unwatched' THEN 'watching' ELSE watch_status END,
            updated_at=? WHERE id=?""",
            (core.now_iso(), core.now_iso(), movie_id),
        )
        row = conn.execute("SELECT play_count FROM movies WHERE id=?", (movie_id,)).fetchone()
    return int(row["play_count"] or 0) if row else 0


def format_duration(seconds: float | int) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return "未知"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours} 小时 {minutes} 分" if hours else f"{minutes} 分 {secs} 秒"


def dashboard_stats() -> dict[str, Any]:
    with core.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) total, COALESCE(SUM(file_size),0) bytes,
            COALESCE(SUM(CASE WHEN favorite=1 THEN 1 ELSE 0 END),0) favorites,
            COALESCE(SUM(CASE WHEN match_status IN ('unmatched','review','no_match') THEN 1 ELSE 0 END),0) unmatched,
            COALESCE(SUM(CASE WHEN disposition='delete' THEN 1 ELSE 0 END),0) delete_count
            FROM movies WHERE exists_now=1"""
        ).fetchone()
        duplicates = conn.execute(
            "SELECT COALESCE(SUM(c-1),0) n FROM (SELECT COUNT(*) c FROM movies WHERE fingerprint<>'' AND exists_now=1 GROUP BY fingerprint HAVING c>1)"
        ).fetchone()["n"]
    return {**dict(row), "duplicates": duplicates, "size_label": core.human_size(row["bytes"])}


def query_actor_facets() -> list[dict[str, Any]]:
    with core.connect() as conn:
        rows = conn.execute("SELECT cast_json,favorite FROM movies WHERE exists_now=1 AND cast_json<>'[]'").fetchall()
        profile_rows = [dict(row) for row in conn.execute(
            "SELECT name_key,name,display_name,avatar_url,aliases_json,info_json,source FROM actor_profiles"
        )]
    profiles = {row["name_key"]: row for row in profile_rows}
    profile_aliases: dict[str, dict[str, Any]] = {}
    for profile in profile_rows:
        for alias in core.unique_actor_names([profile["name"], *core.json_value(profile["aliases_json"], [])]):
            profile_aliases[core.normalize_actor_name(alias)] = profile
    actors: dict[str, dict[str, Any]] = {}
    for row in rows:
        seen: set[str] = set()
        for person in core.json_value(row["cast_json"], []):
            name = str(person.get("name") or "").strip()
            raw_key = core.normalize_actor_name(name)
            profile = profile_aliases.get(raw_key, {})
            key = str(profile.get("name_key") or raw_key)
            if not name or key in seen:
                continue
            seen.add(key)
            actor = actors.setdefault(key, {
                "name": profile.get("name") or name, "display_name": profile.get("display_name") or name,
                "avatar": profile.get("avatar_url") or "", "count": 0, "favorite_count": 0,
                "aliases": core.json_value(profile.get("aliases_json"), []),
                "info": core.json_value(profile.get("info_json"), {}), "source": profile.get("source") or "",
            })
            actor["count"] += 1
            actor["favorite_count"] += 1 if row["favorite"] else 0
            actor["avatar"] = actor["avatar"] or str(person.get("avatar") or "")
    return sorted(actors.values(), key=lambda item: (-item["count"], item["name"].casefold()))


def actor_display_name_map() -> dict[str, str]:
    with core.connect() as conn:
        rows = conn.execute(
            "SELECT name_key,name,display_name,aliases_json FROM actor_profiles WHERE display_name<>''"
        ).fetchall()
    result: dict[str, str] = {}
    for row in rows:
        for alias in core.unique_actor_names([row["name"], *core.json_value(row["aliases_json"], [])]):
            result[core.normalize_actor_name(alias)] = row["display_name"]
    return result


class ImageManager(QObject):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.cache: dict[str, QPixmap] = {}
        self.download_queue=deque();self.active_downloads=0;self.replies=set();self.stopped=False
        self.download_timer=QTimer(self);self.download_timer.setInterval(350);self.download_timer.timeout.connect(self._pump_downloads)

    @staticmethod
    def disk_cache_path(source: str) -> Path:
        return core.DATA_DIR / "image-cache" / (hashlib.sha256(source.encode("utf-8")).hexdigest() + ".img")

    @staticmethod
    def placeholder(text: str, size: QSize, dark: bool = True) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(QColor("#2a2b2f" if dark else "#ddd8cf"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#d56a57" if dark else "#8a8379"))
        painter.setFont(QFont(UI_FONT, max(14, size.width() // 5), QFont.Weight.DemiBold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, (text or "影")[:1])
        painter.end()
        return pixmap

    def load(
        self, source: str, label: QLabel, size: QSize, fallback: str = "影",
        crop: bool = True, focus_top: bool = False, rounded: bool = False,
    ) -> None:
        label.setProperty("imageSource", source)
        placeholder = self.placeholder(fallback, size, dark=True)
        self._set(label, placeholder, size, True, focus_top, rounded)
        if not source:
            return
        if source in self.cache:
            self._set(label, self.cache[source], size, crop, focus_top, rounded)
            return
        local = Path(source)
        if local.is_file():
            pixmap = QPixmap(str(local))
            if not pixmap.isNull():
                self.cache[source] = pixmap
                self._set(label, pixmap, size, crop, focus_top, rounded)
            return
        if not source.startswith(("http://", "https://")):
            return
        disk_cache = self.disk_cache_path(source)
        if disk_cache.is_file():
            pixmap = QPixmap(str(disk_cache))
            if not pixmap.isNull():
                self.cache[source] = pixmap
                self._set(label, pixmap, size, crop, focus_top, rounded)
                return
        self._request(source,label,size,crop,focus_top,rounded)

    def _request(self,source,label,size,crop,focus_top,rounded,attempt=0):
        if self.stopped:return
        self.download_queue.append((source,label,size,crop,focus_top,rounded,attempt))
        if not self.download_timer.isActive():self.download_timer.start()

    def _pump_downloads(self):
        if self.active_downloads>=2:return
        while self.download_queue:
            source,label,size,crop,focus_top,rounded,attempt=self.download_queue.popleft()
            try:
                if label.property('imageSource')!=source:continue
                if source in self.cache:
                    self._set(label,self.cache[source],size,crop,focus_top,rounded);continue
            except RuntimeError:continue
            request=QNetworkRequest(QUrl(source))
            request.setRawHeader(b'User-Agent',b'YingKu/2.8.2 (local desktop media library)')
            request.setTransferTimeout(15000)
            self.active_downloads+=1;reply=self.network.get(request);self.replies.add(reply)
            reply.finished.connect(lambda r=reply,s=source,l=label,z=size,c=crop,f=focus_top,o=rounded,a=attempt:self._finished(r,s,l,z,c,f,o,a))
            break
        if not self.download_queue:self.download_timer.stop()

    def _finished(
        self, reply: QNetworkReply, source: str, label: QLabel, size: QSize,
        crop: bool, focus_top: bool, rounded: bool, attempt: int = 0,
    ) -> None:
        self.replies.discard(reply)
        reply.finished.disconnect()
        self.active_downloads=max(0,self.active_downloads-1)
        status=reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if not attempt and status in (429,500,502,503,504):
            try:
                try:header=reply.rawHeader('Retry-After')
                except TypeError:header=reply.rawHeader(b'Retry-After')
                delay=int(bytes(header).decode() or '8')
            except (ValueError,TypeError):delay=8
            reply.readAll();reply.deleteLater()
            if delay<=120:QTimer.singleShot(max(2,delay)*1000,self,lambda:self._request(source,label,size,crop,focus_top,rounded,1))
            return
        data = reply.readAll()
        reply.deleteLater()
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.cache[source] = pixmap
            try:
                cache_path = self.disk_cache_path(source)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(bytes(data))
            except OSError:
                pass
            try:
                if label.property("imageSource") != source:
                    return
                self._set(label, pixmap, size, crop, focus_top, rounded)
            except RuntimeError:
                pass

    def shutdown(self):
        self.stopped=True;self.download_timer.stop();self.download_queue.clear()
        for reply in tuple(self.replies):
            reply.finished.disconnect();reply.abort();reply.deleteLater()
        self.replies.clear();self.active_downloads=0

    @staticmethod
    def _set(
        label: QLabel, pixmap: QPixmap, size: QSize, crop: bool,
        focus_top: bool = False, rounded: bool = False,
    ) -> None:
        if not crop:
            rendered = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            label.setPixmap(rendered)
            return
        scaled = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        x = max(0, (scaled.width() - size.width()) // 2)
        overflow = max(0, scaled.height() - size.height())
        y = int(overflow * (0.18 if focus_top else 0.5))
        rendered = scaled.copy(x, y, size.width(), size.height())
        if rounded:
            output = QPixmap(size)
            output.fill(Qt.GlobalColor.transparent)
            painter = QPainter(output)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(0, 0, size.width(), size.height(), 14, 14)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, rendered)
            painter.end()
            rendered = output
        label.setPixmap(rendered)


class StarRating(QWidget):
    changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.value = 0
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.stars: list[QToolButton] = []
        for value in range(1, 6):
            button = QToolButton()
            button.setText("★")
            button.setObjectName("ratingStar")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, score=value: self.set_rating(score, True))
            layout.addWidget(button)
            self.stars.append(button)
        layout.addStretch()

    def set_rating(self, value: int, emit: bool = False) -> None:
        self.value = max(0, min(5, int(value)))
        for index, button in enumerate(self.stars, 1):
            button.setProperty("active", index <= self.value)
            button.style().unpolish(button)
            button.style().polish(button)
        if emit:
            self.changed.emit(self.value)


class PosterLabel(QLabel):
    clicked = Signal()
    right_clicked = Signal(object)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        elif event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit(event.globalPosition().toPoint())
        super().mousePressEvent(event)


class ExpandableTitle(QWidget):
    """Elide the title without allowing its natural width to stretch the detail pane."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.expanded = False
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(8)
        self.label = QLabel()
        self.label.setObjectName("detailTitle")
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setMinimumWidth(0)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.row.addWidget(self.label, 1)
        self.toggle = QToolButton()
        self.toggle.setObjectName("titleToggle")
        self.toggle.setText("展开")
        self.toggle.setAccessibleName("展开完整标题")
        self.toggle.clicked.connect(self.toggle_expanded)
        self.row.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        self.toggle.hide()

    def setText(self, text, reset=False):
        if text != self.full_text or reset:
            self.expanded = False
        self.full_text = str(text or "")
        self.label.setToolTip(self.full_text)
        self.label.setAccessibleName(self.full_text)
        self.refresh()

    def toggle_expanded(self):
        self.expanded = not self.expanded
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def refresh(self):
        single_line = " ".join(self.full_text.split())
        metrics = self.label.fontMetrics()
        overflows = metrics.horizontalAdvance(single_line) > max(1, self.width())
        self.toggle.setVisible(self.expanded or overflows)
        self.toggle.setText("收起" if self.expanded else "展开")
        self.toggle.setAccessibleName("收起完整标题" if self.expanded else "展开完整标题")
        self.label.setWordWrap(self.expanded)
        reserve = self.toggle.sizeHint().width() + self.row.spacing() if self.expanded or overflows else 0
        width = max(1, self.width() - reserve)
        if self.expanded:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
            self.label.setMinimumHeight(0)
            self.label.setMaximumHeight(16777215)
            self.label.setText(self.full_text)
        else:
            self.label.setText(metrics.elidedText(single_line, Qt.TextElideMode.ElideRight, width))
            self.label.setFixedHeight(metrics.height() + 6)
            self.setFixedHeight(max(metrics.height() + 6, self.toggle.sizeHint().height() if overflows else 0))
        self.updateGeometry()


class ResizableHeroLabel(QLabel):
    height_committed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("影片封面高度")
        self.setToolTip("拖动封面底边调整高度，松手后自动记住；也可用上下方向键微调。")
        self._dragging = False
        self._drag_origin_y = 0.0
        self._drag_origin_height = 0

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 16, 20, 185))
        painter.drawRoundedRect((self.width() - 64) // 2, self.height() - 21, 64, 17, 8, 8)
        painter.setBrush(QColor("#f0ece5"))
        painter.drawRoundedRect((self.width() - 34) // 2, self.height() - 14, 34, 3, 1.5, 1.5)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            delta = 10 if event.key() == Qt.Key.Key_Down else -10
            self.setFixedHeight(max(HERO_MIN_HEIGHT, min(HERO_MAX_HEIGHT, self.height() + delta)))
            self.height_committed.emit(self.height())
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() >= self.height() - 24:
            self._dragging = True
            self._drag_origin_y = event.globalPosition().y()
            self._drag_origin_height = self.height()
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._dragging:
            height = self._drag_origin_height + int(event.globalPosition().y() - self._drag_origin_y)
            self.setFixedHeight(max(HERO_MIN_HEIGHT, min(HERO_MAX_HEIGHT, height)))
            event.accept()
            return
        self.setCursor(
            Qt.CursorShape.SizeVerCursor if event.position().y() >= self.height() - 24
            else Qt.CursorShape.ArrowCursor
        )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._dragging:
            self._dragging = False
            self.height_committed.emit(self.height())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: Any) -> None:
        if not self._dragging:
            self.unsetCursor()
        super().leaveEvent(event)


class ScreenshotLabel(QLabel):
    clicked = Signal(str)

    def __init__(self, source: str = "") -> None:
        super().__init__()
        self.source = source
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.source:
            self.clicked.emit(self.source)
        super().mousePressEvent(event)


class PreviewDialog(QDialog):
    def __init__(
        self, sources: list[str], source: str, images: ImageManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.sources = list(dict.fromkeys(item for item in sources if item))
        if source and source not in self.sources:
            self.sources.append(source)
        self.index = self.sources.index(source) if source in self.sources else 0
        self.images = images
        self.setWindowTitle("关键截图预览")
        self.resize(1100, 700)
        layout = QVBoxLayout(self)
        viewer = QHBoxLayout()
        self.previous_button = QToolButton()
        self.previous_button.setText("‹")
        self.previous_button.setObjectName("previewArrow")
        self.previous_button.setToolTip("上一张（←）")
        self.previous_button.setFixedSize(54, 96)
        self.previous_button.clicked.connect(self.previous_image)
        viewer.addWidget(self.previous_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(760, 520)
        viewer.addWidget(self.image, 1)
        self.next_button = QToolButton()
        self.next_button.setText("›")
        self.next_button.setObjectName("previewArrow")
        self.next_button.setToolTip("下一张（→）")
        self.next_button.setFixedSize(54, 96)
        self.next_button.clicked.connect(self.next_image)
        viewer.addWidget(self.next_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(viewer, 1)
        self.previous_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Left), self)
        self.previous_shortcut.activated.connect(self.previous_image)
        self.next_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Right), self)
        self.next_shortcut.activated.connect(self.next_image)
        footer = QHBoxLayout()
        self.counter = QLabel()
        self.counter.setObjectName("previewCounter")
        footer.addWidget(self.counter)
        footer.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.show_current()

    def show_current(self) -> None:
        total = len(self.sources)
        if not total:
            self.image.setText("没有可预览的截图")
            self.previous_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.counter.setText("0 / 0")
            return
        self.index = max(0, min(self.index, total - 1))
        self.images.load(
            self.sources[self.index], self.image, QSize(920, 600),
            str(self.index + 1), False,
        )
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < total - 1)
        self.counter.setText(f"{self.index + 1} / {total}  ·  可使用键盘 ← → 切换")

    def previous_image(self) -> None:
        if self.index > 0:
            self.index -= 1
            self.show_current()

    def next_image(self) -> None:
        if self.index + 1 < len(self.sources):
            self.index += 1
            self.show_current()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Left:
            self.previous_image()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.next_image()
            event.accept()
            return
        super().keyPressEvent(event)


def image_average_hash(path: Path) -> int | None:
    image = QImage(str(path))
    if image.isNull():
        return None
    sample = image.convertToFormat(QImage.Format.Format_Grayscale8).scaled(
        8, 8, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    values = [sample.pixelColor(x, y).red() for y in range(8) for x in range(8)]
    mean = sum(values) / len(values)
    result = 0
    for index, value in enumerate(values):
        if value >= mean:
            result |= 1 << index
    return result


def extract_keyframes(path: str, output_dir: Path) -> list[str]:
    """Extract up to ten distinct frames without relying on system codecs."""
    from imageio_ffmpeg import get_ffmpeg_exe

    output_dir.mkdir(parents=True, exist_ok=True)
    for old in output_dir.glob("frame_*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    ffmpeg = get_ffmpeg_exe()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path], capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=flags, timeout=25,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
    if not match:
        raise RuntimeError("无法读取影片时长，可能是文件损坏或格式不受支持。")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise RuntimeError("影片时长无效。")
    files: list[str] = []
    hashes: list[int] = []
    fractions = (0.05, 0.13, 0.21, 0.29, 0.37, 0.45, 0.53, 0.61, 0.69, 0.77, 0.85, 0.93)
    for candidate_index, fraction in enumerate(fractions, 1):
        if len(files) >= 10:
            break
        temp_path = output_dir / f"candidate_{candidate_index:02d}.jpg"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{duration * fraction:.3f}",
            "-i", path, "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=720:-2",
            "-q:v", "3", "-an", "-sn", "-y", str(temp_path),
        ]
        try:
            result = subprocess.run(command, capture_output=True, creationflags=flags, timeout=30)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode != 0 or not temp_path.is_file():
            continue
        signature = image_average_hash(temp_path)
        if signature is None or any((signature ^ old).bit_count() < 6 for old in hashes):
            temp_path.unlink(missing_ok=True)
            continue
        final_path = output_dir / f"frame_{len(files)+1:02d}.jpg"
        temp_path.replace(final_path)
        files.append(str(final_path))
        hashes.append(signature)
    for leftover in output_dir.glob("candidate_*.jpg"):
        leftover.unlink(missing_ok=True)
    if not files:
        raise RuntimeError("未能从影片中提取画面，请检查文件是否可以正常播放。")
    return files


class MovieCard(QFrame):
    opened = Signal(int)
    favorite_changed = Signal(int, bool)
    delete_requested = Signal(int)

    def __init__(self, movie: dict[str, Any], images: ImageManager) -> None:
        super().__init__()
        self.movie = movie
        self.setObjectName("movieCard")
        self.setFixedWidth(184)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 9)
        layout.setSpacing(7)
        self.poster = PosterLabel()
        self.poster.setFixedSize(172, 248)
        self.poster.setScaledContents(False)
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setStyleSheet("border-radius:8px;background:#292a2e;")
        self.poster.setCursor(Qt.CursorShape.PointingHandCursor)
        self.poster.clicked.connect(lambda: self.opened.emit(movie["id"]))
        images.load(movie.get("poster", ""), self.poster, self.poster.size(), movie.get("title", "影"))
        layout.addWidget(self.poster)
        title = QLabel(movie.get("title") or movie.get("filename"))
        title.setObjectName("cardTitle")
        title.setToolTip(movie.get("filename", ""))
        title.setWordWrap(False)
        layout.addWidget(title)
        row = QHBoxLayout()
        meta = QLabel(f"{movie.get('year') or '年份未知'}  ·  {movie.get('size_label', '')}")
        meta.setObjectName("mutedSmall")
        row.addWidget(meta, 1)
        fav = QToolButton()
        fav.setText("♥" if movie.get("favorite") else "♡")
        fav.setObjectName("favoriteOn" if movie.get("favorite") else "favoriteOff")
        fav.clicked.connect(lambda: self.favorite_changed.emit(movie["id"], not bool(movie.get("favorite"))))
        row.addWidget(fav)
        remove = QToolButton()
        remove.setText("⌫")
        remove.setObjectName("cardDelete")
        remove.setToolTip("在影库中把影片移到 回收站")
        remove.clicked.connect(lambda: self.delete_requested.emit(movie["id"]))
        row.addWidget(remove)
        layout.addLayout(row)
        if movie.get("match_status") == "unmatched":
            self.setToolTip("资料待匹配")


class ActorChip(QFrame):
    clicked = Signal(str)
    photo_requested = Signal(str, object)
    search_requested = Signal(str)

    def __init__(self, actor: dict[str, Any], images: ImageManager, selected: bool = False) -> None:
        super().__init__()
        self.actor_name = actor["name"]
        self.setObjectName("actorChip")
        self.setProperty("selected", selected)
        self.setFixedSize(126, 180)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 6)
        layout.setSpacing(4)
        avatar = PosterLabel()
        avatar.setFixedSize(94, 94)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        avatar.setStyleSheet("border-radius:14px;background:#25262b;")
        avatar.clicked.connect(lambda: self.clicked.emit(self.actor_name))
        avatar.right_clicked.connect(lambda position: self.photo_requested.emit(self.actor_name, position))
        images.load(
            actor.get("avatar", ""), avatar, avatar.size(), actor.get("display_name") or self.actor_name,
            True, True, True,
        )
        name = QPushButton(actor.get("display_name") or self.actor_name)
        name.setObjectName("actorName")
        name.setToolTip(
            f"{actor.get('display_name') or self.actor_name} · 原名 {self.actor_name}\n"
            f"{actor['count']} 部影片，其中喜欢 {actor['favorite_count']} 部"
            + (f"\n别名：{'、'.join(actor.get('aliases', [])[:4])}" if actor.get("aliases") else "")
            + "\n右键头像或姓名可查看演员详情、收藏与照片"
        )
        name.setCheckable(True)
        name.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        name.customContextMenuRequested.connect(lambda position: self.photo_requested.emit(self.actor_name, position))
        name.setChecked(selected)
        name.clicked.connect(lambda: self.clicked.emit(self.actor_name))
        layout.addWidget(avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)
        self.search_button = QPushButton("搜索")
        self.search_button.setObjectName("actorSearch")
        self.search_button.setFixedSize(94, 27)
        self.search_button.setCursor(Qt.CursorShape.PointingHandCursor)
        search_name = actor.get("display_name") or self.actor_name
        self.search_button.setAccessibleName(f"搜索演员 {search_name}")
        self.search_button.setToolTip(f"在浏览器中搜索 {search_name} · 地址可在系统设置中修改")
        self.search_button.clicked.connect(lambda: self.search_requested.emit(search_name))
        layout.addWidget(self.search_button, 0, Qt.AlignmentFlag.AlignHCenter)
        stats = QLabel(f"影片 {actor['count']}  ·  喜欢 {actor['favorite_count']}")
        stats.setObjectName("actorStats")
        stats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(stats)


class CircularAvatar(PosterLabel):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addEllipse(1, 1, self.width() - 2, self.height() - 2)
        painter.setClipPath(clip)
        painter.fillRect(self.rect(), QColor("#25262b"))
        pixmap = self.pixmap()
        if pixmap and not pixmap.isNull():
            painter.drawPixmap(self.rect(), pixmap)
        painter.setClipping(False)
        if self.property("selected"):
            painter.setPen(QColor("#df8878"))
            painter.drawEllipse(1, 1, self.width() - 2, self.height() - 2)


class CompactActorChip(QWidget):
    clicked = Signal(str)

    def __init__(self, actor, images, selected=False):
        super().__init__()
        self.actor_name = actor["name"]
        self.setObjectName("compactActor")
        self.setProperty("selected", selected)
        self.setFixedSize(76, 73)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 1, 3, 2)
        layout.setSpacing(3)
        self.avatar = CircularAvatar()
        self.avatar.setFixedSize(48, 48)
        self.avatar.setProperty("selected", selected)
        self.avatar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.avatar.clicked.connect(lambda: self.clicked.emit(self.actor_name))
        display = actor.get("display_name") or self.actor_name
        images.load(actor.get("avatar", ""), self.avatar, QSize(48, 48), display, True, True, False)
        name = QPushButton()
        name.setObjectName("compactActorName")
        name.setText(name.fontMetrics().elidedText(display, Qt.TextElideMode.ElideRight, 70))
        name.setToolTip(display)
        name.setAccessibleName(f"筛选演员 {display}")
        name.setCheckable(True)
        name.setChecked(selected)
        name.clicked.connect(lambda: self.clicked.emit(self.actor_name))
        layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)


class ActorPhotoDialog(QDialog):
    def __init__(
        self, actor_name: str, display_name: str, photos: list[dict[str, Any]],
        images: ImageManager, profile: dict[str, Any] | None = None, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.selected_url = next((str(photo["photo_url"]) for photo in photos if photo.get("selected")), "")
        self.request_refresh = False
        self.frames: dict[str, QFrame] = {}
        self.setWindowTitle(f"选择演员照片 · {display_name or actor_name}")
        self.resize(760, 650)
        layout = QVBoxLayout(self)
        title = QLabel(display_name or actor_name)
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        profile = profile or {}
        aliases = core.json_value(profile.get("aliases_json"), [])
        info = core.json_value(profile.get("info_json"), {})
        details: list[str] = []
        if aliases:
            details.append("别名：" + "、".join(aliases[:8]))
        if info:
            details.append("  ·  ".join(f"{key}：{value}" for key, value in list(info.items())[:7]))
        if profile.get("source"):
            details.append("资料来源：" + str(profile["source"]))
        if profile.get("biography"):
            biography = str(profile["biography"]).replace("\r", " ").replace("\n", " ")
            details.append("简介：" + biography[:240] + ("…" if len(biography) > 240 else ""))
        if details:
            profile_text = QLabel("\n".join(details))
            profile_text.setObjectName("actorProfileInfo")
            profile_text.setWordWrap(True)
            profile_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(profile_text)
        hint = QLabel(f"已保存 {len(photos)} / 50 张候选照片 · 点击选择后设为头像 · 照片与选择结果保存在本机")
        hint.setObjectName("muted")
        layout.addWidget(hint)
        scroll = NativeScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        grid = QGridLayout(container)
        grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        grid.setSpacing(12)
        for index, photo in enumerate(photos[:50]):
            url = str(photo.get("photo_url") or "")
            frame = QFrame()
            frame.setObjectName("photoChoice")
            frame.setProperty("selected", bool(photo.get("selected")))
            frame.setFixedSize(128, 178)
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(6, 6, 6, 6)
            image = PosterLabel()
            image.setFixedSize(114, 142)
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            image.setCursor(Qt.CursorShape.PointingHandCursor)
            image.clicked.connect(lambda value=url: self.choose(value))
            images.load(url, image, image.size(), display_name or actor_name, True, True, True)
            source = QLabel(str(photo.get("source") or "未知来源"))
            source.setObjectName("photoSource")
            source.setAlignment(Qt.AlignmentFlag.AlignCenter)
            frame_layout.addWidget(image)
            frame_layout.addWidget(source)
            self.frames[url] = frame
            grid.addWidget(frame, index // 5, index % 5)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)
        actions = QHBoxLayout()
        refresh = QPushButton("↻ 强制重新联网查找更多照片")
        refresh.setToolTip("即使已有头像也重新匹配；只有成功找到新资料才会替换")
        refresh.clicked.connect(self.refresh_and_close)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.apply = QPushButton("设为头像")
        self.apply.setObjectName("primary")
        self.apply.setEnabled(bool(self.selected_url))
        self.apply.clicked.connect(self.accept)
        actions.addWidget(refresh)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(self.apply)
        layout.addLayout(actions)

    def choose(self, url: str) -> None:
        self.selected_url = url
        for candidate, frame in self.frames.items():
            frame.setProperty("selected", candidate == url)
            frame.style().unpolish(frame)
            frame.style().polish(frame)
        self.apply.setEnabled(True)

    def refresh_and_close(self) -> None:
        self.request_refresh = True
        self.accept()


class ScanDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.library_changed = False
        self.setWindowTitle("扫描源管理")
        self.resize(690, 590)
        layout = QVBoxLayout(self)
        title = QLabel("扫描源管理")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        lead = QLabel("添加、扫描或移除影片文件夹。文件消失后会保留整理记录，可在“备份与文件关联”中重新关联；离线硬盘不会误删资料。")
        lead.setWordWrap(True)
        lead.setObjectName("muted")
        layout.addWidget(lead)
        self.sources = QListWidget()
        self.sources.setObjectName("driveList")
        layout.addWidget(self.sources, 1)
        source_actions = QHBoxLayout()
        add = QPushButton("＋ 添加硬盘或文件夹")
        add.clicked.connect(self.choose_folder)
        remove = QPushButton("移除选中源")
        remove.setObjectName("dangerText")
        remove.clicked.connect(self.remove_selected)
        source_actions.addWidget(add)
        source_actions.addWidget(remove)
        source_actions.addStretch()
        layout.addLayout(source_actions)
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("过滤短于"))
        self.duration_threshold = QSpinBox()
        self.duration_threshold.setRange(0, 180)
        self.duration_threshold.setSuffix(" 分钟")
        try:
            threshold = int(core.setting_value("min_duration_minutes") or "10")
        except ValueError:
            threshold = 10
        self.duration_threshold.setValue(max(0, threshold))
        threshold_row.addWidget(self.duration_threshold)
        threshold_hint = QLabel("0 表示不限制；扫描与每次启动时都会复查")
        threshold_hint.setObjectName("mutedSmall")
        threshold_row.addWidget(threshold_hint)
        threshold_row.addStretch()
        layout.addLayout(threshold_row)
        self.hide_imported = QCheckBox("影片入库后在 Windows 资源管理器中隐藏（保持原来的硬盘与位置）")
        self.hide_imported.setChecked(core.setting_value("hide_files_after_import").lower() != "false")
        layout.addWidget(self.hide_imported)
        privacy_row = QHBoxLayout()
        privacy_row.addWidget(QLabel("隐藏方式"))
        self.hide_mode = QComboBox()
        self.hide_mode.addItem("标准隐藏（推荐）", False)
        if sys.platform != "darwin":
            self.hide_mode.addItem("加强隐藏（隐藏 + 系统属性）", True)
        self.hide_mode.setCurrentIndex(1 if core.setting_value("hide_files_system_attribute").lower() == "true" else 0)
        self.hide_mode.setEnabled(self.hide_imported.isChecked())
        self.hide_imported.toggled.connect(self.hide_mode.setEnabled)
        privacy_row.addWidget(self.hide_mode)
        privacy_row.addStretch()
        layout.addLayout(privacy_row)
        privacy_hint = QLabel(
            "这是原地修改 Windows 文件属性，不会移动或加密影片。影片不在扫描源根目录时，其直接所在文件夹也会隐藏，"
            "但扫描源根目录绝不隐藏。标准隐藏在资源管理器开启“隐藏的项目”后仍可看见；"
            "加强隐藏通常还需关闭“隐藏受保护的操作系统文件”才会显示。取消此选项并重新扫描，可恢复由本软件隐藏的影片。"
        )
        if sys.platform == "darwin":
            self.hide_imported.setText("影片入库后在 Finder 中隐藏（保持原来的硬盘与位置）")
            self.hide_mode.setCurrentIndex(0)
            self.hide_mode.setItemText(0, "Finder 隐藏（原地隐藏）")
            privacy_hint.setText(
                "通过 macOS 文件标记原地隐藏，不移动、不改名、不加密影片。影片的直接所在文件夹也会隐藏，"
                "扫描源根目录除外。Finder 按 ⌘⇧. 可显示隐藏项目。取消此选项并重新扫描可恢复原属性。"
                "Mac 不提供 Windows 的加强隐藏；不支持隐藏标记的磁盘上，影片仍可正常入库。"
            )
        privacy_hint.setObjectName("mutedSmall")
        privacy_hint.setWordWrap(True)
        layout.addWidget(privacy_hint)
        local_hint = QLabel("移除扫描源会清除相关影库记录，并恢复由本软件设置的隐藏属性；影片文件不会删除。真正删除可使用影片卡片上的 ⌫，或详情中的“移到回收站”。")
        local_hint.setObjectName("warning")
        local_hint.setWordWrap(True)
        layout.addWidget(local_hint)
        actions = QHBoxLayout()
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        scan_all = QPushButton("扫描全部来源")
        scan_all.clicked.connect(self.validate_all)
        start = QPushButton("扫描勾选来源")
        start.setObjectName("primary")
        start.clicked.connect(self.validate)
        actions.addStretch()
        actions.addWidget(close)
        actions.addWidget(scan_all)
        actions.addWidget(start)
        layout.addLayout(actions)
        self.reload_sources()

    def reload_sources(self, checked_path: str = "") -> None:
        self.sources.clear()
        for source in core.list_scan_roots():
            status = "可访问" if source["available"] else "当前离线"
            scanned = (source.get("last_scanned_at") or "尚未扫描").replace("T", " ")[:19]
            item = QListWidgetItem(
                f"{source['path']}\n{source['movie_count']} 部 · 待关联 {source.get('missing_count',0)} 部 · {source['size_label']} · {status} · 上次扫描 {scanned}"
            )
            item.setData(Qt.ItemDataRole.UserRole, source["path"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if source["path"] == checked_path else Qt.CheckState.Unchecked)
            item.setSizeHint(QSize(0, 62))
            self.sources.addItem(item)

    def choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "添加影片硬盘或文件夹")
        if path:
            try:
                registered = core.register_scan_root(path)
                self.reload_sources(registered)
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "无法添加", str(exc))

    def remove_selected(self) -> None:
        item = self.sources.currentItem()
        if not item:
            QMessageBox.information(self, "选择扫描源", "请先选中要移除的扫描源。")
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        answer = QMessageBox.question(
            self, "移除扫描源",
            f"从影库移除这个来源及其全部影片记录？\n\n{path}\n\n"
            "硬盘上的影片文件不会删除；由影库隐藏的影片会恢复原来的文件属性。"
            "相关封面资料、评分、喜欢和备注会从数据库清除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            removed = core.remove_scan_root(path)
            self.library_changed = True
            self.reload_sources()
            QMessageBox.information(self, "已移除", f"已从影库移除 {removed} 部影片；硬盘文件保持不变并已恢复原属性。")

    def selected_roots(self) -> list[str]:
        roots: list[str] = []
        for index in range(self.sources.count()):
            item = self.sources.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                roots.append(item.data(Qt.ItemDataRole.UserRole))
        return roots

    def validate_all(self) -> None:
        for index in range(self.sources.count()):
            self.sources.item(index).setCheckState(Qt.CheckState.Checked)
        self.validate()

    def validate(self) -> None:
        if not self.selected_roots():
            QMessageBox.information(self, "请选择位置", "请先添加并勾选至少一个扫描源。")
            return
        with core.connect() as conn:
            conn.executemany(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    ("min_duration_minutes", str(self.duration_threshold.value())),
                    ("hide_files_after_import", "true" if self.hide_imported.isChecked() else "false"),
                    ("hide_files_system_attribute", "true" if bool(self.hide_mode.currentData()) else "false"),
                ],
            )
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("系统设置")
        self.resize(620, 580)
        layout = QVBoxLayout(self)
        title = QLabel("系统设置")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        search_title = QLabel("演员搜索")
        search_title.setObjectName("sectionTitle")
        layout.addWidget(search_title)
        description = QLabel("点击演员头像下的搜索按钮，在浏览器中打开你设置的网址。")
        description.setObjectName("searchHelp")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.search_template = QLineEdit(actor_search.load_template())
        self.search_template.setAccessibleName("演员搜索网址模板")
        self.search_template.setPlaceholderText(actor_search.DEFAULT_TEMPLATE)
        layout.addWidget(self.search_template)
        template_hint = QLabel("用 <name> 代表演员姓名；普通与隐私模式共用此地址。")
        template_hint.setTextFormat(Qt.TextFormat.PlainText)
        template_hint.setObjectName("searchHelp")
        layout.addWidget(template_hint)
        presets = QComboBox()
        presets.setAccessibleName("填入常用搜索网址")
        presets.addItem("填入常用网址…", "")
        for label, url in [
            ("百度", actor_search.DEFAULT_TEMPLATE),
            ("豆瓣", "https://www.douban.com/search?q=<name>"),
            ("IMDb", "https://www.imdb.com/find/?q=<name>&s=nm"),
        ]:
            presets.addItem(label, url)
        def fill_preset(index):
            if presets.itemData(index):
                self.search_template.setText(presets.itemData(index))
                presets.setCurrentIndex(0)
        presets.activated.connect(fill_preset)
        layout.addWidget(presets)
        self.search_preview = QLabel()
        self.search_preview.setObjectName("searchPreview")
        self.search_preview.setTextFormat(Qt.TextFormat.PlainText)
        self.search_preview.setWordWrap(True)
        self.search_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.search_preview.setMinimumHeight(36)
        self.search_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.search_preview)
        source_title = QLabel("联网资料源")
        source_title.setObjectName("sectionTitle")
        layout.addWidget(source_title)
        lead = QLabel("影片资料支持 Bangumi、编号资料库与 TMDb。演员会先利用影片来源和发行编号确认身份，再查询 Bangumi 人物、TMDb 别名、Wikidata、Wikipedia 与 Wikimedia；姓名、别名、头像和简介都会保存在本机。")
        lead.setObjectName("muted")
        lead.setWordWrap(True)
        layout.addWidget(lead)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("粘贴 TMDb API Read Access Token")
        layout.addWidget(self.token)
        configured = bool(core.setting_value("tmdb_token"))
        hint = QLabel("已配置令牌；留空不会覆盖。" if configured else "令牌只保存在本机数据库中。")
        hint.setObjectName("mutedSmall")
        layout.addWidget(hint)
        self.auto_match = QCheckBox("扫描完成后自动匹配新影片资料")
        self.auto_match.setChecked(core.setting_value("auto_match_after_scan") != "false")
        self.auto_match.setToolTip("只会自动采用高置信度且无冲突的结果；模糊候选仍需人工确认。")
        layout.addWidget(self.auto_match)
        layout.addStretch()
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("primary")
        save.clicked.connect(self.accept)
        self.save_button = save
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)
        self.search_template.textChanged.connect(self.update_search_preview)
        self.update_search_preview()

    def update_search_preview(self) -> None:
        try:
            url = actor_search.search_url(self.search_template.text(), "张译")
        except ValueError as exc:
            self.search_preview.setText(str(exc))
            self.search_preview.setStyleSheet("color:#edb0a5;")
            self.save_button.setEnabled(False)
        else:
            self.search_preview.setText("示例（张译）：" + QUrl(url).toDisplayString())
            self.search_preview.setStyleSheet("")
            self.save_button.setEnabled(True)

    def accept(self) -> None:
        if not self.save_button.isEnabled():
            self.search_template.setFocus()
            return
        try:
            self.save()
        except (OSError, ValueError, core.sqlite3.Error) as exc:
            QMessageBox.warning(self, "设置未保存", f"无法保存设置：{exc}")
            return
        super().accept()

    def save(self) -> None:
        actor_search.search_url(self.search_template.text(), "张译")
        token = self.token.text().strip()
        with core.connect() as conn:
            actor_search.save_template(self.search_template.text())
            if token:
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES('tmdb_token',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (token,),
                )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('auto_match_after_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("true" if self.auto_match.isChecked() else "false",),
            )


class ManualEditDialog(QDialog):
    def __init__(self, movie: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.movie = movie
        self.cast_rows: list[tuple[QLineEdit, QLineEdit, QLineEdit]] = []
        self.cast_originals: list[dict[str, Any]] = []
        self.setWindowTitle("手工编辑影片资料")
        self.resize(720, 670)
        outer = QVBoxLayout(self)
        title = QLabel("手工编辑影片资料")
        title.setObjectName("dialogTitle")
        outer.addWidget(title)
        lead = QLabel("小众影片若联网搜索不到，可以在这里完整维护片名、封面、简介、主演和头像。头像支持本地图片路径或网络地址。")
        lead.setObjectName("muted")
        lead.setWordWrap(True)
        outer.addWidget(lead)
        grid = QGridLayout()
        grid.addWidget(QLabel("片名"), 0, 0)
        self.title_edit = QLineEdit(movie.get("title", ""))
        grid.addWidget(self.title_edit, 0, 1, 1, 3)
        grid.addWidget(QLabel("年份"), 1, 0)
        self.year_edit = QLineEdit(str(movie.get("year") or ""))
        self.year_edit.setMaximumWidth(110)
        grid.addWidget(self.year_edit, 1, 1)
        grid.addWidget(QLabel("类型"), 1, 2)
        self.genres_edit = QLineEdit("、".join(movie.get("genres") or []))
        self.genres_edit.setPlaceholderText("剧情、悬疑、治愈…")
        grid.addWidget(self.genres_edit, 1, 3)
        grid.addWidget(QLabel("封面"), 2, 0)
        self.poster_edit = QLineEdit(movie.get("local_poster") or movie.get("poster_url") or "")
        grid.addWidget(self.poster_edit, 2, 1, 1, 2)
        choose = QPushButton("选择图片…")
        choose.clicked.connect(self.choose_poster)
        grid.addWidget(choose, 2, 3)
        outer.addLayout(grid)
        outer.addWidget(QLabel("影片简介"))
        self.overview_edit = QPlainTextEdit(movie.get("overview") or "")
        self.overview_edit.setMaximumHeight(110)
        outer.addWidget(self.overview_edit)
        outer.addWidget(QLabel("主演与角色"))
        cast_scroll = NativeScrollArea()
        cast_scroll.setWidgetResizable(True)
        cast_scroll.setMinimumHeight(170)
        cast_container = QWidget()
        self.cast_grid = QGridLayout(cast_container)
        for column, text in enumerate(("姓名", "角色", "头像路径或网址")):
            head = QLabel(text)
            head.setObjectName("mutedSmall")
            self.cast_grid.addWidget(head, 0, column)
        existing = movie.get("cast") or []
        for person in existing or [{}]:
            self.add_cast_row(person)
        cast_scroll.setWidget(cast_container)
        outer.addWidget(cast_scroll, 1)
        add_actor = QPushButton("＋ 添加演员")
        add_actor.clicked.connect(lambda: self.add_cast_row({}))
        outer.addWidget(add_actor, 0, Qt.AlignmentFlag.AlignLeft)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存资料")
        save.setObjectName("primary")
        save.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(save)
        outer.addLayout(actions)

    def add_cast_row(self, person: dict[str, Any]) -> None:
        row = len(self.cast_rows) + 1
        fields = tuple(QLineEdit(str(person.get(key) or "")) for key in ("name", "role", "avatar"))
        fields[2].setPlaceholderText("可留空")
        for column, field in enumerate(fields):
            self.cast_grid.addWidget(field, row, column)
        remove = QPushButton("移除")
        remove.clicked.connect(lambda: self.remove_cast_row(fields, remove))
        self.cast_grid.addWidget(remove, row, 3)
        self.cast_rows.append(fields)
        self.cast_originals.append(dict(person))

    def remove_cast_row(self, fields: tuple, button: QPushButton) -> None:
        fields[0].clear()
        for field in (*fields, button):
            field.hide()

    def choose_poster(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择封面", "", "图片 (*.jpg *.jpeg *.png *.webp)")
        if path:
            self.poster_edit.setText(path)

    def save(self) -> None:
        cast = []
        for original, (name, role, avatar) in zip(self.cast_originals, self.cast_rows):
            if name.text().strip():
                cast.append({**original, "name": name.text().strip(), "role": role.text().strip(), "avatar": avatar.text().strip()})
        raw_year = self.year_edit.text().strip()
        genres = [x.strip() for x in self.genres_edit.text().replace("，", "、").replace(",", "、").split("、") if x.strip()]
        poster = self.poster_edit.text().strip()
        fields: dict[str, Any] = {
            "title": self.title_edit.text().strip() or self.movie.get("title", ""),
            "year": int(raw_year) if raw_year.isdigit() else None,
            "genres": genres, "overview": self.overview_edit.toPlainText().strip(), "cast_json": cast,
        }
        if poster and Path(poster).is_file():
            fields["local_poster"] = poster
        update_movie(self.movie["id"], **fields)
        if poster.startswith(("http://", "https://")):
            with core.connect() as conn:
                conn.execute("UPDATE movies SET poster_url=?, match_status='manual', updated_at=? WHERE id=?", (poster, core.now_iso(), self.movie["id"]))
        else:
            with core.connect() as conn:
                conn.execute("UPDATE movies SET match_status='manual', updated_at=? WHERE id=?", (core.now_iso(), self.movie["id"]))


class TasteDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("我的口味画像")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        title = QLabel("我的口味画像")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        lead = QLabel("这不是平台替你决定的推荐，而是从你的评分、喜欢、保留与待删除中逐渐长出来的私人画像。")
        lead.setWordWrap(True)
        lead.setObjectName("muted")
        layout.addWidget(lead)
        with core.connect() as conn:
            rows = conn.execute("SELECT genres,cast_json,personal_rating,favorite,disposition FROM movies WHERE exists_now=1").fetchall()
        rated = [row for row in rows if (row["personal_rating"] or 0) > 0]
        favorites = [row for row in rows if row["favorite"]]
        average = sum(row["personal_rating"] for row in rated) / len(rated) if rated else 0
        stat_row = QHBoxLayout()
        for value, caption in ((len(rated), "已评分"), (len(favorites), "喜欢"), (f"{average:.1f}" if rated else "—", "平均分"), (sum(1 for r in rows if r["disposition"] == "delete"), "待处理")):
            box = QFrame()
            box.setObjectName("statCard")
            box_lay = QVBoxLayout(box)
            number = QLabel(str(value))
            number.setObjectName("statValue")
            desc = QLabel(caption)
            desc.setObjectName("mutedSmall")
            box_lay.addWidget(number)
            box_lay.addWidget(desc)
            stat_row.addWidget(box)
        layout.addLayout(stat_row)
        genre_scores: dict[str, list[float]] = {}
        actor_scores: dict[str, list[float]] = {}
        for row in rows:
            signal = float(row["personal_rating"] or 0) + (3 if row["favorite"] else 0) - (3 if row["disposition"] == "delete" else 0)
            if signal == 0:
                continue
            for genre in core.json_value(row["genres"], []):
                genre_scores.setdefault(genre, []).append(signal)
            for person in core.json_value(row["cast_json"], []):
                if person.get("name"):
                    actor_scores.setdefault(person["name"], []).append(signal)
        top_genres = sorted(genre_scores.items(), key=lambda x: (sum(x[1]) / len(x[1]), len(x[1])), reverse=True)[:8]
        top_actors = sorted(actor_scores.items(), key=lambda x: (sum(x[1]) / len(x[1]), len(x[1])), reverse=True)[:8]
        columns = QHBoxLayout()
        genre_box = QFrame()
        genre_box.setObjectName("insightCard")
        genre_layout = QVBoxLayout(genre_box)
        genre_layout.addWidget(self.block_title("偏爱类型"))
        genre_layout.addWidget(QLabel(self.format_rank(top_genres, "继续评分后，会看到你偏爱的类型。")))
        actor_box = QFrame()
        actor_box.setObjectName("insightCard")
        actor_layout = QVBoxLayout(actor_box)
        actor_layout.addWidget(self.block_title("偏爱主演"))
        actor_layout.addWidget(QLabel(self.format_rank(top_actors, "补全主演资料并评分后，会形成主演偏好。")))
        columns.addWidget(genre_box)
        columns.addWidget(actor_box)
        layout.addLayout(columns, 1)
        guidance = QLabel("建议：看完一部先评分，再写一句喜欢或不喜欢的原因。评分负责排序，喜欢负责收藏，待处理负责清理——三种信号分开，画像才会准确。")
        guidance.setWordWrap(True)
        guidance.setObjectName("tasteHint")
        layout.addWidget(guidance)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

    @staticmethod
    def block_title(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    @staticmethod
    def format_rank(items: list[tuple[str, list[float]]], fallback: str) -> str:
        if not items:
            return fallback
        return "\n\n".join(f"{index + 1}.  {name}    ·    {len(values)} 部" for index, (name, values) in enumerate(items))


class MatchDialog(QDialog):
    applied = Signal()

    def __init__(self, movie: dict[str, Any], run_task: Callable[..., None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.movie = movie
        self.run_task = run_task
        self.candidates: list[dict[str, Any]] = []
        self.setWindowTitle("匹配影片资料")
        self.resize(760, 590)
        layout = QVBoxLayout(self)
        title = QLabel("为影片匹配资料")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)
        search_row = QHBoxLayout()
        self.query = QLineEdit(movie.get("title", ""))
        self.provider = QComboBox()
        self.provider.addItem("全部来源", "all")
        self.provider.addItem("TMDb", "tmdb")
        self.provider.addItem("Bangumi", "bangumi")
        self.provider.addItem("编号资料库（实验）", "catalog")
        self.search_button = QPushButton("搜索")
        self.search_button.setObjectName("primary")
        self.search_button.clicked.connect(self.search)
        search_row.addWidget(self.query, 1)
        search_row.addWidget(self.provider)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)
        self.warning = QLabel()
        self.warning.setObjectName("warning")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        self.results = QListWidget()
        self.results.itemDoubleClicked.connect(lambda _: self.apply_selected())
        layout.addWidget(self.results, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.apply_button = QPushButton("使用所选资料")
        self.apply_button.setObjectName("primary")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected)
        actions.addWidget(cancel)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)

    def search(self) -> None:
        query = self.query.text().strip()
        if not query:
            return
        self.search_button.setEnabled(False)
        self.search_button.setText("搜索中…")
        provider = self.provider.currentData()

        def work() -> dict[str, Any]:
            results, warnings = [], []
            if provider in {"all", "tmdb"}:
                try:
                    results.extend(core.search_tmdb(query, self.movie.get("year")))
                except Exception as exc:
                    warnings.append(f"TMDb：{exc}")
            if provider in {"all", "bangumi"}:
                try:
                    results.extend(core.search_bangumi(query))
                except Exception as exc:
                    warnings.append(f"Bangumi：{exc}")
            if provider in {"all", "catalog"}:
                try:
                    results.extend(core.search_code_catalog(query))
                except Exception as exc:
                    warnings.append(f"编号资料库：{exc}")
            return {"results": results, "warnings": warnings}

        self.run_task(work, self.show_results, self.show_error)

    def show_results(self, payload: dict[str, Any]) -> None:
        self.search_button.setEnabled(True)
        self.search_button.setText("搜索")
        self.candidates = payload["results"]
        self.warning.setText("\n".join(payload["warnings"]))
        self.results.clear()
        for candidate in self.candidates:
            rating = f"  ·  {candidate['external_rating']:.1f} 分" if candidate.get("external_rating") else ""
            item = QListWidgetItem(
                f"{candidate.get('title','')}  ({candidate.get('year') or '年份未知'})\n"
                f"{candidate.get('original_title','')}  ·  {candidate.get('provider','').upper()}{rating}"
            )
            item.setSizeHint(QSize(0, 58))
            self.results.addItem(item)
        self.apply_button.setEnabled(bool(self.candidates))
        if self.candidates:
            self.results.setCurrentRow(0)

    def show_error(self, message: str) -> None:
        self.search_button.setEnabled(True)
        self.search_button.setText("搜索")
        self.warning.setText(message)

    def apply_selected(self) -> None:
        index = self.results.currentRow()
        if index < 0 or index >= len(self.candidates):
            return
        self.apply_button.setEnabled(False)
        self.apply_button.setText("正在获取完整资料…")
        candidate = self.candidates[index]
        candidate["confidence"] = 1.0

        def done(_: Any) -> None:
            self.applied.emit()
            self.accept()

        self.run_task(lambda: core.apply_metadata(self.movie["id"], candidate), done, self.show_error)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        core.init_db()
        settings_override = os.environ.get("YINGKU_SETTINGS_PATH", "")
        if sys.platform == "darwin" and not settings_override:
            settings_override = str(core.DATA_DIR / "window-layout.ini")
        self.ui_settings = (
            QSettings(settings_override, QSettings.Format.IniFormat)
            if settings_override else QSettings("YingKu", "YingKuDesktop")
        )
        self.ui_save_timer = QTimer(self)
        self.ui_save_timer.setSingleShot(True)
        self.ui_save_timer.timeout.connect(self.save_ui_state)
        self.actor_reflow_timer = QTimer(self)
        self.actor_reflow_timer.setSingleShot(True)
        self.actor_reflow_timer.timeout.connect(self.reflow_actor_chips)
        self.actor_status_timer = QTimer(self)
        self.actor_status_timer.timeout.connect(self.update_actor_enrichment_progress)
        self.pool = QThreadPool.globalInstance()
        self.tasks = TaskRunner(self, self.pool)
        self.images = ImageManager(self)
        self.view = "all"
        self.sort = "updated"
        self.actor_filter = ""
        self._keyword_attempted = set()
        self.actors_expanded = False
        self.current_movie_id: int | None = None
        self.extraction_movie_id: int | None = None
        self.extraction_running = False
        self.extraction_manual = False
        self.loading_detail = False
        self.current_hero_source = ""
        self.actor_photo_reopen = ""
        self.cards: list[MovieCard] = []
        self.movie_page = 0
        self.page_size = 60
        self.maintenance_active = False
        self.privacy_locking = False
        self.privacy_relaunch_args = []
        self.layout_mode = "landscape"
        self.restoring_ui_state = True
        self.last_mode_geometry: Any = None
        self.setWindowTitle("影库")
        self.setWindowIcon(QIcon(str(resource_path("assets/app-icon.svg"))))
        self.resize(1420, 880)
        self.setMinimumSize(900, 480)
        self.setWindowFlag(Qt.WindowType.WindowFullscreenButtonHint, True)
        self.build_ui()
        view_menu = self.menuBar().addMenu("显示")
        self.fullscreen_action = QAction("进入全屏", self)
        self.fullscreen_action.setShortcut(QKeySequence("Ctrl+Meta+F" if sys.platform == "darwin" else "F11"))
        self.fullscreen_action.triggered.connect(self.toggle_fullscreen)
        view_menu.addAction(self.fullscreen_action)
        self.restore_ui_state()
        self.restoring_ui_state = False
        if self.detected_layout_mode() != self.layout_mode:
            self.switch_layout_mode(self.detected_layout_mode())
        self.last_mode_geometry = self.saveGeometry()
        self.load_all()
        self.lock_shortcut = QShortcut(QKeySequence("Ctrl+Shift+L"), self)
        self.lock_shortcut.activated.connect(self.flip_library)
        # Private access lasts for this app session. Focus changes, playback,
        # idle time, and sleep do not change the selected library.
        if privacy.MODE == "private":
            QApplication.instance().installEventFilter(self)
        if os.environ.get("YINGKU_DISABLE_STARTUP_TASKS") != "1":
            QTimer.singleShot(450, self.start_startup_sync)
            QTimer.singleShot(650, self.restore_video_player)

    def run_task(self, fn: Callable[..., Any], on_result: Callable[[Any], None] | None = None, on_error: Callable[[str], None] | None = None) -> None:
        if self.maintenance_active:
            return
        self.tasks.start(
            fn,
            lambda value: on_result(value) if on_result and not self.privacy_locking else None,
            lambda message: (on_error or self.show_error)(message) if not self.privacy_locking else None,
        )

    def lock_private(self):
        if privacy.MODE == "private" and not privacy.AUTHENTICATING:
            self.begin_flip(False)

    def flip_library(self):
        if privacy.MODE == "private":
            self.lock_private()
        elif privacy.MODE == "public":
            self.begin_flip(True)

    def begin_flip(self, to_private):
        if self.privacy_locking:
            return
        self.privacy_locking = True
        self.maintenance_active = True
        self.save_ui_state()
        self.privacy_relaunch_args = ["--private"] if to_private else ["--public"]
        # Hide every private view immediately, including previews and modal dialogs.
        for timer in self.findChildren(QTimer):
            timer.stop()
        for widget in QApplication.topLevelWidgets():
            if widget is not self:
                widget.hide()
                if isinstance(widget, QDialog):
                    widget.reject()
        self.protected_content = self.takeCentralWidget()
        self.protected_content.hide()
        self.statusBar().hide()
        self.setWindowTitle("影库 · 正在翻转")
        shield = QLabel("私密模式已锁定\n正在安全结束后台任务…" if not to_private else "正在翻转 · 接下来请完成系统验证")
        shield.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(shield)
        self.images.cache.clear()
        self.privacy_restart_timer = QTimer(self)
        self.privacy_restart_timer.timeout.connect(self.finish_flip)
        self.privacy_restart_timer.start(150)

    def finish_flip(self):
        if self.pool.activeThreadCount():
            return
        self.privacy_restart_timer.stop()
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve()))
        command += self.privacy_relaunch_args + ["--wait-for-pid", str(os.getpid())]
        try:
            subprocess.Popen(command, start_new_session=True)
        except OSError:
            self.centralWidget().setText("模式已锁定。请关闭并重新打开影库。")
            return
        self.relaunching = True
        self.close()
        QApplication.instance().quit()

    def open_partition(self):
        if not privacy.MODE or self.privacy_locking:
            return
        if self.pool.activeThreadCount():
            QMessageBox.information(self, "任务进行中", "请等后台任务完成后再整理分区。")
            return
        from privacy_ui import PartitionDialog
        self.maintenance_active = True
        dialog = PartitionDialog(self)
        try:
            dialog.exec()
        finally:
            self.maintenance_active = self.privacy_locking
        if dialog.changed and not self.privacy_locking:
            self.current_movie_id = None
            self.current_hero_source = ""
            self.detail_stack.setCurrentIndex(0)
            self.images.cache.clear()
            self.load_all()

    def move_current_partition(self):
        if self.privacy_locking or not privacy.MODE:
            return
        movie = self.current_movie()
        if not movie:
            return
        if self.pool.activeThreadCount():
            QMessageBox.information(self, "任务进行中", "请等后台任务完成后再移动分区。")
            return
        target = "普通" if privacy.MODE == "private" else "私密"
        self.maintenance_active = True
        try:
            if privacy.MODE == "public" and not privacy.authenticate(self):
                return
            if self.privacy_locking:
                return
            if QMessageBox.question(self, "移动影片分区", f"将这部影片移入{target}模式？\n收藏、评分、备注和截图会一并保留，原始视频文件的位置不变。") != QMessageBox.StandardButton.Yes:
                return
            if self.privacy_locking:
                return
            privacy.move_movie(movie["id"])
            self.current_movie_id = None
            self.current_hero_source = ""
            self.detail_stack.setCurrentIndex(0)
            self.images.cache.clear()
            self.load_all()
            self.statusBar().showMessage(f"已移入{target}模式", 4000)
        except Exception as exc:
            self.show_error(str(exc))
        finally:
            self.maintenance_active = self.privacy_locking

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "fullscreen_action"):
            self.fullscreen_action.setText("退出全屏" if self.isFullScreen() else "进入全屏")

    @staticmethod
    def scrollable_panel(content):
        scroll = NativeScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setMinimumSize(0, 0)
        scroll.setWidget(content)
        return scroll

    def build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.setCentralWidget(root)
        self.sidebar_scroll = self.scrollable_panel(self.build_sidebar())
        self.sidebar_scroll.setFixedWidth(225)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root_layout.addWidget(self.sidebar_scroll)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(7)
        self.library_panel = self.build_library()
        self.library_scroll = self.scrollable_panel(self.library_panel)
        set_background(self.library_scroll.viewport(), "#101115")
        self.library_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.library_scroll.setMinimumHeight(140)
        self.scroll = self.library_scroll  # Shared by paging and existing scroll shortcuts.
        self.library_scroll.viewport().installEventFilter(self)
        self.build_sticky_header()
        self.library_scroll.verticalScrollBar().valueChanged.connect(self.update_sticky_header)
        self.library_scroll.verticalScrollBar().rangeChanged.connect(lambda *_: self.update_sticky_header())
        self.splitter.addWidget(self.library_scroll)
        self.detail = self.build_detail()
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([780, 550])
        self.splitter.splitterMoved.connect(lambda *_: self.ui_save_timer.start(350))
        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self.splitter)
        self.actor_library = ActorLibrary(self, PosterLabel)
        self.content_stack.addWidget(self.actor_library)
        root_layout.addWidget(self.content_stack, 1)
        self.bottom_stats = QLabel("")
        self.bottom_stats.setObjectName("bottomStats")
        self.statusBar().addPermanentWidget(self.bottom_stats)

    def restore_ui_state(self) -> None:
        saved_mode = str(self.ui_settings.value("lastLayoutMode", "landscape"))
        if saved_mode not in {"landscape", "portrait"}:
            saved_mode = "landscape"
        self.layout_mode = saved_mode
        self.configure_layout_mode(saved_mode)
        geometry = self.ui_settings.value(f"windowGeometry_{saved_mode}") or self.ui_settings.value("windowGeometry")
        splitter_state = self.ui_settings.value(f"mainSplitter_{saved_mode}") or self.ui_settings.value("mainSplitter")
        if geometry:
            self.restoreGeometry(geometry)
        if splitter_state:
            self.splitter.restoreState(splitter_state)
        hero_height = self.ui_settings.value(
            f"detailHeroHeight_{saved_mode}",
            self.ui_settings.value("detailHeroHeight", 250),
            type=int,
        )
        self.detail_poster.setFixedHeight(max(HERO_MIN_HEIGHT, min(HERO_MAX_HEIGHT, hero_height)))

    def save_ui_state(self) -> None:
        mode = self.layout_mode
        self.ui_settings.setValue("lastLayoutMode", mode)
        self.ui_settings.setValue(f"windowGeometry_{mode}", self.saveGeometry())
        self.ui_settings.setValue(f"mainSplitter_{mode}", self.splitter.saveState())
        self.ui_settings.setValue(f"detailHeroHeight_{mode}", self.detail_poster.height())
        self.ui_settings.sync()

    def hero_height_changed(self, height: int) -> None:
        self.ui_settings.setValue(f"detailHeroHeight_{self.layout_mode}", height)
        self.ui_settings.sync()
        self.statusBar().showMessage(f"详情封面高度已记住：{height} 像素", 1800)

    def detected_layout_mode(self) -> str:
        return "portrait" if self.height() > self.width() else "landscape"

    def configure_layout_mode(self, mode: str) -> None:
        portrait = mode == "portrait"
        self.splitter.setOrientation(Qt.Orientation.Vertical if portrait else Qt.Orientation.Horizontal)
        self.detail.setMinimumHeight(160 if portrait else 0)
        self.detail.setMinimumWidth(380)
        self.library_scroll.setMinimumHeight(160 if portrait else 0)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)

    def switch_layout_mode(self, mode: str) -> None:
        if mode == self.layout_mode or mode not in {"landscape", "portrait"}:
            return
        old_mode = self.layout_mode
        if self.last_mode_geometry is not None:
            self.ui_settings.setValue(f"windowGeometry_{old_mode}", self.last_mode_geometry)
            self.ui_settings.setValue(f"mainSplitter_{old_mode}", self.splitter.saveState())
            self.ui_settings.setValue(f"detailHeroHeight_{old_mode}", self.detail_poster.height())
        self.layout_mode = mode
        self.configure_layout_mode(mode)
        saved_splitter = self.ui_settings.value(f"mainSplitter_{mode}")
        if saved_splitter:
            self.splitter.restoreState(saved_splitter)
        else:
            extent = self.splitter.height() if mode == "portrait" else self.splitter.width()
            self.splitter.setSizes([max(360, int(extent * 0.58)), max(360, int(extent * 0.42))])
        hero_height = self.ui_settings.value(f"detailHeroHeight_{mode}", 250, type=int)
        self.detail_poster.setFixedHeight(max(HERO_MIN_HEIGHT, min(HERO_MAX_HEIGHT, hero_height)))
        self.ui_settings.setValue("lastLayoutMode", mode)
        self.ui_settings.sync()
        self.last_mode_geometry = self.saveGeometry()
        QTimer.singleShot(0, self.reflow_cards)
        QTimer.singleShot(0, self.reflow_actor_chips)
        QTimer.singleShot(0, self.refresh_detail_hero)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "splitter") and not getattr(self, "restoring_ui_state", False):
            detected = self.detected_layout_mode()
            if detected != self.layout_mode:
                self.switch_layout_mode(detected)
            else:
                self.last_mode_geometry = self.saveGeometry()
        if hasattr(self, "ui_save_timer"):
            self.ui_save_timer.start(400)
        if hasattr(self, "actor_reflow_timer"):
            self.actor_reflow_timer.start(100)

    def closeEvent(self, event: Any) -> None:
        self.save_ui_state()
        self.images.shutdown()
        super().closeEvent(event)

    def build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setMinimumWidth(200)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(19, 25, 19, 22)
        self.brand = QPushButton("◉  影 库")
        self.brand.setObjectName("brand")
        self.brand.setFlat(True)
        self.brand.setCursor(Qt.CursorShape.PointingHandCursor)
        self.brand.setAccessibleName("影库")
        self.brand.clicked.connect(self.flip_library)
        layout.addWidget(self.brand)
        brand_sub = QLabel("MY FILM ARCHIVE")
        brand_sub.setObjectName("brandSub")
        layout.addWidget(brand_sub)
        partition = QPushButton("整理影片分区…")
        partition.setObjectName("sideText")
        partition.clicked.connect(self.open_partition)
        layout.addWidget(partition)
        layout.addSpacing(12)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        navs = [
            ("all", "▦   全部影片"), ("actors", "演员"), ("favorite", "♥   我的喜欢"), ("unwatched", "◷   还没看过"),
            ("unmatched", "?   待匹配资料"), ("duplicates", "⧉   重复影片"), ("delete", "⌫   待处理"),
        ]
        for index, (key, text) in enumerate(navs):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("nav", True)
            button.setProperty("viewKey", key)
            button.clicked.connect(lambda checked=False, value=key: self.set_view(value))
            self.nav_group.addButton(button)
            layout.addWidget(button)
            if index == 0:
                button.setChecked(True)
        layout.addStretch()
        for symbol, title, callback in [
            ("⚙", "系统设置", self.open_settings),
            ("◎", "我的口味画像", self.open_taste),
            ("⌁", "扫描源管理", self.open_scan),
            ("▣", "备份与文件关联", self.open_maintenance),
        ]:
            button = QPushButton(title)
            button.setObjectName("sideText")
            button.setProperty("bottomMenu", True)
            icon = QPixmap(32, 32)
            icon.setDevicePixelRatio(2)
            icon.fill(Qt.GlobalColor.transparent)
            painter = QPainter(icon)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor("#888b91"))
            painter.setFont(QFont(UI_FONT, 13))
            painter.drawText(0, 0, 16, 16, Qt.AlignmentFlag.AlignCenter, symbol)
            painter.end()
            button.setIcon(QIcon(icon))
            button.setIconSize(QSize(16, 16))
            button.clicked.connect(callback)
            layout.addWidget(button)
        privacy_hint = QLabel("● 资料库只保存在本机")
        privacy_hint.setObjectName("privacy")
        privacy_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(privacy_hint)
        return side

    def build_library(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("libraryPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(34, 25, 28, 25)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("搜索片名、演员、标签或文件名…")
        self.search.setClearButtonEnabled(True)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.load_movies)
        self.search.textChanged.connect(lambda: self.search_timer.start(260))
        top.addWidget(self.search, 1)
        layout.addLayout(top)
        actor_head = QHBoxLayout()
        actor_title = QLabel("按演员浏览")
        actor_title.setObjectName("actorStripTitle")
        self.active_actor = QLabel("")
        self.active_actor.setObjectName("activeFilter")
        self.clear_actor_button = QPushButton("清除演员")
        self.clear_actor_button.setObjectName("compactText")
        self.clear_actor_button.clicked.connect(self.clear_actor_filter)
        self.clear_actor_button.hide()
        self.actor_expand = QPushButton("展开")
        self.actor_expand.setObjectName("compactText")
        self.actor_expand.clicked.connect(self.toggle_actors)
        actor_head.addWidget(actor_title)
        actor_head.addWidget(self.active_actor)
        actor_head.addWidget(self.clear_actor_button)
        actor_head.addStretch()
        self.actor_refresh = QToolButton()
        self.actor_refresh.setText("↻")
        self.actor_refresh.setObjectName("actorRefresh")
        self.actor_refresh.setToolTip("只补全没有头像的演员；右键演员头像可强制重新匹配")
        self.actor_refresh.clicked.connect(self.refresh_actor_information)
        actor_head.addWidget(self.actor_refresh)
        actor_head.addWidget(self.actor_expand)
        layout.addLayout(actor_head)
        self.actor_update_status = QLabel("演员资料：使用本地缓存")
        self.actor_update_status.setObjectName("actorUpdateStatus")
        self.actor_update_status.setWordWrap(True)
        layout.addWidget(self.actor_update_status)
        self.actor_scroll = NativeScrollArea(horizontal_only=True)
        self.actor_scroll.setWidgetResizable(True)
        self.actor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.actor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.actor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.actor_scroll.setFixedHeight(184)
        set_background(self.actor_scroll.viewport(), "#101115")
        self.actor_container = QWidget()
        set_background(self.actor_container, "#101115")
        self.actor_layout = QGridLayout(self.actor_container)
        self.actor_layout.setContentsMargins(0, 0, 0, 0)
        self.actor_layout.setSpacing(10)
        self.actor_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.actor_chips: list[ActorChip] = []
        self.actor_scroll.setWidget(self.actor_container)
        layout.addWidget(self.actor_scroll)
        layout.addSpacing(10)
        self.eyebrow = QLabel("PERSONAL FILM ARCHIVE")
        self.eyebrow.setObjectName("eyebrow")
        layout.addWidget(self.eyebrow)
        self.heading = QLabel("我的全部影片")
        self.heading.setObjectName("heading")
        layout.addWidget(self.heading)
        self.subtitle = QLabel("先看清自己的收藏，再决定留下什么。")
        self.subtitle.setObjectName("muted")
        layout.addWidget(self.subtitle)
        layout.addSpacing(8)
        self.filter_slot = QWidget()
        slot_layout = QVBoxLayout(self.filter_slot)
        slot_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_bar = QWidget()
        self.filter_bar.setObjectName("filmFilters")
        self.filter_layout = QGridLayout(self.filter_bar)
        self.filter_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_layout.setSpacing(6)
        self.result_label = QLabel("0 部影片")
        self.result_label.setObjectName("resultLabel")
        self.favorite_filter_combo = ScrollSafeComboBox()
        for text, key in (("喜欢：全部", "any"), ("只看喜欢", "liked"), ("未标喜欢", "not_liked")):
            self.favorite_filter_combo.addItem(text, key)
        self.favorite_filter_combo.currentIndexChanged.connect(self.filter_changed)
        self.rating_filter_combo = ScrollSafeComboBox()
        for text, key in (("评分：全部", "any"), ("未评分", "unrated"), ("4–5 星", "high"), ("3 星", "mid"), ("1–2 星", "low")):
            self.rating_filter_combo.addItem(text, key)
        self.rating_filter_combo.currentIndexChanged.connect(self.filter_changed)
        self.reset_filters_button = QPushButton("重置筛选")
        self.reset_filters_button.setObjectName("compactText")
        self.reset_filters_button.clicked.connect(self.reset_filters)
        self.reset_filters_button.hide()
        self.sort_combo = ScrollSafeComboBox()
        for text, key in (("最近入库", "updated"), ("片名 A-Z", "title"), ("上映年份", "year"), ("我的评分", "rating"), ("文件大小", "size")):
            self.sort_combo.addItem(text, key)
        self.sort_combo.currentIndexChanged.connect(self.sort_changed)
        self.auto_match_button = QPushButton("✨ 自动匹配")
        self.auto_match_button.setObjectName("matchAction")
        self.auto_match_button.setToolTip("批量匹配所有待补全影片；只自动采用高置信度结果")
        self.auto_match_button.clicked.connect(self.start_auto_match)
        slot_layout.addWidget(self.filter_bar)
        layout.addWidget(self.filter_slot)
        self.grid_widget = QWidget()
        set_background(self.grid_widget, "#101115")
        self.grid = QGridLayout(self.grid_widget)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.grid.setHorizontalSpacing(15)
        self.grid.setVerticalSpacing(18)
        self.grid.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.grid_widget)
        self.pagination = QWidget()
        pages = QHBoxLayout(self.pagination)
        pages.setContentsMargins(0, 0, 0, 0)
        self.previous_page = QPushButton("上一页")
        self.previous_page.clicked.connect(lambda: self.change_page(-1))
        self.page_label = QLabel()
        self.next_page = QPushButton("下一页")
        self.next_page.clicked.connect(lambda: self.change_page(1))
        pages.addStretch()
        pages.addWidget(self.previous_page)
        pages.addWidget(self.page_label)
        pages.addWidget(self.next_page)
        layout.addWidget(self.pagination)
        self.empty = QLabel("▶\n\n这里还没有影片\n\n选择一个装有影片的硬盘或文件夹，影库会自动建立索引。")
        self.empty.setObjectName("empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(140)
        self.empty.hide()
        layout.addWidget(self.empty, 1)
        self.progress_frame = QFrame()
        self.progress_frame.setObjectName("progressFrame")
        progress_layout = QVBoxLayout(self.progress_frame)
        self.progress_label = QLabel("正在扫描…")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress)
        self.progress_frame.hide()
        layout.addWidget(self.progress_frame)
        return panel

    def build_detail(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("detailPanel")
        panel.setMinimumWidth(380)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        self.detail_stack = QStackedWidget()
        placeholder = QLabel("选择一部影片\n查看资料并整理你的偏好")
        placeholder.setObjectName("emptyDetail")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_stack.addWidget(placeholder)
        page_scroll = NativeScrollArea()
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_background(page_scroll.viewport(), "#131419")
        page = QWidget()
        set_background(page, "#131419")
        self.detail_layout = QVBoxLayout(page)
        self.detail_layout.setContentsMargins(22, 18, 22, 34)
        self.detail_layout.setSpacing(12)
        self.detail_poster = ResizableHeroLabel()
        self.detail_poster.setObjectName("detailHero")
        self.detail_poster.setMinimumWidth(330)
        self.detail_poster.setFixedHeight(250)
        self.detail_poster.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.detail_poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_poster.installEventFilter(self)
        self.detail_poster.height_committed.connect(self.hero_height_changed)
        self.detail_layout.addWidget(self.detail_poster)
        self.detail_title = ExpandableTitle()
        self.detail_layout.addWidget(self.detail_title)
        keyword_row = QHBoxLayout()
        self.movie_keywords = KeywordStrip(limit=5)
        keyword_row.addWidget(self.movie_keywords, 1)
        self.keyword_refresh = QPushButton("更新")
        self.keyword_refresh.setObjectName("compactText")
        self.keyword_refresh.setToolTip("更新公开关键词和评论样本")
        self.keyword_refresh.clicked.connect(self.refresh_movie_keywords)
        keyword_row.addWidget(self.keyword_refresh)
        self.keyword_evidence = QPushButton("依据")
        self.keyword_evidence.setObjectName("compactText")
        self.keyword_evidence.clicked.connect(self.movie_keywords.show_evidence)
        keyword_row.addWidget(self.keyword_evidence)
        self.detail_layout.addLayout(keyword_row)
        score_row = QHBoxLayout()
        score_label = QLabel("我的评分")
        score_label.setObjectName("titleRatingLabel")
        score_row.addWidget(score_label)
        self.rating = StarRating()
        self.rating.changed.connect(self.rating_changed)
        score_row.addWidget(self.rating, 1)
        self.detail_layout.addLayout(score_row)
        self.detail_meta = QLabel()
        self.detail_meta.setObjectName("muted")
        self.detail_meta.setWordWrap(True)
        self.detail_meta.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.detail_layout.addWidget(self.detail_meta)
        screenshot_head = QHBoxLayout()
        screenshot_head.addWidget(self.section_label("影片内容速览"))
        screenshot_head.addStretch()
        self.extract_button = QPushButton("生成约 10 张")
        self.extract_button.clicked.connect(self.extract_screenshots)
        screenshot_head.addWidget(self.extract_button)
        self.detail_layout.addLayout(screenshot_head)
        screenshot_hint = QLabel("打开详情时自动生成 · 两排大图 · 点击放大后可用箭头或键盘左右键浏览 · 只保存在本机")
        screenshot_hint.setObjectName("mutedSmall")
        self.detail_layout.addWidget(screenshot_hint)
        self.screenshot_scroll = NativeScrollArea(horizontal_only=True)
        self.screenshot_scroll.setWidgetResizable(False)
        self.screenshot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.screenshot_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.screenshot_scroll.setFixedHeight(252)
        set_background(self.screenshot_scroll.viewport(), "#131419")
        self.screenshot_container = QWidget()
        set_background(self.screenshot_container, "#131419")
        self.screenshot_layout = QGridLayout(self.screenshot_container)
        self.screenshot_layout.setContentsMargins(0, 0, 0, 0)
        self.screenshot_layout.setHorizontalSpacing(9)
        self.screenshot_layout.setVerticalSpacing(9)
        self.screenshot_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.screenshot_scroll.setWidget(self.screenshot_container)
        self.detail_layout.addWidget(self.screenshot_scroll)
        action_row = QHBoxLayout()
        action_row.setSpacing(7)
        self.play_button = QPushButton("▶ 播放")
        self.play_button.setObjectName("primary")
        self.play_button.clicked.connect(self.play_current)
        folder = QPushButton("目录")
        folder.setProperty("detailAction", True)
        folder.setToolTip("打开影片所在目录")
        folder.clicked.connect(self.open_current_folder)
        self.match_button = QPushButton("匹配")
        self.match_button.setProperty("detailAction", True)
        self.match_button.setToolTip("联网匹配或更新影片资料")
        self.match_button.clicked.connect(self.open_match)
        edit = QPushButton("编辑")
        edit.setProperty("detailAction", True)
        edit.setToolTip("手工编辑影片资料")
        edit.clicked.connect(self.open_manual_edit)
        delete_local = QPushButton("回收站")
        delete_local.setObjectName("dangerText")
        delete_local.setToolTip("把本地影片文件移到 回收站")
        delete_local.clicked.connect(self.delete_current_file)
        action_row.addWidget(self.play_button)
        action_row.addWidget(folder)
        action_row.addWidget(self.match_button)
        action_row.addWidget(edit)
        action_row.addStretch()
        action_row.addWidget(delete_local)
        self.detail_layout.addLayout(action_row)
        self.partition_button = QPushButton("移入普通模式" if privacy.MODE == "private" else "移入私密模式")
        self.partition_button.setVisible(bool(privacy.MODE))
        self.partition_button.clicked.connect(self.move_current_partition)
        self.detail_layout.addWidget(self.partition_button)
        self.detail_layout.addWidget(self.section_label("影片简介"))
        self.overview = QLabel()
        self.overview.setObjectName("overview")
        self.overview.setWordWrap(True)
        self.overview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_layout.addWidget(self.overview)
        self.cast_title = self.section_label("主演与角色")
        self.detail_layout.addWidget(self.cast_title)
        self.cast_widget = QWidget()
        self.cast_layout = QHBoxLayout(self.cast_widget)
        self.cast_layout.setContentsMargins(0, 0, 0, 0)
        self.cast_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.detail_layout.addWidget(self.cast_widget)
        self.detail_layout.addWidget(self.section_label("我的偏好"))
        self.favorite = QCheckBox("这是我喜欢的影片")
        self.favorite.toggled.connect(self.favorite_detail_changed)
        self.detail_layout.addWidget(self.favorite)
        edit_row = QHBoxLayout()
        self.watch = QComboBox()
        self.watch.addItem("还没看", "unwatched")
        self.watch.addItem("在看", "watching")
        self.watch.addItem("已看完", "watched")
        self.disposition = QComboBox()
        self.disposition.addItem("保留", "keep")
        self.disposition.addItem("待考虑", "review")
        self.disposition.addItem("待删除", "delete")
        self.watch.currentIndexChanged.connect(self.detail_organize_changed)
        self.disposition.currentIndexChanged.connect(self.detail_organize_changed)
        edit_row.addWidget(self.watch)
        edit_row.addWidget(self.disposition)
        self.detail_layout.addLayout(edit_row)
        self.file_info = QLabel()
        self.file_info.setObjectName("fileInfo")
        self.file_info.setWordWrap(True)
        self.file_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_layout.addWidget(self.file_info)
        self.detail_layout.addStretch()
        page_scroll.setWidget(page)
        self.detail_stack.addWidget(page_scroll)
        outer.addWidget(self.detail_stack)
        return panel

    @staticmethod
    def section_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def eventFilter(self, watched: QObject, event: Any) -> bool:
        if privacy.MODE == "private":
            if self.privacy_locking and event.type() == QEvent.Type.Show and isinstance(watched, QDialog):
                watched.hide()
                watched.reject()
                return True
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.reflow_library)
        elif watched is getattr(self, "detail_poster", None) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.refresh_detail_hero)
        return super().eventFilter(watched, event)

    def refresh_detail_hero(self) -> None:
        if not self.current_hero_source or self.current_movie_id is None:
            return
        movie = get_movie(self.current_movie_id)
        if movie:
            self.images.load(
                self.current_hero_source, self.detail_poster,
                QSize(max(330, self.detail_poster.width()), self.detail_poster.height()),
                movie.get("title", "影"), True, False, True,
            )

    def load_all(self) -> None:
        self.load_stats()
        self.load_actor_strip()
        self.load_movies()

    def load_stats(self) -> None:
        stats = dashboard_stats()
        self.bottom_stats.setText(
            f"共 {stats['total']} 部  ·  {stats['size_label']}  ·  喜欢 {stats['favorites']}  ·  "
            f"待补全 {stats['unmatched']}  ·  待处理 {stats['delete_count']}  ·  重复 {stats['duplicates']}"
        )

    def change_page(self, direction: int) -> None:
        self.movie_page = max(0, self.movie_page + direction)
        self.load_movies(reset_page=False)
        self.scroll.verticalScrollBar().setValue(0)

    def load_movies(self, reset_page: bool = True) -> None:
        if reset_page:
            self.movie_page = 0
        movies, total = query_movie_page(
            self.view, self.search.text().strip(), self.sort, actor=self.actor_filter,
            favorite_filter=self.favorite_filter_combo.currentData(),
            rating_filter=self.rating_filter_combo.currentData(),
            limit=self.page_size, offset=self.movie_page * self.page_size,
        )
        if not movies and total and self.movie_page:
            self.movie_page = (total - 1) // self.page_size
            return self.load_movies(reset_page=False)
        self.cards = []
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for movie in movies:
            card = MovieCard(movie, self.images)
            card.opened.connect(self.show_movie)
            card.favorite_changed.connect(self.quick_favorite)
            card.delete_requested.connect(self.delete_movie_from_library)
            self.cards.append(card)
        self.reflow_cards()
        self.result_label.setText(f"共 {total} 部影片")
        self.page_label.setText(f"{self.movie_page + 1} / {max(1, (total + self.page_size - 1) // self.page_size)} 页")
        self.previous_page.setEnabled(self.movie_page > 0)
        self.next_page.setEnabled((self.movie_page + 1) * self.page_size < total)
        self.pagination.setVisible(total > self.page_size)
        self.grid_widget.setVisible(bool(movies))
        self.empty.setVisible(not movies)
        self.update_filter_state()
        self.reflow_filters()
        QTimer.singleShot(0, self.update_sticky_header)

    def load_actor_strip(self) -> None:
        while self.actor_layout.count():
            item = self.actor_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.actor_chips = []
        compact_position = self.compact_actor_scroll.horizontalScrollBar().value()
        while self.compact_actor_layout.count():
            item = self.compact_actor_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.compact_actor_chips = []
        actors = query_actor_facets()
        if self.actor_filter and not any(actor["name"] == self.actor_filter for actor in actors) and not discovery.actor_profile(self.actor_filter):
            self.actor_filter = ""
        for actor in actors:
            chip = ActorChip(actor, self.images, actor["name"] == self.actor_filter)
            chip.clicked.connect(self.actor_clicked)
            chip.photo_requested.connect(lambda name, position: self.show_actor_profile(name))
            chip.search_requested.connect(self.search_actor)
            self.actor_chips.append(chip)
            compact = CompactActorChip(actor, self.images, actor["name"] == self.actor_filter)
            compact.clicked.connect(self.actor_clicked)
            self.compact_actor_chips.append(compact)
            self.compact_actor_layout.addWidget(compact)
        self.compact_actor_container.setMinimumWidth(len(actors) * 80)
        self.compact_actor_scroll.setVisible(bool(actors))
        self.compact_actor_layout.activate()
        self.compact_actor_scroll.horizontalScrollBar().setValue(compact_position)
        self.reflow_actor_chips()
        self.actor_scroll.setVisible(bool(actors))
        self.actor_expand.setVisible(len(actors) > 12)
        self.actor_expand.setText("收起" if self.actors_expanded else f"展开多行 · {len(actors)} 位")
        selected_actor = next((actor for actor in actors if actor["name"] == self.actor_filter), None)
        self.active_actor.setText(
            f"已选：{selected_actor['display_name']}" if selected_actor else ""
        )
        self.clear_actor_button.setVisible(bool(selected_actor))

    def reflow_actor_chips(self) -> None:
        if not hasattr(self, "actor_layout"):
            return
        while self.actor_layout.count():
            self.actor_layout.takeAt(0)
        chips = getattr(self, "actor_chips", [])
        if not self.actors_expanded:
            for index, chip in enumerate(chips):
                self.actor_layout.addWidget(chip, 0, index)
            self.actor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.actor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.actor_container.setMinimumSize(max(0, len(chips) * 136), 180)
            self.actor_scroll.setFixedHeight(184)
            return
        available = max(136, self.actor_scroll.viewport().width() - 8)
        columns = max(1, available // 136)
        for index, chip in enumerate(chips):
            self.actor_layout.addWidget(chip, index // columns, index % columns)
        rows = max(1, (len(chips) + columns - 1) // columns)
        self.actor_container.setMinimumSize(0, rows * 190)
        self.actor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.actor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.actor_scroll.setFixedHeight(rows * 190 + 4)

    def search_actor(self, name: str) -> None:
        try:
            template = actor_search.load_template()
            url = actor_search.search_url(template, name)
        except (ValueError, OSError, core.sqlite3.Error) as exc:
            QMessageBox.warning(self, "请检查搜索网址", f"{exc}\n请到左侧的系统设置中修改演员搜索网址。")
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(self, "未能打开浏览器", "请检查系统默认浏览器设置后重试。")

    def actor_clicked(self, name: str) -> None:
        self.actor_filter = "" if self.actor_filter == name else name
        self.load_actor_strip()
        self.load_movies()

    def clear_actor_filter(self) -> None:
        if not self.actor_filter:
            return
        self.actor_filter = ""
        self.load_actor_strip()
        self.load_movies()

    def toggle_actors(self) -> None:
        self.actors_expanded = not self.actors_expanded
        self.load_actor_strip()

    def refresh_actor_information(self) -> None:
        if core.ACTOR_ENRICH.snapshot().get("running"):
            self.statusBar().showMessage("演员资料正在更新，请稍候", 2500)
            return
        keys = [core.normalize_actor_name(self.actor_filter)] if self.actor_filter else None
        target = "当前演员" if keys else "全部演员"
        self.actor_photo_reopen = ""
        self.start_actor_refresh(keys, target, force=False)

    def start_actor_refresh(self, keys: list[str] | None, target: str, *, force: bool = False) -> None:
        if core.ACTOR_ENRICH.snapshot().get("running"):
            self.statusBar().showMessage("演员资料正在更新，请稍候", 2500)
            return
        self.actor_refresh.setEnabled(False)
        self.actor_refresh.setText("…")
        action = "强制重新匹配" if force else "补全缺少头像的"
        self.actor_update_status.setText(f"演员资料：正在准备{action}{target}…")
        self.actor_status_timer.start(250)
        self.statusBar().showMessage(f"正在{action}{target}资料…")
        self.run_task(
            lambda: core.refresh_actor_profiles(keys, only_missing=not force),
            self.actor_refresh_finished, self.actor_refresh_failed,
        )

    def show_actor_profile(self, name: str) -> None:
        self.content_stack.setCurrentIndex(1)
        self.actor_library.show_actor(name)
        for button in self.nav_group.buttons():
            if button.property("viewKey") == "actors":
                button.setChecked(True)

    def view_actor_films(self, name: str) -> None:
        self.actor_filter = name
        self.set_view("all")
        self.load_actor_strip()
        for button in self.nav_group.buttons():
            if button.property("viewKey") == "all":
                button.setChecked(True)

    def open_actor_movie(self, movie_id: int) -> None:
        self.view_actor_films(self.actor_library.actor['name'])
        self.show_movie(movie_id)

    def refresh_movie_keywords(self) -> None:
        movie_id = self.current_movie_id
        if movie_id is None:
            return
        self.keyword_refresh.setEnabled(False)
        self.statusBar().showMessage("正在读取作品公开关键词与评论样本…")
        def complete(result):
            if self.current_movie_id == movie_id:
                self.movie_keywords.set_keywords(result['keywords'])
                self.keyword_refresh.setEnabled(True)
                count = result.get('review_sample_count', 0)
                self.statusBar().showMessage(f"关键词已更新 · 取得 {count} 条公开评论样本" if count else "公开类型与题材已更新；暂未取得评论样本，未生成评价标签。", 8000)
        def failed(message):
            if self.current_movie_id == movie_id:
                self.keyword_refresh.setEnabled(True)
                self.statusBar().showMessage(message, 10000)
        self.run_task(lambda: discovery.refresh_movie(movie_id), complete, failed)

    def open_actor_photo_dialog(self, actor_name: str, position: Any = None) -> None:
        requested_key = core.normalize_actor_name(actor_name)
        with core.connect() as conn:
            profile_rows = conn.execute("SELECT * FROM actor_profiles").fetchall()
        profile_row = next((row for row in profile_rows if row["name_key"] == requested_key), None)
        if not profile_row:
            profile_row = next((
                row for row in profile_rows
                if any(
                    core.normalize_actor_name(alias) == requested_key
                    for alias in core.unique_actor_names([row["name"], *core.json_value(row["aliases_json"], [])])
                )
            ), None)
        profile = dict(profile_row) if profile_row else {}
        name_key = str(profile.get("name_key") or requested_key)
        display_name = str(profile.get("display_name") or actor_name)
        photos = core.actor_photo_choices(name_key)
        if not photos:
            answer = QMessageBox.question(
                self, "还没有候选照片",
                f"{display_name} 还没有已保存的照片。现在联网查找吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.actor_photo_reopen = actor_name
                self.start_actor_refresh([name_key], display_name, force=True)
            return
        dialog = ActorPhotoDialog(actor_name, display_name, photos, self.images, profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.request_refresh:
            self.actor_photo_reopen = actor_name
            self.start_actor_refresh([name_key], display_name, force=True)
            return
        if not dialog.selected_url:
            return
        try:
            core.select_actor_photo(name_key, dialog.selected_url)
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.images.cache.clear()
        self.load_actor_strip()
        if self.current_movie_id is not None:
            self.show_movie(self.current_movie_id)
        self.statusBar().showMessage(f"已切换 {display_name} 的头像", 2600)

    def actor_refresh_finished(self, result: dict[str, Any]) -> None:
        self.actor_status_timer.stop()
        self.actor_refresh.setEnabled(True)
        self.actor_refresh.setText("↻")
        self.images.cache.clear()
        self.load_actor_strip()
        if self.current_movie_id is not None:
            self.show_movie(self.current_movie_id)
        updated = (
            int(result.get("avatar_updated", 0)) + int(result.get("name_updated", 0))
            + int(result.get("photos_added", 0))
        )
        if int(result.get("processed", 0)) == 0 and int(result.get("skipped", 0)):
            skipped = int(result.get("skipped", 0))
            self.actor_update_status.setText(f"无需刷新：{skipped} 位演员已有头像；右键头像可强制刷新")
            self.statusBar().showMessage("已有头像的演员已跳过，没有重复联网匹配", 4000)
            return
        self.actor_update_status.setText(
            f"演员资料更新完成：检查 {result.get('processed', 0)} 位 · 头像 {result.get('avatar_updated', 0)} · "
            f"中文名 {result.get('name_updated', 0)} · 新照片 {result.get('photos_added', 0)} · "
            f"未找到 {result.get('failed', 0)} · 跳过已有头像 {result.get('skipped', 0)}"
        )
        message = (
            f"已检查 {result.get('processed', 0)} 位演员。\n\n"
            f"头像更新：{result.get('avatar_updated', 0)}\n"
            f"中文姓名更新：{result.get('name_updated', 0)}\n"
            f"新增候选照片：{result.get('photos_added', 0)}\n"
            f"资料无变化：{result.get('unchanged', 0)}\n"
            f"未找到新资料：{result.get('failed', 0)}"
        )
        if not updated:
            message += "\n\n现有头像和姓名均已保留。"
        QMessageBox.information(self, "演员资料刷新完成", message)
        reopen = getattr(self, "actor_photo_reopen", "")
        self.actor_photo_reopen = ""
        if reopen:
            QTimer.singleShot(50, lambda name=reopen: self.open_actor_photo_dialog(name))

    def actor_refresh_failed(self, message: str) -> None:
        self.actor_status_timer.stop()
        self.actor_refresh.setEnabled(True)
        self.actor_refresh.setText("↻")
        self.actor_photo_reopen = ""
        self.actor_update_status.setText("演员资料更新未完成：现有姓名和头像均已保留")
        QMessageBox.warning(self, "演员资料刷新未完成", f"{message}\n\n现有资料没有被清空。")

    def filter_changed(self) -> None:
        self.load_movies()

    def update_filter_state(self) -> None:
        active = bool(
            self.actor_filter or self.search.text().strip()
            or self.favorite_filter_combo.currentData() != "any"
            or self.rating_filter_combo.currentData() != "any"
        )
        self.reset_filters_button.setVisible(active)

    def reset_filters(self) -> None:
        self.actor_filter = ""
        self.search.blockSignals(True)
        self.favorite_filter_combo.blockSignals(True)
        self.rating_filter_combo.blockSignals(True)
        self.search.clear()
        self.favorite_filter_combo.setCurrentIndex(0)
        self.rating_filter_combo.setCurrentIndex(0)
        self.search.blockSignals(False)
        self.favorite_filter_combo.blockSignals(False)
        self.rating_filter_combo.blockSignals(False)
        self.load_actor_strip()
        self.load_movies()

    def reflow_cards(self) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        width = max(184, self.library_scroll.viewport().width() - 62)
        columns = max(1, (width + 15) // 199)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)
        rows = (len(self.cards) + columns - 1) // columns
        card_height = max((card.sizeHint().height() for card in self.cards), default=0)
        self.grid_widget.setFixedHeight(rows * card_height + max(0, rows - 1) * 18)
        self.grid.activate()

    def build_sticky_header(self):
        self.is_sticky = False
        self.sticky_header = QFrame(self.library_scroll.viewport())
        self.sticky_header.setObjectName("stickyHeader")
        self.sticky_layout = QVBoxLayout(self.sticky_header)
        self.sticky_layout.setContentsMargins(34, 6, 28, 8)
        self.sticky_layout.setSpacing(5)
        self.compact_actor_scroll = NativeScrollArea(horizontal_only=True)
        self.compact_actor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.compact_actor_scroll.setWidgetResizable(True)
        self.compact_actor_scroll.setFixedHeight(76)
        self.compact_actor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_background(self.compact_actor_scroll.viewport(), "#101115")
        self.compact_actor_container = QWidget()
        set_background(self.compact_actor_container, "#101115")
        self.compact_actor_layout = QHBoxLayout(self.compact_actor_container)
        self.compact_actor_layout.setContentsMargins(0, 0, 0, 0)
        self.compact_actor_layout.setSpacing(4)
        self.compact_actor_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.compact_actor_chips = []
        self.compact_actor_scroll.setWidget(self.compact_actor_container)
        self.sticky_layout.addWidget(self.compact_actor_scroll)
        self.sticky_header.hide()
        self.reflow_filters()

    def reflow_filters(self):
        if not hasattr(self, "filter_bar") or not hasattr(self, "library_scroll"):
            return
        width = max(190, self.library_scroll.viewport().width() - 62)
        self.filter_bar.setFixedWidth(width)
        while self.filter_layout.count():
            self.filter_layout.takeAt(0)
        self.filter_layout.addWidget(self.result_label, 0, 0)
        self.filter_layout.addWidget(self.auto_match_button, 0, 1, Qt.AlignmentFlag.AlignRight)
        controls = [self.favorite_filter_combo, self.rating_filter_combo, self.sort_combo]
        if not self.reset_filters_button.isHidden():
            controls.append(self.reset_filters_button)
        columns = max(1, min(4, width // 112))
        # Nesting just the filter controls lets their columns differ from the heading.
        if not hasattr(self, "filter_controls"):
            self.filter_controls = QWidget()
            self.filter_controls_layout = QGridLayout(self.filter_controls)
            self.filter_controls_layout.setContentsMargins(0, 0, 0, 0)
            self.filter_controls_layout.setSpacing(6)
        while self.filter_controls_layout.count():
            self.filter_controls_layout.takeAt(0)
        for index, control in enumerate(controls):
            self.filter_controls_layout.addWidget(control, index // columns, index % columns)
        self.filter_layout.addWidget(self.filter_controls, 1, 0, 1, 2)
        self.filter_controls_layout.activate()
        self.filter_layout.activate()
        height = self.filter_bar.sizeHint().height()
        self.filter_bar.setFixedHeight(height)
        self.filter_slot.setFixedHeight(height)

    def reflow_library(self):
        self.reflow_filters()
        self.reflow_cards()
        self.reflow_actor_chips()
        self.update_sticky_header()

    def update_sticky_header(self, *_):
        if not hasattr(self, "sticky_header"):
            return
        actor_height = 81 if self.compact_actor_chips else 0
        threshold = max(1, self.filter_slot.y() - actor_height - 6)
        pinned = self.library_scroll.verticalScrollBar().value() >= threshold
        if pinned != self.is_sticky:
            self.is_sticky = pinned
            if pinned:
                self.filter_slot.layout().removeWidget(self.filter_bar)
                self.sticky_layout.addWidget(self.filter_bar)
                self.filter_bar.show()
                self.sticky_header.show()
            else:
                self.sticky_layout.removeWidget(self.filter_bar)
                self.filter_slot.layout().addWidget(self.filter_bar)
                self.filter_bar.show()
                self.sticky_header.hide()
        # Keep the compact context when a filter returns only a few films.
        # Scrolling back above the threshold still restores the full actor area.
        minimum = self.library_scroll.viewport().height() + threshold if pinned else 0
        if self.library_panel.minimumHeight() != minimum:
            self.library_panel.setMinimumHeight(minimum)
        if pinned:
            height = actor_height + self.filter_bar.height() + 14
            self.sticky_header.setGeometry(0, 0, self.library_scroll.viewport().width(), height)
            self.sticky_header.raise_()

    def set_view(self, view: str) -> None:
        for button in self.nav_group.buttons():
            if button.property("viewKey") == view:
                button.setChecked(True)
        if view == "actors":
            self.content_stack.setCurrentIndex(1)
            self.actor_library.go_back()
            return
        self.content_stack.setCurrentIndex(0)
        self.view = view
        labels = {
            "all": ("我的全部影片", "先看清自己的收藏，再决定留下什么。"),
            "favorite": ("我的喜欢", "这些影片正在慢慢勾勒你的口味。"),
            "unwatched": ("还没看过", "从收藏中挑一部，今晚就看。"),
            "unmatched": ("待匹配资料", "补上封面、简介和主演信息。"),
            "duplicates": ("重复影片", "核对相同内容，腾出硬盘空间。"),
            "delete": ("待处理影片", "这里只是安全清单，不会自动删除文件。"),
        }
        title, subtitle = labels.get(view, labels["all"])
        self.heading.setText(title)
        self.subtitle.setText(subtitle)
        self.load_movies()

    def sort_changed(self) -> None:
        self.sort = self.sort_combo.currentData()
        self.load_movies()

    def show_movie(self, movie_id: int) -> None:
        movie = get_movie(movie_id)
        if not movie:
            return
        changed_movie = self.current_movie_id != movie_id
        self.current_movie_id = movie_id
        self.detail_stack.setCurrentIndex(1)
        self.current_hero_source = movie.get("backdrop_url") or movie.get("poster", "")
        self.refresh_detail_hero()
        self.detail_title.setText(movie.get("title", ""), reset=changed_movie)
        self.detail_title.setToolTip(movie.get("title", ""))
        genres = " · ".join((movie.get("genres") or [])[:3])
        self.detail_meta.setText(" · ".join(str(x) for x in (movie.get("year"), genres, movie.get("source", "local").upper()) if x))
        self.movie_keywords.set_keywords(discovery.movie_insights(movie)['keywords'])
        self.keyword_refresh.setEnabled(True)
        if movie.get('source') == 'tmdb' and core.setting_value('tmdb_token') and not core.json_value(movie.get('insights_json'), {}).get('fetched_at') and movie['id'] not in self._keyword_attempted:
            self._keyword_attempted.add(movie['id'])
            QTimer.singleShot(180, lambda mid=movie['id']: self.refresh_movie_keywords() if self.current_movie_id == mid else None)
        self.overview.setText(movie.get("overview") or "暂无简介。点击“联网匹配”补充资料。")
        self.loading_detail = True
        self.rating.set_rating(round(float(movie.get("personal_rating") or 0) / 2))
        self.favorite.setChecked(bool(movie.get("favorite")))
        self.set_combo(self.watch, movie.get("watch_status", "unwatched"))
        self.set_combo(self.disposition, movie.get("disposition", "keep"))
        self.loading_detail = False
        status_text = {
            "matched": "已匹配", "manual": "手工资料", "review": "候选待确认",
            "no_match": "未找到资料", "unmatched": "尚未匹配",
        }.get(movie.get("match_status"), movie.get("match_status", ""))
        confidence = float(movie.get("match_confidence") or 0)
        match_line = f"匹配：{status_text}" + (f" · {confidence:.0%}" if confidence else "")
        if movie.get("match_note"):
            match_line += f"\n说明：{movie['match_note']}"
        self.file_info.setText(
            f"文件：{movie.get('filename','')}\n位置：{movie.get('path','')}\n"
            f"大小：{movie.get('size_label','')}  ·  时长：{format_duration(movie.get('duration_seconds') or 0)}\n"
            f"播放次数：{int(movie.get('play_count') or 0)}  ·  最近播放："
            f"{(movie.get('last_played_at') or '尚未播放').replace('T', ' ')[:19]}\n"
            f"入库时间：{(movie.get('created_at') or '').replace('T', ' ')[:19]}\n{match_line}"
        )
        self.match_button.setText("重匹配" if movie.get("match_status") == "matched" else "匹配")
        while self.cast_layout.count():
            item = self.cast_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        cast = (movie.get("cast") or [])[:5]
        display_names = actor_display_name_map()
        self.cast_title.setVisible(bool(cast))
        self.cast_widget.setVisible(bool(cast))
        for person in cast:
            box = QWidget()
            box.setFixedWidth(78)
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0)
            avatar = PosterLabel()
            avatar.setFixedSize(64, 64)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            avatar.setStyleSheet("border-radius:12px;background:#ddd8cf;")
            original_name = person.get("name") or "未知"
            avatar.right_clicked.connect(
                lambda position, actor_name=original_name: self.open_actor_photo_dialog(actor_name, position)
            )
            shown_name = display_names.get(core.normalize_actor_name(original_name), original_name)
            self.images.load(person.get("avatar", ""), avatar, avatar.size(), shown_name, True, True, True)
            name = QLabel(shown_name)
            name.setObjectName("personName")
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name.setWordWrap(True)
            lay.addWidget(avatar, 0, Qt.AlignmentFlag.AlignHCenter)
            lay.addWidget(name)
            self.cast_layout.addWidget(box)
        sources = self.load_screenshots(movie)
        if not sources and movie.get("screenshots_status") != "failed":
            QTimer.singleShot(180, lambda movie_id=movie_id: self.ensure_current_screenshots(movie_id))

    @staticmethod
    def set_combo(combo: QComboBox, data: str) -> None:
        index = combo.findData(data)
        combo.setCurrentIndex(max(0, index))

    def quick_favorite(self, movie_id: int, value: bool) -> None:
        update_movie(movie_id, favorite=value)
        self.load_all()
        if self.current_movie_id == movie_id:
            self.show_movie(movie_id)

    def rating_changed(self, stars: int) -> None:
        if self.loading_detail or self.current_movie_id is None:
            return
        update_movie(self.current_movie_id, personal_rating=stars * 2)
        self.load_stats()
        self.load_movies()
        self.statusBar().showMessage(f"评分已自动保存：{stars} 星", 1800)

    def favorite_detail_changed(self, value: bool) -> None:
        if self.loading_detail or self.current_movie_id is None:
            return
        update_movie(self.current_movie_id, favorite=value)
        self.load_stats()
        self.load_actor_strip()
        self.load_movies()
        self.statusBar().showMessage("喜欢状态已自动保存", 1800)

    def detail_organize_changed(self) -> None:
        if self.loading_detail or self.current_movie_id is None:
            return
        update_movie(
            self.current_movie_id,
            watch_status=self.watch.currentData(), disposition=self.disposition.currentData(),
        )
        self.load_stats()
        self.load_movies()
        self.statusBar().showMessage("整理状态已自动保存", 1800)

    def current_movie(self) -> dict[str, Any] | None:
        return get_movie(self.current_movie_id) if self.current_movie_id is not None else None

    def restore_video_player(self) -> None:
        self.run_task(
            player.restore_video_defaults,
            lambda count: self.statusBar().showMessage(f"已将 {count} 种视频文件的默认播放器恢复为 IINA", 5000) if count else None,
            lambda message: self.statusBar().showMessage(message, 8000),
        )

    def play_current(self) -> None:
        movie = self.current_movie()
        if movie and Path(movie["path"]).is_file():
            try:
                player.play_movie(movie["path"])
            except RuntimeError as exc:
                self.show_error(str(exc))
                return
            record_movie_play(movie["id"])
            self.show_movie(movie["id"])
            self.load_stats()
        else:
            self.show_error("影片文件暂时无法访问，可检查硬盘或重新关联文件夹。")

    def open_current_folder(self) -> None:
        movie = self.current_movie()
        if movie and Path(movie["path"]).parent.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(movie["path"]).parent)))

    def delete_current_file(self) -> None:
        movie = self.current_movie()
        if not movie:
            return
        self.delete_movie_from_library(movie["id"])

    def delete_movie_from_library(self, movie_id: int) -> None:
        movie = get_movie(movie_id)
        if not movie:
            return
        answer = QMessageBox.warning(
            self, "移到回收站",
            f"确定把这部影片移到 回收站？\n\n{movie.get('title','')}\n{movie.get('path','')}\n\n"
            "影库会移除这条影片记录；同目录的字幕、NFO 和封面不会删除。"
            "如果这是该目录最后一部影片，影库会恢复目录原来的显示属性。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted = core.recycle_movie_file(movie["id"])
        except (OSError, ValueError) as exc:
            self.show_error(str(exc))
            return
        if self.current_movie_id == movie_id:
            self.current_movie_id = None
            self.detail_stack.setCurrentIndex(0)
        self.load_all()
        self.statusBar().showMessage(f"已移到回收站：{Path(deleted).name}", 5000)

    @staticmethod
    def screenshot_dir(movie: dict[str, Any]) -> Path:
        key = movie.get("fingerprint") or hashlib.sha1(movie.get("path", "").encode("utf-8")).hexdigest()[:20]
        return core.DATA_DIR / "screenshots" / key

    def screenshot_sources(self, movie: dict[str, Any]) -> list[str]:
        local = sorted(str(path) for path in self.screenshot_dir(movie).glob("frame_*.jpg"))
        return (local if local else list(movie.get("screenshots") or []))[:10]

    def load_screenshots(self, movie: dict[str, Any]) -> list[str]:
        while self.screenshot_layout.count():
            item = self.screenshot_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        sources = self.screenshot_sources(movie)
        if not sources:
            if self.extraction_running and self.extraction_movie_id == movie["id"]:
                message = "正在后台提取关键画面，完成后会自动显示…"
            elif movie.get("screenshots_status") == "failed":
                message = "自动生成未成功，可点击“重试生成”。"
            else:
                message = "正在准备自动生成影片内容速览…"
            empty = QLabel(message)
            empty.setObjectName("muted")
            self.screenshot_layout.addWidget(empty, 0, 0, 2, 1)
        for index, source in enumerate(sources):
            thumb = ScreenshotLabel(source)
            thumb.setFixedSize(190, 108)
            thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            thumb.setToolTip(f"关键截图 {index + 1} · 点击放大")
            thumb.setStyleSheet("border-radius:6px;background:#25262b;")
            thumb.clicked.connect(self.preview_screenshot)
            self.images.load(source, thumb, thumb.size(), str(index + 1), True)
            self.screenshot_layout.addWidget(thumb, index % 2, index // 2)
        if self.extraction_running and self.extraction_movie_id == movie["id"]:
            self.extract_button.setText("生成中…")
            self.extract_button.setEnabled(False)
        else:
            failed = movie.get("screenshots_status") == "failed"
            self.extract_button.setText(
                "重新生成" if any(Path(s).is_file() for s in sources) else ("重试生成" if failed else "生成约 10 张")
            )
            self.extract_button.setEnabled(True)
        columns = max(1, (len(sources) + 1) // 2)
        content_width = columns * 190 + max(0, columns - 1) * 9
        self.screenshot_container.setFixedSize(
            max(self.screenshot_scroll.viewport().width(), content_width),
            225,
        )
        return sources

    def preview_screenshot(self, source: str) -> None:
        movie = self.current_movie()
        sources = self.screenshot_sources(movie) if movie else [source]
        PreviewDialog(sources, source, self.images, self).exec()

    def ensure_current_screenshots(self, movie_id: int) -> None:
        if self.current_movie_id != movie_id or self.extraction_running:
            return
        movie = get_movie(movie_id)
        if not movie or self.screenshot_sources(movie) or movie.get("screenshots_status") == "failed":
            return
        self.extract_screenshots(manual=False)

    def extract_screenshots(self, checked: bool = False, manual: bool = True) -> None:
        movie = self.current_movie()
        if not movie or not Path(movie.get("path", "")).is_file():
            if manual:
                self.show_error("影片文件暂时无法访问，可检查硬盘或重新关联文件夹。")
            return
        if self.extraction_running:
            return
        self.extraction_running = True
        self.extraction_manual = manual
        self.extract_button.setEnabled(False)
        self.extract_button.setText("生成中…")
        self.extraction_movie_id = movie["id"]
        update_screenshot_status(movie["id"], "running")
        self.statusBar().showMessage("正在本地提取并筛选关键截图…")
        self.run_task(
            lambda: extract_keyframes(movie["path"], self.screenshot_dir(movie)),
            self.extraction_finished, self.extraction_failed,
        )

    def extraction_finished(self, files: list[str]) -> None:
        finished_id = self.extraction_movie_id
        if finished_id is not None:
            update_screenshot_status(finished_id, "ready")
        self.extraction_running = False
        movie = self.current_movie()
        if movie and movie["id"] == finished_id:
            self.load_screenshots(movie)
        self.statusBar().showMessage(f"已生成 {len(files)} 张不同画面的关键截图", 5000)
        if movie and movie["id"] != finished_id:
            QTimer.singleShot(150, lambda movie_id=movie["id"]: self.ensure_current_screenshots(movie_id))

    def extraction_failed(self, message: str) -> None:
        failed_id = self.extraction_movie_id
        if failed_id is not None:
            update_screenshot_status(failed_id, "failed")
        was_manual = self.extraction_manual
        self.extraction_running = False
        movie = self.current_movie()
        if movie and movie["id"] == failed_id:
            refreshed = get_movie(movie["id"])
            if refreshed:
                self.load_screenshots(refreshed)
        if was_manual:
            self.show_error(message + "\n\n你仍可以通过编号资料库获取在线预览图，或手工添加封面。")
        else:
            self.statusBar().showMessage("这部影片未能自动生成截图，可在详情中手工重试", 5000)
        if movie and movie["id"] != failed_id:
            QTimer.singleShot(150, lambda movie_id=movie["id"]: self.ensure_current_screenshots(movie_id))

    def open_maintenance(self) -> None:
        if self.pool.activeThreadCount():
            QMessageBox.information(self, "后台任务进行中", "请等扫描、资料匹配或截图任务完成后，再备份、恢复或重新关联。")
            return
        from maintenance_ui import MaintenanceDialog
        self.save_ui_state()
        self.maintenance_active = True
        dialog = MaintenanceDialog(self)
        restore_folder = []
        dialog.restore_requested.connect(restore_folder.append)
        try:
            dialog.exec()
        finally:
            self.maintenance_active = self.privacy_locking
        if self.privacy_locking:
            return
        if restore_folder:
            self.restart_for_restore(restore_folder[0])
        elif dialog.changed:
            self.images.cache.clear()
            self.load_all()
            if self.current_movie_id:
                self.show_movie(self.current_movie_id)

    def restart_for_restore(self, folder: str) -> None:
        self.maintenance_active = True
        self.save_ui_state()
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve()))
        command.append("--private" if privacy.MODE == "private" else "--public")
        command += ["--restore-backup", folder, "--wait-for-pid", str(os.getpid())]
        try:
            subprocess.Popen(command, start_new_session=True)
        except OSError as exc:
            self.maintenance_active = False
            self.show_error(f"无法重启恢复，当前资料保持不变：{exc}")
            return
        self.relaunching = True
        self.close()
        QApplication.instance().quit()

    def open_scan(self) -> None:
        if core.SCAN.snapshot().get("running"):
            QMessageBox.information(self, "正在同步", "影库正在后台检查已管理位置，请稍候再打开扫描源管理。")
            return
        dialog = ScanDialog(self)
        result = dialog.exec()
        if dialog.library_changed:
            if self.current_movie_id is not None and not get_movie(self.current_movie_id):
                self.current_movie_id = None
                self.detail_stack.setCurrentIndex(0)
            self.load_all()
        if result != QDialog.DialogCode.Accepted:
            return
        roots = dialog.selected_roots()
        self.progress_frame.show()
        self.progress.setRange(0, 0)
        self.progress_label.setText("正在建立影片索引…")
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self.update_scan_progress)
        self.scan_timer.start(350)
        self.run_task(lambda: core.scan_roots(roots), self.scan_finished)

    def start_duration_filter(self) -> None:
        if self.maintenance_active:
            return
        if core.DURATION_FILTER.snapshot().get("running"):
            return
        threshold = core.duration_threshold_seconds() // 60
        if not threshold:
            return
        self.statusBar().showMessage(f"正在复查并过滤短于 {threshold} 分钟的内容…")
        self.run_task(core.enforce_duration_threshold, self.duration_filter_finished, self.duration_filter_failed)

    def duration_filter_finished(self, result: dict[str, Any]) -> None:
        removed = int(result.get("removed", 0))
        if removed:
            if self.current_movie_id is not None and not get_movie(self.current_movie_id):
                self.current_movie_id = None
                self.current_hero_source = ""
                self.detail_stack.setCurrentIndex(0)
            self.load_all()
            self.statusBar().showMessage(
                f"启动检查完成：已从影库过滤 {removed} 个短于 {result.get('threshold_minutes', 10)} 分钟的内容",
                6000,
            )
        else:
            self.statusBar().showMessage("短片阈值检查完成", 2200)

    def duration_filter_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"短片阈值检查未完成：{message}", 5000)

    def update_scan_progress(self) -> None:
        state = core.SCAN.snapshot()
        current = Path(state.get("current", "")).name
        self.progress_label.setText(f"已发现 {state.get('processed',0)} 个影片文件  {current}")

    def scan_finished(self, _: Any) -> None:
        if hasattr(self, "scan_timer"):
            self.scan_timer.stop()
        state = core.SCAN.snapshot()
        self.progress_frame.hide()
        self.statusBar().showMessage(
            f"扫描完成：新建或更新 {state.get('found',0)} 部，隐藏 {state.get('hidden',0)} 部，"
            f"隐藏目录 {state.get('folders_hidden',0)} 个，文件待关联 {state.get('removed',0)} 部，"
            f"过滤短内容 {state.get('filtered',0)} 部",
            7000,
        )
        if state.get("errors"):
            self.show_error("部分位置无法读取：\n" + "\n".join(state["errors"][:4]))
        self.load_all()
        if core.setting_value("auto_match_after_scan") != "false" and core.auto_match_pending_count():
            QTimer.singleShot(500, self.start_auto_match)
        else:
            QTimer.singleShot(500, self.start_actor_enrichment)

    def start_startup_sync(self) -> None:
        if self.maintenance_active:
            return
        if core.SCAN.snapshot().get("running"):
            return
        roots = [source["path"] for source in core.list_scan_roots()]
        if not roots:
            QTimer.singleShot(150, self.start_duration_filter)
            QTimer.singleShot(900, self.start_actor_enrichment)
            return
        self.statusBar().showMessage("正在检查已管理位置中的新增和已删除影片…")
        self.run_task(
            lambda: core.scan_roots(roots),
            self.startup_sync_finished,
            self.startup_sync_failed,
        )

    def startup_sync_finished(self, _: Any) -> None:
        state = core.SCAN.snapshot()
        if self.current_movie_id is not None and not get_movie(self.current_movie_id):
            self.current_movie_id = None
            self.current_hero_source = ""
            self.detail_stack.setCurrentIndex(0)
        self.load_all()
        unavailable = len(state.get("errors") or [])
        message = (
            f"启动同步完成：新增或更新 {state.get('found',0)} 部，隐藏 {state.get('hidden',0)} 部，"
            f"隐藏目录 {state.get('folders_hidden',0)} 个，文件待关联 {state.get('removed',0)} 部，"
            f"过滤短内容 {state.get('filtered',0)} 部"
        )
        if unavailable:
            message += f"；{unavailable} 个位置或文件暂时无法读取（未清除其记录）"
        self.statusBar().showMessage(message, 8000)
        if core.setting_value("auto_match_after_scan") != "false" and core.auto_match_pending_count():
            QTimer.singleShot(550, self.start_auto_match)
        else:
            QTimer.singleShot(700, self.start_actor_enrichment)

    def startup_sync_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"启动同步未完成：{message}；现有影库记录未主动清除", 7000)
        QTimer.singleShot(800, self.start_actor_enrichment)

    def start_auto_match(self) -> None:
        if self.maintenance_active:
            return
        if core.AUTO_MATCH.snapshot().get("running"):
            return
        unmatched = core.auto_match_pending_count()
        if not unmatched:
            self.statusBar().showMessage("已使用保存的匹配结果，没有需要重复查询的影片", 3000)
            QTimer.singleShot(200, self.start_actor_enrichment)
            return
        self.auto_match_button.setEnabled(False)
        self.auto_match_button.setText("匹配中…")
        self.progress_frame.show()
        self.progress.setRange(0, max(1, int(unmatched)))
        self.progress.setValue(0)
        self.progress_label.setText(f"准备自动匹配 {unmatched} 部影片…")
        self.match_timer = QTimer(self)
        self.match_timer.timeout.connect(self.update_auto_match_progress)
        self.match_timer.start(350)
        self.run_task(core.auto_match_library, self.auto_match_finished, self.auto_match_failed)

    def update_auto_match_progress(self) -> None:
        state = core.AUTO_MATCH.snapshot()
        total = max(1, int(state.get("total", 1)))
        processed = int(state.get("processed", 0))
        self.progress.setRange(0, total)
        self.progress.setValue(processed)
        self.progress_label.setText(
            f"自动匹配 {processed}/{total} · 已成功 {state.get('matched',0)} · {state.get('current','')}"
        )

    def auto_match_finished(self, result: dict[str, Any]) -> None:
        if hasattr(self, "match_timer"):
            self.match_timer.stop()
        self.auto_match_button.setEnabled(True)
        self.auto_match_button.setText("✨ 自动匹配")
        self.progress_frame.hide()
        self.progress.setRange(0, 0)
        self.load_all()
        self.statusBar().showMessage(
            f"自动匹配完成：成功 {result.get('matched',0)}，待确认 {result.get('review',0)}，未找到 {result.get('no_match',0)}",
            7000,
        )
        QTimer.singleShot(300, self.start_actor_enrichment)

    def auto_match_failed(self, message: str) -> None:
        if hasattr(self, "match_timer"):
            self.match_timer.stop()
        self.auto_match_button.setEnabled(True)
        self.auto_match_button.setText("✨ 自动匹配")
        self.progress_frame.hide()
        self.progress.setRange(0, 0)
        self.show_error("自动匹配未完成：" + message)
        QTimer.singleShot(300, self.start_actor_enrichment)

    def start_actor_enrichment(self) -> None:
        if self.maintenance_active:
            return
        if core.ACTOR_ENRICH.snapshot().get("running"):
            return
        self.statusBar().showMessage("正在读取已保存的演员资料；仅新演员会联网…")
        self.actor_update_status.setText("演员资料：正在检查新演员…")
        self.actor_status_timer.start(250)
        self.run_task(core.enrich_actor_avatars, self.actor_enrichment_finished, self.actor_enrichment_failed)

    def update_actor_enrichment_progress(self) -> None:
        state = core.ACTOR_ENRICH.snapshot()
        if not state.get("running"):
            return
        current = str(state.get("current") or "准备中")
        text = (
            f"正在获取：{current} · {state.get('processed',0)}/{state.get('total',0)} · "
            f"头像 {state.get('matched',0)} · 中文名 {state.get('localized',0)} · 未找到 {state.get('failed',0)}"
        )
        if state.get("last_result"):
            text += f" · 上一项 {state['last_result']}"
        self.actor_update_status.setText(text)

    def actor_enrichment_finished(self, result: dict[str, Any]) -> None:
        self.actor_status_timer.stop()
        matched = int(result.get("matched", 0))
        localized = int(result.get("localized", 0))
        self.load_actor_strip()
        if self.current_movie_id is not None:
            self.show_movie(self.current_movie_id)
        if matched or localized:
            self.actor_update_status.setText(f"演员资料完成：补全 {matched} 个头像、{localized} 个中文姓名")
            self.statusBar().showMessage(f"已补全 {matched} 位演员头像、{localized} 个中文姓名", 5000)
        elif int(result.get("total", 0)) == 0:
            self.actor_update_status.setText("演员资料：已使用本地缓存，没有重复联网")
            self.statusBar().showMessage("已使用本地演员缓存，没有重复联网匹配", 3000)
        else:
            self.actor_update_status.setText(
                f"演员资料检查完成：检查 {result.get('processed',0)} 位，未找到 {result.get('failed',0)} 位"
            )
            self.statusBar().showMessage("演员头像资料已检查", 2200)

    def actor_enrichment_failed(self, message: str) -> None:
        self.actor_status_timer.stop()
        self.actor_update_status.setText("演员资料检查暂未完成：现有资料已保留，稍后会自动重试")
        self.statusBar().showMessage("演员头像后台补全暂未完成，将在以后自动重试", 5000)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.statusBar().showMessage("系统设置已保存", 2500)
            QTimer.singleShot(200, self.start_actor_enrichment)

    def open_taste(self) -> None:
        TasteDialog(self).exec()

    def open_match(self) -> None:
        movie = self.current_movie()
        if not movie:
            return
        dialog = MatchDialog(movie, self.run_task, self)
        dialog.applied.connect(lambda: self.metadata_applied(movie["id"]))
        dialog.exec()

    def open_manual_edit(self) -> None:
        movie = self.current_movie()
        if not movie:
            return
        dialog = ManualEditDialog(movie, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.save()
            self.images.cache.clear()
            self.load_all()
            self.show_movie(movie["id"])
            self.statusBar().showMessage("手工资料已保存", 3000)
            QTimer.singleShot(250, self.start_actor_enrichment)

    def metadata_applied(self, movie_id: int) -> None:
        self.statusBar().showMessage("影片资料已匹配", 3000)
        self.load_all()
        self.show_movie(movie_id)
        QTimer.singleShot(250, self.start_actor_enrichment)

    def show_error(self, message: str) -> None:
        if self.privacy_locking:
            return
        QMessageBox.warning(self, "影库", message)


STYLE = """
* { font-family: 'Microsoft YaHei UI'; font-size: 13px; color:#e9e6df; }
QMainWindow, #libraryPanel { background:#101115; }
QSplitter::handle:horizontal { background:#2c2d33; margin:0 2px; }
QSplitter::handle:horizontal:hover { background:#b95749; }
#sidebar { background:#090a0d; border-right:1px solid #25262b; }
#brand { color:#f5f1e8; font-size:20px; font-weight:700; letter-spacing:3px; text-align:left; padding:0; border:0; background:transparent; }
#brand:hover, #brand:pressed { background:transparent; color:#f5f1e8; }
#brand:focus { color:#e8b9ad; }
#brandSub { color:#7c7f85; font-size:9px; letter-spacing:2px; margin-left:27px; }
QPushButton[nav='true'] { text-align:left; border:0; border-radius:7px; padding:12px 13px; color:#909399; background:transparent; }
QPushButton[nav='true']:hover { color:#f2f2f2; background:#1d1e22; }
QPushButton[nav='true']:checked { color:white; background:#2a2426; }
#sideAction { color:#eee; border:1px solid #383a3f; border-radius:7px; padding:11px; background:#1b1c20; }
#sideText { color:#888b91; border:0; padding:10px; background:transparent; }
#sideText[bottomMenu="true"] { text-align:left; padding:10px 13px; }
#sideText:hover { color:#eee; background:#1b1c20; }
#privacy { color:#666b70; font-size:10px; padding-top:18px; border-top:1px solid #2a2b2f; }
QLineEdit, QPlainTextEdit, QComboBox, QListWidget { background:#191a1f; color:#ece8e1; border:1px solid #34363d; border-radius:7px; padding:8px; selection-background-color:#d75d4d; }
#search { background:#191a1e; border:1px solid #292b31; padding:10px 13px; }
#search:focus { background:#202126; border-color:#9d5e55; }
QPushButton { background:#1a1b20; border:1px solid #383a42; border-radius:7px; padding:9px 13px; }
QPushButton:hover { background:#24252b; border-color:#555861; }
QPushButton#primary { color:white; background:#d65443; border:0; font-weight:600; }
QPushButton#primary:hover { background:#bc3f33; }
#matchAction { color:#e8b9ad; border-color:#6f3b35; background:#261b1b; }
#matchAction:hover { background:#352120; border-color:#9b5147; }
#dangerText { color:#e79a8e; border-color:#623a38; background:#251a1b; }
#dangerText:hover { color:white; background:#482321; border-color:#985149; }
#compactText { color:#9b9da3; border:0; background:transparent; padding:5px 8px; }
#compactText:hover { color:#f0ece5; background:#202126; }
#actorRefresh { color:#d9d5ce; font-size:20px; border:0; background:transparent; padding:2px 8px; }
#actorRefresh:hover { color:white; background:#27282e; border-radius:8px; }
#actorStripTitle { color:#bbb7af; font-size:11px; font-weight:600; }
#actorUpdateStatus { color:#969aa3; font-size:10px; padding:0 0 3px 1px; }
#activeFilter { color:#e18474; font-size:11px; padding-left:7px; }
#stickyHeader { background:#101115; border-bottom:1px solid #303139; }
#filmFilters, #compactActor { background:#101115; border:0; }
#compactActorName { border:0; padding:0; background:transparent; font-size:11px; color:#b9bbc2; }
#compactActorName:checked, #compactActorName:hover { color:#edaa9b; }
#compactActorName:focus { color:white; }
#actorChip { background:transparent; border:1px solid transparent; border-radius:9px; }
#actorChip[selected='true'] { background:#2b2021; border-color:#8e4b43; }
#actorName { color:#e5e1da; font-size:12px; font-weight:600; border:0; background:transparent; padding:0; }
#actorName:hover, #actorName:checked { color:#fff; background:transparent; }
#actorSearch { color:#c7c9cf; font-size:11px; background:#1b1c22; border:1px solid #373940; border-radius:6px; padding:2px 8px; }
#actorSearch:hover { color:#fff; background:#292b32; border-color:#636671; }
#actorSearch:focus { border-color:#d17e70; }
#searchHelp, #searchPreview { color:#afb1b8; font-size:12px; }
#actorStats { color:#8f9298; font-size:9px; }
#cardDelete { color:#8f9298; border:0; background:transparent; font-size:16px; }
#cardDelete:hover { color:#e18474; background:#302326; border-radius:6px; }
#previewArrow { color:#f1eee8; background:#292b31; border:1px solid #3a3c43; border-radius:24px; font-size:42px; }
#previewArrow:hover { background:#3a3031; border-color:#8e4b43; }
#previewArrow:disabled { color:#5f626a; background:#202126; border-color:#2a2b30; }
#previewCounter { color:#a9abb2; font-size:12px; padding-left:64px; }
#photoChoice { background:#1b1c21; border:2px solid transparent; border-radius:10px; }
#photoChoice:hover { background:#24252b; border-color:#4c4f58; }
#photoChoice[selected='true'] { background:#302120; border-color:#d65443; }
#photoSource { color:#8f9298; font-size:9px; }
#actorProfileInfo { color:#a7a39c; font-size:11px; background:#1b1c21; border-radius:8px; padding:10px; }
#eyebrow { color:#d65443; font-size:10px; font-weight:700; letter-spacing:3px; }
#heading { font-family:'Microsoft YaHei UI'; font-size:32px; font-weight:500; }
#muted, .muted { color:#8e8b85; }
#mutedSmall { color:#777a80; font-size:10px; }
#statCard { background:#17181d; border:1px solid #292b31; border-radius:9px; }
#statValue { font-family:Georgia; font-size:25px; }
#resultLabel { font-weight:600; padding:0; }
#actorLibraryName { text-align:left; color:#e9e6df; background:transparent; border:0; padding:0; font-size:14px; font-weight:600; }
#actorDetailName { font-size:30px; font-weight:650; color:#f5f1e8; }
#actorBiography { color:#c8c5bf; font-size:13px; }
#actorSection { color:#e9e6df; font-size:18px; font-weight:600; margin-top:17px; }
#actorSave { padding:6px 12px; color:#e9e6df; background:#24252b; border:1px solid #41434b; border-radius:7px; }
#actorSave:checked { color:#f2b4a7; background:#352422; border:1px solid #985b4c; }
#actorSave:hover { background:#34363d; }
#actorReason { color:#d8c8b5; font-size:11px; }
#actorWorkTitle { color:#e9e6df; font-size:12px; }
#insightTag { color:#dbd5c9; background:#26272c; font-size:11px; padding:4px 7px; border-radius:5px; }
#movieCard { background:transparent; border-radius:9px; }
#movieCard:hover { background:#1a1b20; }
#cardTitle { font-weight:600; }
#favoriteOn { color:#d65443; font-size:19px; border:0; }
#favoriteOff { color:#aaa69e; font-size:19px; border:0; }
QScrollArea { background:transparent; border:0; }
#empty { color:#7f7c75; font-size:15px; line-height:1.7; }
#detailPanel { background:#131419; border-left:1px solid #2b2c32; }
#emptyDetail { color:#99958f; font-size:14px; }
#detailHero { background:#22242a; border-radius:13px; }
#detailTitle { font-size:27px; font-weight:650; margin-top:3px; }
#titleToggle { color:#b9b4ad; background:transparent; border:0; padding:8px 2px; font-size:12px; }
#titleToggle:hover, #titleToggle:focus { color:#f5f1e8; background:#24252b; border-radius:4px; }
#titleRatingLabel { color:#a9a59e; font-size:11px; padding-right:4px; }
QPushButton[detailAction='true'] { padding:7px 9px; }
#sectionTitle { font-size:16px; font-weight:700; margin-top:10px; }
#overview { color:#aaa69f; line-height:1.6; }
#accent { color:#d65443; font-weight:700; }
#ratingStar { color:#565961; background:transparent; border:0; font-size:29px; padding:0 2px; }
#ratingStar:hover, #ratingStar[active='true'] { color:#f1ad42; }
#fileInfo { color:#8d8981; font-size:10px; background:#1c1d22; padding:10px; border-radius:7px; }
#personName { font-size:11px; }
#dialogTitle { font-size:25px; font-weight:600; margin-bottom:5px; }
#driveList::item { padding:5px; border-bottom:1px solid #ebe8e0; }
#warning { color:#a96a2c; }
#progressFrame { background:#1c1d22; border-radius:8px; padding:8px; }
QProgressBar { border:0; background:#303138; border-radius:3px; height:6px; text-align:center; }
QProgressBar::chunk { background:#d65443; border-radius:3px; }
QStatusBar { background:#17181c; color:#8e8b85; }
#bottomStats { color:#777b82; font-size:10px; padding-right:10px; }
QDialog { background:#15161a; color:#e9e6df; }
QDialog QLabel { color:#e9e6df; }
QDialog QListWidget { background:#1b1c21; color:#e9e6df; }
QDialog QListWidget::item { color:#e9e6df; border-bottom:1px solid #2c2e34; }
#insightCard { background:#1b1c21; border:1px solid #30323a; border-radius:9px; padding:14px; }
#tasteHint { color:#c8b9ad; background:#2a211f; border-radius:8px; padding:12px; }
"""

if sys.platform == "darwin":
    STYLE = STYLE.replace("Microsoft YaHei UI", UI_FONT)


def cleanup_iina_on_exit(window: MainWindow) -> None:
    # A mode flip or database restore starts a replacement process in the same session.
    if getattr(window, "relaunching", False):
        return
    try:
        iina_cleanup.clear_playback_history()
    except Exception:
        pass  # Playback cleanup is best-effort and must never interrupt quitting.


def main() -> None:
    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("影库")
    qt_app.setOrganizationName("YingKu")
    qt_app.setWindowIcon(QIcon(str(resource_path("assets/app-icon.svg"))))
    qt_app.setStyle("Fusion")
    qt_app.setStyleSheet(STYLE)
    if "--wait-for-pid" in sys.argv:
        previous_pid = int(sys.argv[sys.argv.index("--wait-for-pid") + 1])
        for _ in range(600):
            if not process_alive(previous_pid):break
            time.sleep(0.1)
        else:
            QMessageBox.warning(None, "恢复未开始", "旧影库尚未退出，请关闭后重试。当前数据没有改变。")
            return
    core.DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
    lock_name = hashlib.sha256(str(core.DATA_DIR.resolve()).encode()).hexdigest()[:16]
    instance_lock = QLockFile(str(core.DATA_DIR.parent / f"YingKu-{lock_name}.lock"))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(0):
        QMessageBox.information(None, "影库已打开", "这个资料库已有影库窗口正在使用，请切换到已有窗口。")
        return
    base = core.DATA_DIR
    # A plain launch always asks. Explicit public is used only for a manual flip
    # or restoring the public library, never as an authentication bypass.
    want_private = "--public" not in sys.argv
    authentication_host = None
    if want_private:
        authentication_host = QMainWindow()
        authentication_host.setWindowTitle("影库")
        authentication_host.setWindowIcon(qt_app.windowIcon())
        authentication_host.resize(1100, 720)
        surface = QWidget()
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(48, 48, 48, 48)
        title = QLabel("影库")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addStretch()
        hint = QLabel("请完成系统验证\n验证成功后进入隐私模式，取消则进入普通模式。")
        hint.setObjectName("searchHelp")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        layout.addStretch()
        authentication_host.setCentralWidget(surface)
        authentication_host.show()
        authentication_host.raise_()
        authentication_host.activateWindow()
        qt_app.processEvents()
    allowed_private = privacy.authenticate(authentication_host) if want_private else False
    privacy.configure(base, private=allowed_private)
    core.init_db()
    privacy.stamp_library()
    restore_message = ""
    if "--restore-backup" in sys.argv and (not want_private or allowed_private):
        from maintenance import restore_backup
        try:
            result = restore_backup(sys.argv[sys.argv.index("--restore-backup") + 1])
            restore_message = f"已恢复 {result['movies']} 条影片记录。请先核对扫描源，再手工扫描。\n恢复前备份：{result['safety_backup']}"
        except Exception as exc:
            QMessageBox.warning(None, "恢复未完成", f"{exc}\n请查看原有资料或恢复前备份。")
        os.environ["YINGKU_DISABLE_STARTUP_TASKS"] = "1"
    privacy.stamp_library()
    window = MainWindow()
    window.show()
    window.raise_()
    window.activateWindow()
    if authentication_host is not None:
        authentication_host.close()
        authentication_host.deleteLater()
    if restore_message and "--smoke-test" not in sys.argv:
        QTimer.singleShot(200, lambda: QMessageBox.information(window, "恢复完成", restore_message))
    if "--smoke-test" in sys.argv:
        from mac_smoke import run
        run(qt_app, window, sys.argv[sys.argv.index("--smoke-test") + 1])
    qt_app.aboutToQuit.connect(lambda: cleanup_iina_on_exit(window))
    raise SystemExit(qt_app.exec())


if __name__ == "__main__":
    main()
