"""Recycle bin: browse records removed from the library; batch restore or permanently delete."""
from __future__ import annotations
import math
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QLineEdit, QVBoxLayout, QHBoxLayout, QGridLayout,
    QCheckBox, QScrollArea, QMessageBox, QSizePolicy,
)
import app as core
from scrolling import set_background, ScrollSafeComboBox


class TrashCard(QWidget):
    """A trashed movie with checkbox for batch selection."""

    def __init__(self, movie, images_loader, poster_class, parent=None):
        super().__init__(parent)
        self.movie = movie
        self.setFixedWidth(154)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        self.checkbox = QCheckBox()
        self.checkbox.setStyleSheet("QCheckBox{color:#c9c9ce;font-size:12px;}")
        layout.addWidget(self.checkbox)

        self.poster = poster_class()
        self.poster.setFixedSize(154, 218)
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = movie.get("title") or movie.get("filename", "")
        images_loader.load(movie.get("poster", ""), self.poster, self.poster.size(), title, True, True, True)
        layout.addWidget(self.poster)

        name = QLabel(title[:24] + ("…" if len(title) > 24 else ""))
        name.setObjectName("actorWorkTitle")
        name.setToolTip(title)
        layout.addWidget(name)

        cast = movie.get("cast") or []
        actor_text = ", ".join(p.get("name", "") for p in cast[:2])
        if actor_text:
            actors = QLabel(actor_text[:30])
            actors.setObjectName("mutedSmall")
            layout.addWidget(actors)

        size = QLabel(movie.get("size_label", ""))
        size.setObjectName("mutedSmall")
        layout.addWidget(size)
        layout.addStretch()


