from PySide6.QtCore import QEvent, QThread, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

import lol


class CatalogWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, parent=None, *, require_remote=False):
        super().__init__(parent)
        self._require_remote = require_remote

    def run(self):
        try:
            catalog = (
                lol.refresh_champion_catalog(prefer_data_dragon=True)
                if self._require_remote
                else lol.get_champion_catalog(fetch_remote=True, force_refresh=True)
            )
        except Exception as error:
            self.failed.emit(str(error))
            return
        self.loaded.emit(catalog)


class LaneAssignerDialog(QDialog):
    lanes = lol.LANES

    def __init__(
        self, parent, translate, icon_provider, on_applied, on_catalog_updated=None
    ):
        super().__init__(parent)
        self._translate = translate
        self._icon_provider = icon_provider
        self._on_applied = on_applied
        self._on_catalog_updated = on_catalog_updated or (lambda _catalog: None)
        self._catalog = lol.get_champion_catalog(fetch_remote=False)
        self._saved_overrides = lol.load_lane_manual_overrides()
        self._baseline = {}
        self._standard = {}
        self._working = {}
        self._row_keys = []
        self._reset_staged = False
        self._reset_refreshing = False
        self._allow_close = False
        self._catalog_worker = None
        self.setModal(False)
        self.setMinimumSize(780, 520)
        self.resize(1000, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)
        self.title_label = QLabel()
        self.title_label.setObjectName("pageTitle")
        layout.addWidget(self.title_label)
        self.search = QLineEdit()
        self.search.textChanged.connect(self._filter_rows)
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 7, self)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for column in range(2, 7):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.itemChanged.connect(self._assignment_changed)
        layout.addWidget(self.table, 1)

        self.action_bar = QHBoxLayout()
        self.cancel_button = QPushButton()
        self.close_button = QPushButton()
        self.apply_button = QPushButton()
        self.apply_button.setObjectName("accentButton")
        self.reset_button = QPushButton()
        for button, action in (
            (self.cancel_button, self.cancel),
            (self.close_button, self.close_with_prompt),
            (self.apply_button, self.apply_changes),
            (self.reset_button, self.stage_reset),
        ):
            self.action_bar.addWidget(button)
            button.clicked.connect(action)
        layout.addLayout(self.action_bar)

        if self._catalog:
            self._initialize_assignments()
        else:
            self._set_catalog_message(self._tr("catalog_loading"))
            self._load_catalog_async()
        self.retranslate()

    def _tr(self, key):
        return self._translate(key)

    def retranslate(self):
        self.setWindowTitle(self._tr("assigner_title"))
        self.title_label.setText(self._tr("assigner_title"))
        self.search.setPlaceholderText(self._tr("assigner_search"))
        self.table.setHorizontalHeaderLabels(
            [self._tr("icon"), self._tr("champion"), *[self._tr(lane.casefold()) for lane in self.lanes]]
        )
        self.cancel_button.setText(self._tr("cancel"))
        self.close_button.setText(self._tr("close"))
        self.apply_button.setText(self._tr("apply"))
        self.reset_button.setText(self._tr("reset_standard"))

    def _set_catalog_message(self, message):
        self.table.setRowCount(1)
        item = QTableWidgetItem(message)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.table.setItem(0, 1, item)

    def _load_catalog_async(self):
        if self._catalog_worker is not None and self._catalog_worker.isRunning():
            return
        self._catalog_worker = CatalogWorker(self)
        self._catalog_worker.loaded.connect(self._catalog_loaded)
        self._catalog_worker.failed.connect(self._catalog_failed)
        self.reset_button.setEnabled(False)
        self._catalog_worker.start()

    def _catalog_loaded(self, catalog):
        self.reset_button.setEnabled(True)
        if not catalog:
            self._set_catalog_message(self._tr("catalog_unavailable"))
            return
        changed = catalog != self._catalog
        if not self._catalog:
            self._catalog = catalog
            self._initialize_assignments()
        elif changed:
            self._merge_catalog_preserving_pending(catalog)
        if changed:
            self._on_catalog_updated(catalog)

    def _catalog_failed(self, message):
        if self._reset_refreshing:
            self._set_reset_refreshing(False)
            self.reset_button.setEnabled(bool(self._catalog))
            QMessageBox.warning(
                self,
                self._tr("reset_title"),
                f"{self._tr('reset_refresh_failed')}\n{message}",
            )
        elif not self._catalog:
            self._set_catalog_message(self._tr("catalog_unavailable"))
        else:
            self.reset_button.setEnabled(True)

    def _merge_catalog_preserving_pending(self, catalog):
        old_baseline = self._baseline
        old_working = self._working
        reset_was_staged = self._reset_staged
        self._catalog = catalog
        standard, effective = lol.get_lane_assignments(self._catalog)
        self._row_keys = []
        self._standard = {}
        self._baseline = {}
        self._working = {}
        for record in self._catalog:
            champion_id = record.get("id")
            key = str(champion_id) if champion_id is not None else str(record.get("name", ""))
            if not key:
                continue
            numeric_id = int(champion_id) if champion_id is not None else None
            standard_lanes = list(standard.get(numeric_id, [])) if numeric_id is not None else []
            effective_lanes = list(effective.get(numeric_id, [])) if numeric_id is not None else []
            self._row_keys.append(key)
            self._standard[key] = standard_lanes
            self._baseline[key] = list(old_baseline.get(key, effective_lanes))
            self._working[key] = list(
                old_working.get(
                    key,
                    standard_lanes if reset_was_staged else effective_lanes,
                )
            )
        self._reset_staged = reset_was_staged
        self._populate_table()
        self._update_pending_state()

    def _initialize_assignments(self):
        standard, effective = lol.get_lane_assignments(self._catalog)
        self._row_keys = []
        self._standard = {}
        self._baseline = {}
        self._working = {}
        for record in self._catalog:
            champion_id = record.get("id")
            key = str(champion_id) if champion_id is not None else str(record.get("name", ""))
            if not key:
                continue
            selected = list(effective.get(int(champion_id), [])) if champion_id is not None else []
            standard_lanes = list(standard.get(int(champion_id), [])) if champion_id is not None else []
            self._row_keys.append(key)
            self._standard[key] = standard_lanes
            self._baseline[key] = selected
            self._working[key] = list(selected)
        self._reset_staged = False
        self._populate_table()
        self._update_pending_state()

    def _populate_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._row_keys))
        records_by_id = {str(record.get("id")): record for record in self._catalog}
        for row_index, key in enumerate(self._row_keys):
            record = records_by_id.get(key)
            if record is None:
                record = next(
                    (item for item in self._catalog if str(item.get("name", "")) == key),
                    {},
                )
            icon_item = QTableWidgetItem()
            icon_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            icon = self._icon_provider(record.get("name", ""))
            if isinstance(icon, QIcon) and not icon.isNull():
                icon_item.setIcon(icon)
            self.table.setItem(row_index, 0, icon_item)
            name_item = QTableWidgetItem(str(record.get("name") or key))
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row_index, 1, name_item)
            for lane_index, lane in enumerate(self.lanes, start=2):
                item = QTableWidgetItem()
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if lane in self._working.get(key, [])
                    else Qt.CheckState.Unchecked
                )
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, lane_index, item)
        self.table.blockSignals(False)
        self._filter_rows(self.search.text())

    def refresh_icons(self):
        if not self._row_keys:
            return
        for row_index, key in enumerate(self._row_keys):
            name_item = self.table.item(row_index, 1)
            icon_item = self.table.item(row_index, 0)
            if name_item is None or icon_item is None:
                continue
            icon = self._icon_provider(name_item.text())
            if isinstance(icon, QIcon) and not icon.isNull():
                icon_item.setIcon(icon)

    def _assignment_changed(self, item):
        if item.column() < 2 or item.row() >= len(self._row_keys):
            return
        key = self._row_keys[item.row()]
        lane = self.lanes[item.column() - 2]
        selected = set(self._working.get(key, []))
        if item.checkState() == Qt.CheckState.Checked:
            selected.add(lane)
        else:
            selected.discard(lane)
        self._working[key] = [candidate for candidate in self.lanes if candidate in selected]
        self._update_pending_state()

    def _filter_rows(self, text):
        query = text.strip().casefold()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            self.table.setRowHidden(
                row,
                bool(query and item and query not in item.text().casefold()),
            )

    def _has_changes(self):
        return self._reset_staged or any(
            self._working.get(key, []) != self._baseline.get(key, [])
            for key in self._row_keys
        )

    def _update_pending_state(self):
        self.apply_button.setEnabled(
            not self._reset_refreshing and self._has_changes() and bool(self._catalog)
        )
        suffix = f" · {self._tr('pending_changes')}" if self._has_changes() else ""
        self.setWindowTitle(self._tr("assigner_title") + suffix)

    def _set_reset_refreshing(self, refreshing):
        self._reset_refreshing = refreshing
        self.table.setEnabled(not refreshing)
        self.cancel_button.setEnabled(not refreshing)
        self.close_button.setEnabled(not refreshing)
        self.reset_button.setEnabled(not refreshing)
        self._update_pending_state()

    def _discard_pending(self):
        self._working = {key: list(value) for key, value in self._baseline.items()}
        self._reset_staged = False
        self._populate_table()
        self._update_pending_state()

    def _finish_close(self):
        self._allow_close = True
        self.hide()
        self._allow_close = False

    def cancel(self):
        if self._has_changes():
            dialog = QMessageBox(self)
            dialog.setWindowTitle(self._tr("discard_title"))
            dialog.setText(self._tr("discard_pending_question"))
            discard_button = dialog.addButton(
                self._tr("discard_close"), QMessageBox.ButtonRole.DestructiveRole
            )
            dialog.addButton(
                self._tr("continue_editing"), QMessageBox.ButtonRole.RejectRole
            )
            dialog.exec()
            if dialog.clickedButton() != discard_button:
                return
        self._discard_pending()
        self._finish_close()

    def close_with_prompt(self):
        if not self._has_changes():
            self._finish_close()
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle(self._tr("unsaved_title"))
        dialog.setText(self._tr("unsaved_question"))
        apply_button = dialog.addButton(
            self._tr("apply_close"), QMessageBox.ButtonRole.AcceptRole
        )
        discard_button = dialog.addButton(
            self._tr("discard_close"), QMessageBox.ButtonRole.DestructiveRole
        )
        dialog.addButton(self._tr("return_assigner"), QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() == apply_button:
            if self.apply_changes():
                self._finish_close()
        elif dialog.clickedButton() == discard_button:
            self._discard_pending()
            self._finish_close()

    def apply_changes(self):
        if not self._has_changes():
            return True
        try:
            if self._reset_staged:
                overrides = {}
            else:
                overrides = dict(self._saved_overrides)
                records_by_id = {
                    str(record.get("id")): record for record in self._catalog
                }
                for key in self._row_keys:
                    current = self._working.get(key, [])
                    if current == self._baseline.get(key, []):
                        continue
                    record = records_by_id.get(key, {})
                    name = str(record.get("name") or "")
                    overrides.pop(key, None)
                    if name:
                        overrides.pop(name, None)
                    if current != self._standard.get(key, []):
                        overrides[key] = list(current)
            lol.save_lane_manual_overrides(overrides)
        except (OSError, ValueError) as error:
            QMessageBox.critical(
                self, self._tr("save_failed"), f"{self._tr('save_failed_detail')}\n{error}"
            )
            return False

        self._saved_overrides = overrides
        self._baseline = {key: list(value) for key, value in self._working.items()}
        self._reset_staged = False
        self._update_pending_state()
        self._on_applied()
        return True

    def stage_reset(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle(self._tr("reset_title"))
        dialog.setText(self._tr("reset_confirm"))
        yes_button = dialog.addButton(
            self._tr("yes"), QMessageBox.ButtonRole.AcceptRole
        )
        dialog.addButton(self._tr("no"), QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() != yes_button:
            return
        self._set_reset_refreshing(True)
        self._catalog_worker = CatalogWorker(self, require_remote=True)
        self._catalog_worker.loaded.connect(self._standard_catalog_loaded)
        self._catalog_worker.failed.connect(self._catalog_failed)
        self._catalog_worker.start()

    def _standard_catalog_loaded(self, catalog):
        if not catalog:
            self._catalog_failed(self._tr("catalog_unavailable"))
            return
        changed = catalog != self._catalog
        self._catalog = catalog
        standard, effective = lol.get_lane_assignments(self._catalog)
        self._row_keys = []
        self._standard = {}
        self._baseline = {}
        self._working = {}
        for record in self._catalog:
            champion_id = record.get("id")
            key = str(champion_id) if champion_id is not None else str(record.get("name", ""))
            if not key:
                continue
            numeric_id = int(champion_id) if champion_id is not None else None
            standard_lanes = list(standard.get(numeric_id, [])) if numeric_id is not None else []
            effective_lanes = list(effective.get(numeric_id, [])) if numeric_id is not None else []
            self._row_keys.append(key)
            self._standard[key] = standard_lanes
            self._baseline[key] = effective_lanes
            self._working[key] = list(standard_lanes)
        self._reset_staged = bool(self._saved_overrides) or any(
            self._working[key] != self._baseline[key] for key in self._row_keys
        )
        self._populate_table()
        self._set_reset_refreshing(False)
        if changed:
            self._on_catalog_updated(catalog)

    def prepare_to_open(self):
        if self.isVisible():
            self.raise_()
            self.activateWindow()
            return
        self._saved_overrides = lol.load_lane_manual_overrides()
        self._catalog = lol.get_champion_catalog(fetch_remote=False)
        if self._catalog:
            self._initialize_assignments()
        else:
            self._set_catalog_message(self._tr("catalog_loading"))
        self._load_catalog_async()
        self.show()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()

    def closeEvent(self, event):
        if self._allow_close:
            event.accept()
        else:
            event.ignore()
            self.close_with_prompt()
