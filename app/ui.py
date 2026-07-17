"""主窗口与后台数据更新工作线程。"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.data import (
    SnapshotStore,
    build_events_text,
    format_snapshot_year,
    get_regime_events,
)
from app.log import trace
from app.map import MapWidget, feature_centroid, find_state_at

# --- download_worker ---


class DownloadWorker(QObject):
    step_changed = Signal(str)
    step_progress = Signal(int, int)
    finished = Signal(bool, str)

    def run(self) -> None:
        self.step_changed.emit("正在连接 Wikidata…")
        try:
            import sys
            from pathlib import Path

            init_dir = Path(__file__).resolve().parents[1] / "initdata"
            if str(init_dir) not in sys.path:
                sys.path.insert(0, str(init_dir))
            import run as initdata_run

            def on_progress(
                step: str,
                step_i: int,
                step_n: int,
                item_i: int,
                item_n: int,
            ) -> None:
                self.step_changed.emit(step)
                if item_n > 0:
                    self.step_progress.emit(item_i, item_n)

            initdata_run.run_update(on_progress=on_progress)
            self.finished.emit(True, "政权元数据已更新。")
        except Exception as exc:  # noqa: BLE001 — 向用户展示失败原因
            self.finished.emit(False, str(exc))

# --- main_window ---

SNAPSHOT_YEARS: list[int] = []


def _nearest_snapshot_index(year: int) -> int:
    return min(
        range(len(SNAPSHOT_YEARS)),
        key=lambda i: abs(SNAPSHOT_YEARS[i] - year),
    )


class _MapArea(QWidget):
    """地图 + 浮层标签容器（标签不能作为 WebEngine 子控件）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.map = MapWidget(self)
        self.tag = QFrame(self)
        self.tag.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.tag.setAttribute(Qt.WidgetAttribute.WA_AlwaysStackOnTop)
        self.tag.hide()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.map.setGeometry(0, 0, self.width(), self.height())
        self.tag.raise_()