class TrashBrowser(QWidget):
    """Browse records removed from the library; restore or permanently delete."""

    def __init__(self, owner, poster_class):
        super().__init__(owner)
        self.owner = owner
        self.poster_class = poster_class
        self.movies: list = []
        self.cards: list[TrashCard] = []
        self.columns = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 27, 28, 12)
        layout.setSpacing(15)

        header = QHBoxLayout()
        title = QLabel("回收站")
        title.setObjectName("heading")
        header.addWidget(title)
        self.count_label = QLabel("")
        self.count_label.setObjectName("muted")
        header.addWidget(self.count_label)
        header.addStretch()
        layout.addLayout(header)

        subtitle = QLabel("从资料库移除的影片仍保留在硬盘上；恢复会重新出现在资料库，永久删除会直接删除文件。")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        actions = QHBoxLayout()
        self.select_all = QPushButton("全选")
        self.select_all.setObjectName("compactText")
        self.select_all.clicked.connect(self.toggle_select_all)
        actions.addWidget(self.select_all)

        self.selection_label = QLabel("已选 0 项")
        self.selection_label.setObjectName("mutedSmall")
        actions.addWidget(self.selection_label)
        actions.addStretch()

        self.restore_btn = QPushButton("恢复所选")
        self.restore_btn.setProperty("detailAction", True)
        self.restore_btn.clicked.connect(self.restore_selected)
        actions.addWidget(self.restore_btn)

        self.purge_btn = QPushButton("永久删除所选")
        self.purge_btn.setObjectName("dangerText")
        self.purge_btn.clicked.connect(self.purge_selected)
        actions.addWidget(self.purge_btn)
        layout.addLayout(actions)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标题或文件名")
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)

        self.actor_filter = ScrollSafeComboBox()
        self.actor_filter.addItem("全部演员", "")
        filters.addWidget(self.actor_filter)
        layout.addLayout(filters)

        self.status = QLabel("")
        self.status.setObjectName("mutedSmall")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_background(self.grid_scroll.viewport(), "#101115")
        self.grid_content = QWidget()
        set_background(self.grid_content, "#101115")
        self.grid = QGridLayout(self.grid_content)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(18)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.grid_scroll.setWidget(self.grid_content)
        layout.addWidget(self.grid_scroll, 1)

        self.empty_label = QLabel("回收站为空。\n\n在影片详情页点「移出资料库」后，影片会出现在这里供进一步处理。")
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.debounce.start(160))
        self.actor_filter.currentIndexChanged.connect(self.refresh)

    def refresh_actors(self):
        current = self.actor_filter.currentData() or ""
        self.actor_filter.blockSignals(True)
        self.actor_filter.clear()
        self.actor_filter.addItem("全部演员", "")
        for actor in core.trashed_actor_facets():
            self.actor_filter.addItem(f"{actor['name']} ({actor['count']})", actor["name"])
        self.actor_filter.blockSignals(False)
        index = self.actor_filter.findData(current)
        self.actor_filter.setCurrentIndex(max(0, index))

    def refresh(self):
        actor = self.actor_filter.currentData() or ""
        self.movies = core.trashed_movies(self.search.text().strip(), actor)

        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.cards = []
        for movie in self.movies:
            card = TrashCard(movie, self.owner.images, self.poster_class)
            card.checkbox.stateChanged.connect(self.update_batch_buttons)
            self.cards.append(card)

        self.columns = 0
        self.reflow()
        self.count_label.setText(f"· {len(self.movies)} 部")
        self.empty_label.setVisible(not self.movies)
        self.grid_scroll.setVisible(bool(self.movies))
        self.update_batch_buttons()

    def reflow(self):
        cols = max(1, (self.grid_scroll.viewport().width() + 18) // 172)
        if cols == self.columns:
            return
        while self.grid.count():
            self.grid.takeAt(0)
        self.columns = cols
        for i, card in enumerate(self.cards):
            self.grid.addWidget(card, i // cols, i % cols)
        self.grid_content.setMinimumHeight(math.ceil(max(1, len(self.cards)) / cols) * 320)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self.reflow)

    def selected_ids(self):
        return [card.movie["id"] for card in self.cards if card.checkbox.isChecked()]

    def update_batch_buttons(self):
        ids = self.selected_ids()
        self.selection_label.setText(f"已选 {len(ids)} 项")
        self.restore_btn.setEnabled(bool(ids))
        self.purge_btn.setEnabled(bool(ids))
        self.select_all.setText("取消全选" if len(ids) == len(self.cards) and self.cards else "全选")

    def toggle_select_all(self):
        all_checked = bool(self.cards) and all(card.checkbox.isChecked() for card in self.cards)
        for card in self.cards:
            card.checkbox.setChecked(not all_checked)
        self.update_batch_buttons()

    def restore_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        answer = QMessageBox.question(
            self, "恢复",
            f"把选中的 {len(ids)} 部影片恢复到资料库？\n\n文件不会被修改，影片会重新出现在资料库中。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        errors = []
        restored = 0
        for mid in ids:
            try:
                core.restore_movie_record(mid)
                restored += 1
            except (ValueError, FileNotFoundError, OSError) as exc:
                errors.append(str(exc))
        self.refresh_actors()
        self.refresh()
        self.owner.load_all()
        self.status.setText(f"已恢复 {restored} 部影片到资料库" + (f"；部分失败：{'；'.join(errors)}" if errors else ""))

    def purge_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        answer = QMessageBox.warning(
            self, "永久删除",
            f"确定永久删除选中的 {len(ids)} 部影片？\n\n"
            "这会直接从硬盘删除影片文件，不可恢复。\n"
            "如果不确定，请先在文件资源管理器中核对。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        errors = []
        deleted = 0
        for mid in ids:
            try:
                core.purge_movie_record(mid)
                deleted += 1
            except (ValueError, FileNotFoundError, OSError) as exc:
                errors.append(str(exc))
        self.refresh_actors()
        self.refresh()
        self.owner.load_all()
        self.status.setText(f"已永久删除 {deleted} 部影片" + (f"；部分失败：{'；'.join(errors)}" if errors else ""))