class MainWindow(QMainWindow):
    def __init__(self, store: SnapshotStore) -> None:
        super().__init__()
        global SNAPSHOT_YEARS
        self._store = store
        SNAPSHOT_YEARS = store.years()
        self.setWindowTitle("历史版图")
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)

        self._current_year = 100
        self._sync_retry_count = 0
        self._current_features: dict[str, dict] = {}
        self._selected_state_id: str | None = None
        self._download_thread: QThread | None = None
        self._download_progress: QProgressDialog | None = None
        self._download_action: QAction | None = None
        self._map_ready = False
        self._map_started = False
        self._map_synced_year: int | None = None
        self._panel_sync_gen = 0
        self._panel_anchor_lat = 0.0
        self._panel_anchor_lng = 0.0
        self._panel_payload: dict | None = None

        self._build_ui()
        self._apply_styles()
        self._bind_store(store)
        self._set_loading_progress("正在加载地图…", 0)

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(260)
        sidebar.setMaximumWidth(300)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(16, 16, 16, 16)
        side_layout.setSpacing(12)

        title = QLabel("历史版图")
        title.setObjectName("title")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        side_layout.addWidget(title)

        self._year_label = QLabel()
        self._year_label.setObjectName("yearLabel")
        year_font = QFont()
        year_font.setPointSize(22)
        year_font.setBold(True)
        self._year_label.setFont(year_font)
        self._year_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side_layout.addWidget(self._year_label)

        self._snapshot_label = QLabel()
        self._snapshot_label.setObjectName("snapshotLabel")
        self._snapshot_label.setWordWrap(True)
        self._snapshot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side_layout.addWidget(self._snapshot_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("loadProgress")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        side_layout.addWidget(self._progress_bar)

        self._count_label = QLabel()
        self._count_label.setObjectName("countLabel")
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side_layout.addWidget(self._count_label)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel(format_snapshot_year(SNAPSHOT_YEARS[0])))
        range_row.addStretch()
        range_row.addWidget(QLabel(format_snapshot_year(SNAPSHOT_YEARS[-1])))
        side_layout.addLayout(range_row)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setMinimum(0)
        self._slider.setMaximum(len(SNAPSHOT_YEARS) - 1)
        self._slider.setValue(_nearest_snapshot_index(self._current_year))
        self._slider.setTracking(True)
        self._slider.setPageStep(1)
        self._slider.setSingleStep(1)
        self._slider.valueChanged.connect(self._on_slider_index_changed)
        self._slider.sliderPressed.connect(self._on_slider_pressed)
        self._slider.sliderReleased.connect(self._on_slider_released)
        side_layout.addWidget(self._slider)

        self._slider_dragging = False

        self._detail_name = QLabel("点击地图上的政权查看详情")
        self._detail_name.setObjectName("detailName")
        detail_font = QFont()
        detail_font.setPointSize(13)
        detail_font.setBold(True)
        self._detail_name.setFont(detail_font)
        self._detail_name.setWordWrap(True)
        side_layout.addWidget(self._detail_name)

        self._detail_meta = QLabel()
        self._detail_meta.setObjectName("detailMeta")
        self._detail_meta.setWordWrap(True)
        side_layout.addWidget(self._detail_meta)

        self._events_title = QLabel("大事记")
        self._events_title.setObjectName("eventsTitle")
        self._events_title.setVisible(False)
        events_title_font = QFont()
        events_title_font.setPointSize(13)
        events_title_font.setBold(True)
        self._events_title.setFont(events_title_font)
        side_layout.addWidget(self._events_title)

        self._events_content = QLabel("点击地图上的政权查看大事年表")
        self._events_content.setObjectName("eventsContent")
        self._events_content.setWordWrap(True)
        side_layout.addWidget(self._events_content)

        side_layout.addStretch()

        splitter.addWidget(sidebar)

        self._map_area = _MapArea()
        self._map = self._map_area.map
        self._map.mapClicked.connect(self._on_map_clicked)
        self._map.mapEngineReady.connect(self._on_map_engine_ready)
        self._map.loadProgress.connect(self._on_map_load_progress)
        self._map.yearApplied.connect(self._on_map_year_applied)
        self._map.snapshotsReady.connect(self._on_map_snapshots_ready)
        self._map.viewSettled.connect(self._on_map_view_settled)
        splitter.addWidget(self._map_area)

        self._map_tag = self._map_area.tag
        self._map_tag.setObjectName("mapRegimeTag")
        tag_layout = QVBoxLayout(self._map_tag)
        tag_layout.setContentsMargins(12, 12, 14, 12)
        tag_layout.setSpacing(6)
        self._map_tag_name = QLabel()
        self._map_tag_name.setObjectName("mapTagName")
        self._map_tag_name.setWordWrap(True)
        tag_name_font = QFont()
        tag_name_font.setPointSize(11)
        tag_name_font.setBold(True)
        self._map_tag_name.setFont(tag_name_font)
        self._map_tag_meta = QLabel()
        self._map_tag_meta.setObjectName("mapTagMeta")
        self._map_tag_meta.setWordWrap(True)
        tag_layout.addWidget(self._map_tag_name)
        tag_layout.addWidget(self._map_tag_meta)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        self._build_menu()

    def _build_menu(self) -> None:
        data_menu = self.menuBar().addMenu("数据")
        self._download_action = QAction("更新数据…", self)
        self._download_action.triggered.connect(self._start_data_update)
        data_menu.addAction(self._download_action)

        view_menu = self.menuBar().addMenu("视图")
        reset_action = QAction("重置到公元100年", self)
        reset_action.triggered.connect(lambda: self._go_to_year(100))
        view_menu.addAction(reset_action)

        menu = self.menuBar().addMenu("帮助")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        menu.addAction(about_action)

    def _apply_styles(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #141824;
                color: #e8e8e8;
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
            }
            #sidebar {
                background: #1c2233;
                border-right: 1px solid #2a3148;
            }
            #title { color: #ffffff; }
            #yearLabel { color: #f0c674; padding: 8px 0 0; }
            #snapshotLabel { color: #6d7a96; font-size: 11px; padding-bottom: 4px; }
            #countLabel { color: #6d7a96; font-size: 11px; }
            #loadProgress {
                min-height: 10px;
                max-height: 10px;
                border: none;
                border-radius: 5px;
                background: #2a3148;
                color: #c8cdd8;
                text-align: center;
            }
            #loadProgress::chunk {
                background: #4a6fa5;
                border-radius: 5px;
            }
            #detailName { color: #ffffff; }
            #detailMeta { color: #8892a8; font-size: 12px; line-height: 1.5; }
            #eventsTitle { color: #f0c674; }
            #eventsContent { color: #c8cdd8; font-size: 12px; line-height: 1.6; }
            #mapRegimeTag {
                background: #1c2233;
                border: 1px solid #2a3148;
                border-radius: 8px;
            }
            #mapTagName { color: #f0c674; }
            #mapTagMeta { color: #8892a8; font-size: 12px; line-height: 1.5; }
            QSlider::groove:horizontal {
                height: 8px;
                background: #2a3148;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                width: 18px;
                margin: -6px 0;
                background: #f0c674;
                border-radius: 9px;
            }
            QSlider::sub-page:horizontal { background: #4a6fa5; border-radius: 4px; }
            QMenuBar { background: #1c2233; color: #e8e8e8; }
            QMenuBar::item:selected { background: #2a3148; }
            QMenu { background: #1c2233; color: #e8e8e8; border: 1px solid #2a3148; }
            QMenu::item:selected { background: #2d4a7a; }
        """)

    def _set_loading_progress(self, message: str, percent: int) -> None:
        self._progress_bar.setVisible(True)
        self._progress_bar.setValue(max(0, min(100, percent)))
        self._snapshot_label.setText(message)

    def _finish_loading(self) -> None:
        self._map_ready = True
        self._progress_bar.setVisible(False)
        self._snapshot_label.setText("")

    def _bind_store(self, store: SnapshotStore) -> None:
        global SNAPSHOT_YEARS
        self._store = store
        SNAPSHOT_YEARS = store.years()
        self._slider.setMaximum(max(0, len(SNAPSHOT_YEARS) - 1))
        self._slider.setValue(_nearest_snapshot_index(self._current_year))
        self._slider.setEnabled(True)
        self._go_to_year(self._current_year)

    def _on_map_engine_ready(self) -> None:
        if self._map_started:
            return
        self._map_started = True
        years_js = json.dumps(self._store.years())
        self._map.page().runJavaScript(f"setSnapshotYears({years_js})")
        if self._map_synced_year != self._current_year:
            self._map.set_snapshot_year(self._current_year, force=True)
        QTimer.singleShot(500, self._map.warm_year_cache)
        QTimer.singleShot(800, self._confirm_map_year_from_js)

    def _confirm_map_year_from_js(self) -> None:
        """兜底：JS 版图已就绪但 yearApplied 未到时，仍标记同步完成。"""

        def on_state(raw: object) -> None:
            js = json.loads(raw) if isinstance(raw, str) else {}
            year = js.get("storedYear")
            count = js.get("featureCount", -1)
            if year != self._current_year or js.get("loadingYear"):
                return
            expected = self._store.feature_count(self._current_year)
            if count != expected:
                return
            if self._map_synced_year != year:
                trace("YEAR", f"confirm from JS stored={year} count={count}")
                self._map_synced_year = year
            if not self._map_ready:
                self._map.mark_map_ready()
                self._finish_loading()
            elif not self._slider_dragging:
                self._map.refresh_layers()

        self._map.query_map_state(on_state)

    def _on_map_snapshots_ready(self, ok: bool) -> None:
        if not ok:
            return
        if not self._map_ready:
            self._map.mark_map_ready()
            self._finish_loading()

    def _on_map_load_progress(self, percent: int, message: str) -> None:
        if self._map_ready:
            return
        self._set_loading_progress(message, percent)

    def _on_map_year_applied(self, year: int, layer_count: int) -> None:
        expected_count = self._store.feature_count(self._current_year)
        trace(
            "YEAR",
            f"yearApplied year={year} layers={layer_count} "
            f"current={self._current_year} expected={expected_count} "
            f"synced={self._map_synced_year} retry={self._sync_retry_count}",
        )
        if not self._map_ready:
            self._map.mark_map_ready()
            self._finish_loading()
        if year != self._current_year:
            trace(
                "YEAR",
                f"ignore stale yearApplied={year} current={self._current_year}",
            )
            return
        if layer_count == expected_count:
            self._map_synced_year = year
            trace("YEAR", f"SYNC OK -> synced={year}")
            if not self._slider_dragging:
                self._map.refresh_layers()
            if self._selected_state_id:
                QTimer.singleShot(0, self._sync_map_from_sidebar)
            return
        if self._sync_retry_count >= 3:
            trace("YEAR", "retry limit reached, giving up")
            return
        self._sync_retry_count += 1
        trace("YEAR", f"retry #{self._sync_retry_count} -> {self._current_year}")
        self._map.set_snapshot_year(self._current_year, force=True)

    def _process_map_click(
        self, lat: float, lng: float, state_id_hint: str = ""
    ) -> None:
        # 命中判定只由 Python 几何计算，不用 JS 画布 hint，避免左栏与地图标签不一致
        _ = state_id_hint
        state_id = find_state_at(self._current_features, lat, lng)
        if not state_id or state_id not in self._current_features:
            self._show_detail(None, sync_map=True)
            return
        self._show_detail(state_id, sync_map=True, lat=lat, lng=lng)

    def _on_map_clicked(
        self, lat: float, lng: float, map_year: int, state_id_hint: str
    ) -> None:
        trace(
            "CLICK",
            f"lat={lat:.2f} lng={lng:.2f} map_year={map_year} "
            f"current={self._current_year} synced={self._map_synced_year}",
        )
        if map_year != self._current_year or self._map_synced_year != self._current_year:
            trace("CLICK", "ignored (year not synced)")
            return
        self._process_map_click(lat, lng, state_id_hint or "")

    def _on_map_view_settled(self) -> None:
        if self._selected_state_id:
            self._update_map_tag()

    def _on_slider_pressed(self) -> None:
        self._slider_dragging = True

    def _on_slider_index_changed(self, index: int) -> None:
        year = SNAPSHOT_YEARS[index]
        self._year_label.setText(format_snapshot_year(year))
        self._apply_drag_year(year)

    def _apply_drag_year(self, year: int) -> None:
        """拖动中：立即更新侧栏与地图，不走大 JSON 注入。"""
        if year == self._current_year:
            self._map.set_snapshot_year_fast(year)
            return
        self._current_year = year
        self._sync_retry_count = 0
        self._map_synced_year = None
        self._current_features = self._store.features_by_id(year)
        self._count_label.setText(f"共 {self._store.feature_count(year)} 个政权")
        self._map.set_snapshot_year_fast(year)

    def _on_slider_released(self) -> None:
        self._slider_dragging = False
        year = SNAPSHOT_YEARS[self._slider.value()]
        self._selected_state_id = None
        self._show_detail(None)
        self._commit_year(year, force=True)
        QTimer.singleShot(80, self._flush_map_after_slider)

    def _flush_map_after_slider(self) -> None:
        """滑块松手后确保地图可见且与当前年代一致（无需再点地图）。"""
        year = self._current_year
        if self._map_synced_year != year:
            self._map.set_snapshot_year(year, force=True)
        else:
            self._map.refresh_layers()
        QTimer.singleShot(80, self._confirm_map_year_from_js)

    def _commit_year(self, year: int, *, force: bool = False) -> None:
        """切换年代：更新侧栏与地图，force 时跳过去重并强制重绘。"""
        if not force and year == self._current_year and self._map_synced_year == year:
            trace("GO_YEAR", f"skip already synced year={year}")
            return
        trace(
            "GO_YEAR",
            f"year={year} force={force} prev_current={self._current_year} "
            f"prev_synced={self._map_synced_year}",
        )
        self._current_year = year
        self._sync_retry_count = 0
        self._map_synced_year = None
        self._year_label.setText(format_snapshot_year(year))
        self._current_features = self._store.features_by_id(year)
        self._count_label.setText(f"共 {self._store.feature_count(year)} 个政权")
        if not self._slider_dragging:
            self._selected_state_id = None
            self._show_detail(None)
        self._slider.blockSignals(True)
        self._slider.setValue(_nearest_snapshot_index(year))
        self._slider.blockSignals(False)
        if force:
            self._map.set_snapshot_year(year, force=True)
        else:
            self._map.set_snapshot_year(year, force=False)

    def _go_to_year(self, year: int) -> None:
        """兼容旧调用。"""
        self._commit_year(year)

    @staticmethod
    def _panel_props(properties: dict) -> dict:
        return {
            "name_zh": properties.get("name_zh", ""),
            "name_en": properties.get("name_en", ""),
            "period_zh": properties.get("period_zh", "待考"),
            "ethnicity_zh": properties.get("ethnicity_zh", "待考"),
            "rulers_zh": properties.get("rulers_zh", "暂无记载"),
            "part_of_zh": properties.get("part_of_zh", ""),
            "subject_zh": properties.get("subject_zh", ""),
        }

    def _sync_map_from_sidebar(self) -> None:
        """左侧栏更新后，地图高亮与 Qt 标签与之同步。"""
        self._panel_sync_gen += 1
        gen = self._panel_sync_gen
        if not self._selected_state_id or not self._panel_payload:
            self._map.set_highlight(None, gen)
            self._map_tag_name.clear()
            self._map_tag_meta.clear()
            self._map_tag.hide()
            return
        lat, lng = feature_centroid(self._current_features[self._selected_state_id])
        self._panel_anchor_lat = lat
        self._panel_anchor_lng = lng
        self._map.set_highlight(
            self._selected_state_id,
            gen,
            self._detail_name.text(),
            lat,
            lng,
        )
        self._update_map_tag()

    def _update_map_tag(self) -> None:
        """地图标签文字与左侧栏完全相同。"""
        if not self._selected_state_id:
            self._map_tag.hide()
            return
        self._map_tag_name.setText(self._detail_name.text())
        self._map_tag_meta.setText(self._detail_meta.text())
        self._map_tag.adjustSize()
        self._map_tag.show()
        self._map_tag.raise_()
        self._position_map_tag()

    def _position_map_tag(self) -> None:
        if not self._selected_state_id or not self._map_tag.isVisible():
            return

        def _place(raw: object) -> None:
            if isinstance(raw, str):
                try:
                    pt = json.loads(raw)
                    x = int(pt.get("x", 0))
                    y = int(pt.get("y", 0))
                except json.JSONDecodeError:
                    x = self._map.width() // 2
                    y = self._map.height() // 3
            else:
                x = self._map.width() // 2
                y = self._map.height() // 3
            self._map_tag.adjustSize()
            tag_w = self._map_tag.width()
            tag_h = self._map_tag.height()
            map_w = max(1, self._map.width())
            map_h = max(1, self._map.height())
            px = min(max(8, x + 12), max(8, map_w - tag_w - 8))
            py = min(max(8, y - tag_h - 8), max(8, map_h - tag_h - 8))
            self._map_tag.move(px, py)
            self._map_tag.raise_()

        self._map.lat_lng_to_map_point(
            self._panel_anchor_lat,
            self._panel_anchor_lng,
            _place,
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._selected_state_id and self._map_tag.isVisible():
            QTimer.singleShot(0, self._position_map_tag)

    def _show_detail(
        self,
        state_id: str | None,
        *,
        sync_map: bool = True,
        lat: float = 0.0,
        lng: float = 0.0,
    ) -> None:
        if state_id is None or state_id not in self._current_features:
            self._selected_state_id = None
            self._panel_payload = None
            self._panel_anchor_lat = 0.0
            self._panel_anchor_lng = 0.0
            self._detail_name.setText("点击地图上的政权查看详情")
            self._detail_meta.setText("")
            self._events_title.setText("大事记")
            self._events_content.setText("点击地图上的政权查看大事年表")
            if sync_map:
                self._sync_map_from_sidebar()
            return

        self._selected_state_id = state_id
        self._panel_anchor_lat = lat
        self._panel_anchor_lng = lng
        feature = self._current_features[state_id]
        p = feature["properties"]
        self._panel_payload = self._panel_props(p)
        self._panel_payload["state_id"] = state_id
        name_en = p.get("name_en", "")
        name_zh = p.get("name_zh", "")
        self._detail_name.setText(name_zh)
        self._detail_meta.setText(
            f"存在时间：{p.get('period_zh', '待考')}\n"
            f"主体族群：{p.get('ethnicity_zh', '待考')}\n"
            f"著名君主：{p.get('rulers_zh', '暂无记载')}"
        )
        events = get_regime_events(name_en)
        self._events_title.setText("大事记")
        self._events_content.setText(build_events_text(name_zh, events))
        if sync_map:
            self._sync_map_from_sidebar()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于",
            "历史版图 v2.1\n\n"
            "浏览历史上各政权的不同时期疆域变化。\n"
            "已过滤人种、考古文化等非政权条目。\n\n"
            "数据来源：historical-basemaps、Wikidata 等多源融合",
        )

    def _start_data_update(self) -> None:
        if self._download_thread is not None:
            return

        reply = QMessageBox.question(
            self,
            "更新数据",
            "将基于本地版图，从 Wikidata 拉取最新政权元数据并重建索引。\n"
            "不会重新下载版图文件，但需要联网，可能需要数分钟。\n\n"
            "是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._download_progress = QProgressDialog(
            "正在启动更新…",
            None,
            0,
            0,
            self,
        )
        self._download_progress.setWindowTitle("更新数据")
        self._download_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._download_progress.setMinimumDuration(0)
        self._download_progress.setCancelButton(None)
        self._download_progress.show()

        if self._download_action:
            self._download_action.setEnabled(False)

        worker = DownloadWorker()
        thread = QThread(self)
        worker.moveToThread(thread)
        worker.step_changed.connect(self._on_download_step, Qt.ConnectionType.QueuedConnection)
        worker.step_progress.connect(
            self._on_download_item_progress, Qt.ConnectionType.QueuedConnection
        )
        worker.finished.connect(self._on_download_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_download_thread)
        self._download_thread = thread
        thread.started.connect(worker.run)
        thread.start()

    def _on_download_step(self, step: str) -> None:
        if self._download_progress:
            self._download_progress.setLabelText(step)
            if self._download_progress.maximum() == 0:
                self._download_progress.setRange(0, 0)

    def _on_download_item_progress(self, current: int, total: int) -> None:
        if self._download_progress and total > 0:
            self._download_progress.setRange(0, total)
            self._download_progress.setValue(current)

    def _on_download_finished(self, success: bool, message: str) -> None:
        if self._download_progress:
            self._download_progress.close()
            self._download_progress = None
        if self._download_action:
            self._download_action.setEnabled(True)

        if success:
            import sys
            from pathlib import Path

            init_dir = Path(__file__).resolve().parents[1] / "initdata"
            if str(init_dir) not in sys.path:
                sys.path.insert(0, str(init_dir))
            import tasks as initdata_tasks

            initdata_tasks.build_prepared_cache(force=True)
            self._store.reload()
            self._map.reload_snapshots(
                on_done=lambda: self._go_to_year(self._current_year),
            )
            QMessageBox.information(self, "更新完成", message)
        else:
            QMessageBox.warning(
                self,
                "更新失败",
                f"数据更新未完成：\n{message}\n\n请检查网络连接后重试。",
            )

    def _clear_download_thread(self) -> None:
        self._download_thread = None

__all__ = ["DownloadWorker", "MainWindow"]
