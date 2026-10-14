import base64
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin

import requests
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QFontMetrics, QIcon, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

import lol
from lane_assigner import LaneAssignerDialog
from lockfile_watcher import INSTANCE_SERVER_NAME, start_watcher


BG = "#10141c"
PANEL = "#191f2b"
PANEL_ALT = "#202838"
TEXT = "#edf2f7"
MUTED = "#98a6b8"
ACCENT = "#c8aa6e"
GREEN = "#54d6a0"
RED = "#ff727c"
LOLDLE_URL = "https://loldle.net/"
ICON_BASE_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/default/"
)
MASTERY_GRADE_ORDER = ("S+", "S", "S-", "A", "B", "C", "D", "E", "F")


def app_icon_cache_dir():
    if sys.platform == "darwin":
        cache_root = Path.home() / "Library" / "Caches"
    else:
        cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache"))
    return cache_root / "Riftboard" / "champion-icons"


def reset_marker_path():
    return lol.STORAGE_ROOT.parent / f".{lol.STORAGE_ROOT.name}-reset-pending"


def schedule_full_reset(preferences):
    updated_preferences = dict(preferences)
    updated_preferences["first_run"] = True
    updated_preferences["auto_start_on_lockfile"] = False
    lol.save_preferences(updated_preferences)

    marker = reset_marker_path()
    try:
        marker.write_text("reset requested\n", encoding="utf-8")
    except OSError as marker_error:
        try:
            lol.save_preferences(preferences)
        except OSError as rollback_error:
            raise OSError(
                f"Could not schedule reset ({marker_error}); could not restore settings ({rollback_error})"
            ) from marker_error
        raise
    return updated_preferences


def complete_pending_reset():
    marker = reset_marker_path()
    if not marker.exists():
        return False

    for target in (lol.STORAGE_ROOT, app_icon_cache_dir()):
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    marker.unlink()
    return True


def create_loldle_profile(parent):
    profile_root = lol.STORAGE_ROOT / "loldle-browser"
    cache_path = profile_root / "cache"
    storage_path = profile_root / "storage"
    cache_path.mkdir(parents=True, exist_ok=True)
    storage_path.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile("Riftboard-LoLdle", parent)
    profile.setCachePath(str(cache_path))
    profile.setPersistentStoragePath(str(storage_path))
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    return profile


def normalize_name(name):
    return "".join(character.casefold() for character in name if character.isalnum())


def champion_icon_urls(summary):
    if not isinstance(summary, list):
        return {}
    by_name = {}
    for item in summary:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("alias") or "")
        if alias.startswith("Jade_"):
            continue
        try:
            champion_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue
        if not 0 < champion_id < 60000:
            continue
        name = item.get("name")
        portrait = item.get("squarePortraitPath")
        if name and portrait:
            portrait = portrait.replace("/lol-game-data/assets/", "/")
            by_name.setdefault(
                normalize_name(name),
                urljoin(ICON_BASE_URL, portrait.lstrip("/")),
            )
    return by_name


def mastery_grade_sort_key(grade, best_first=True):
    normalized_grade = str(grade or "").strip().upper()
    grade_rank = {
        value: index for index, value in enumerate(MASTERY_GRADE_ORDER)
    }.get(normalized_grade)
    if grade_rank is None:
        return True, 0
    return False, grade_rank if best_first else -grade_rank


def read_report(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def find_line(lines, prefix):
    return next((line for line in lines if line.startswith(prefix)), "")


def parse_top_champions(lines):
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("--- TOP 5 CHAMPIONS")),
        None,
    )
    if start is None:
        return []
    pattern = re.compile(
        r"^\d+\.\s+(.+?)\s+\|\s+(\d+)\s+Spiele\s+\|\s+"
        r"Meisterschaftswertung:\s+(.+?)\s+\|\s+Topwertung:\s+(.+)$"
    )
    champions = []
    for line in lines[start + 1 :]:
        if line.startswith("--- "):
            break
        match = pattern.match(line)
        if match:
            champions.append(match.groups())
    return champions


def parse_missing_champions(lines):
    start = next(
        (index for index, line in enumerate(lines) if line == "--- FEHLENDE CHAMPIONS ---"),
        None,
    )
    if start is None:
        return []
    missing = []
    for line in lines[start + 1 :]:
        if line.startswith("--- ") or line.startswith("Benötigte") or line.startswith("Bekannte"):
            break
        if line.startswith("- "):
            missing.append(line[2:])
    return missing


def parse_mastery_lanes(lines):
    lanes = {}
    current_lane = None
    for line in lines:
        if line.startswith("--- ") and line.endswith(" ---"):
            current_lane = line[4:-4]
            lanes.setdefault(current_lane, [])
        elif current_lane and line and not line.startswith("Champion |"):
            columns = line.split(" | ")
            if len(columns) == 5:
                champion, score, ownership, rank, grade = columns
                try:
                    score = int(score)
                except ValueError:
                    continue
                lanes[current_lane].append((champion, score, ownership, rank, grade))
    return lanes


def parse_match_history(lines):
    start = next(
        (index for index, line in enumerate(lines) if line.startswith("--- MATCH-HISTORIE")),
        None,
    )
    if start is None:
        return []
    pattern = re.compile(
        r"^(.+?)\s+\|\s+Match\s+(.+?)\s+\|\s+(.+?)\s+\|\s+"
        r"(SIEG|NIEDERLAGE|UNBEKANNT)\s+\|\s+(.+?)\s+\|\s+"
        r"(?:Dauer\s+(\d+:\d+)\s+\|\s+)?KDA\s+(\d+|–)/(\d+|–)/(\d+|–)"
        r"(?:\s+\|\s+Lane\s+(.+))?$"
    )
    matches = []
    for line in lines[start + 1 :]:
        match = pattern.match(line)
        if match:
            (
                date,
                match_id,
                mode,
                result,
                champion,
                duration,
                kills,
                deaths,
                assists,
                lane,
            ) = match.groups()
            lane = (
                "–"
                if not lane
                or lane.strip().casefold() in {"–", "nicht verfügbar", "not available"}
                else lane.strip()
            )
            matches.append(
                (
                    date,
                    match_id,
                    mode,
                    result,
                    champion,
                    duration or "–",
                    kills,
                    deaths,
                    assists,
                    lane,
                )
            )
    return matches


def parse_lane_bias(lines):
    line = next((line for line in lines if line.startswith("LANE_BIAS:")), None)
    if line is None:
        return {}
    counts = {}
    for entry in line.partition(":")[2].split(";"):
        lane, separator, raw_count = entry.partition("=")
        if not separator or lane not in ("Top", "Jungle", "Mid", "Bot", "Support"):
            continue
        try:
            count = int(raw_count)
        except ValueError:
            continue
        if count > 0:
            counts[lane] = count
    return counts


def parse_lane_mastery_bias(mastery_lanes):
    totals = {}
    for lane in ("Top", "Jungle", "Mid", "Bot", "Support"):
        seen_champions = set()
        total_points = 0
        for champion, score, *_details in mastery_lanes.get(lane, []):
            key = normalize_name(champion)
            if key in seen_champions:
                continue
            seen_champions.add(key)
            try:
                points = int(score)
            except (TypeError, ValueError):
                continue
            if points > 0:
                total_points += points
        if total_points > 0:
            totals[lane] = total_points
    return totals


def parse_tft_history(lines):
    pattern = re.compile(
        r"^(.+?)\s+\|\s+Match\s+(.+?)\s+\|\s+"
        r"(?:Mode\s+(.+?)\s+\|\s+)?Platz\s+(\d+)/8$"
    )
    matches = []
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        try:
            placement = int(match.group(4))
        except ValueError:
            continue
        if 1 <= placement <= 8:
            matches.append(
                (match.group(1), match.group(2), match.group(3) or "–", str(placement))
            )
    average = (
        f"{sum(int(match[3]) for match in matches) / len(matches):.2f}/8"
        if matches
        else "–"
    )
    return matches, average


class WorkerSignals(QObject):
    refreshed = Signal(bool, str)
    champion_icons = Signal(dict)
    profile_icon = Signal(bytes)
    profile_icon_error = Signal(str)
    lockfile_tested = Signal(str)
    lockfile_discovered = Signal(str)


class Dashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.preferences = lol.load_preferences()
        self.language = (
            self.preferences.get("language")
            if self.preferences.get("language") in ("en", "de")
            else "en"
        )
        self.refresh_interval_seconds = int(self.preferences.get("refresh_interval", 300) or 300)
        self.lane_assigner = None
        self.setWindowTitle("Riftboard | League of Legends")
        self.resize(1380, 900)
        self.setMinimumSize(1050, 700)
        self.signals = WorkerSignals()
        self.signals.refreshed.connect(self._refresh_complete)
        self.signals.champion_icons.connect(self._apply_champion_icons)
        self.signals.profile_icon.connect(self._apply_profile_icon)
        self.signals.profile_icon_error.connect(self._profile_icon_failed)
        self.signals.lockfile_tested.connect(self._lockfile_test_complete)
        self.signals.lockfile_discovered.connect(self._lockfile_discovered)

        self.champion_icons = {}
        self._icon_load_started = False
        self._refresh_running = False
        self._shutdown = threading.Event()
        self._profile_icon_id = None
        self._refresh_status_key = "status_loading"
        self._active_lockfile_signature = None
        self._last_lockfile_scan = 0.0
        self._lockfile_scan_running = False
        self._auto_discovered_lockfile = None
        self._lockfile_test_running = False
        self._retry_delay_seconds = 3
        self.data_ready = False

        self.icon_cache_dir = app_icon_cache_dir()
        self.mastery_sort_descending = True
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(self.refresh_interval_seconds * 1000)
        self.refresh_timer.timeout.connect(self.refresh_reports)
        self.lockfile_timer = QTimer(self)
        self.lockfile_timer.setInterval(1500)
        self.lockfile_timer.timeout.connect(self._watch_lockfile)
        self.auto_accept_timer = QTimer(self)
        self.auto_accept_timer.setInterval(5000)
        self.auto_accept_timer.timeout.connect(self._maybe_auto_accept_match)
        self.preferences["auto_accept_match"] = bool(self.preferences.get("auto_accept_match", False))
        lol.set_auto_accept_match(self.preferences.get("auto_accept_match", False))
        self._build_ui()
        self._apply_language()
        self.lockfile_timer.start()
        if self.preferences.get("auto_start_on_lockfile", False):
            if start_watcher() is None:
                self._set_connection_status(self._t("watcher_start_failed"))
        if self.preferences.get("first_run", True):
            self.open_settings(first_run=True)
            self.preferences["first_run"] = False
            try:
                lol.save_preferences(self.preferences)
            except OSError:
                self._set_connection_status("connection_save_failed")
        self._start_refresh_timer()
        self.refresh_reports()

    def _start_refresh_timer(self):
        self.refresh_timer.stop()
        self.refresh_timer.setInterval(max(5, int(self.preferences.get("refresh_interval", 300) or 300)) * 1000)
        self.refresh_timer.start()

    def _set_active_language(self, language):
        previous_language = self.language
        self.preferences["language"] = language
        try:
            lol.save_preferences(self.preferences)
        except OSError as error:
            self.preferences["language"] = previous_language
            self.language_combo.blockSignals(True)
            self.language_combo.setCurrentIndex(0 if previous_language == "en" else 1)
            self.language_combo.blockSignals(False)
            QMessageBox.critical(
                self, self._t("connection_save_failed"), str(error)
            )
            return
        self.language = language
        self._apply_language()
        if self.lane_assigner is not None:
            self.lane_assigner.retranslate()
        self._start_refresh_timer()

    def _t(self, key, fallback=None):
        translations = {
            "en": {
                "title": "RIFTBOARD",
                "subtitle": "Your League profile at a glance",
                "refresh": "Refresh",
                "refresh_display": "Refresh display",
                "refresh_display_tooltip": "Reload saved reports and redraw the current display without fetching League data.",
                "status_loading": "Loading reports…",
                "status_update": "Updating League data…",
                "update_failed": "Refresh failed",
                "overview": "Overview",
                "mastery": "Mastery",
                "matches": "Match history",
                "tft": "TFT",
                "loldle": "Daily LoLdle",
                "player_account": "PLAYER ACCOUNT",
                "games": "GAMES",
                "kda": "AVG KDA",
                "be": "BLUE ESSENCE REQUIRED",
                "honor": "HONOR",
                "lane_bias": "MOST QUEUED LANE",
                "lane_bias_unavailable": "Not enough lane data",
                "lane_bias_value": "{lanes} · {count} matches",
                "lane_mastery_bias": "MASTERY LANE BIAS",
                "lane_mastery_bias_unavailable": "Not enough mastery lane data",
                "lane_mastery_bias_value": "{lanes} · {points} mastery points",
                "lane_not_available": "Not available",
                "top5": "TOP 5 BY GAMES",
                "missing": "MISSING CHAMPIONS",
                "settings": "Settings",
                "choose_lane": "Select lane:",
                "sort_by": "Sort by:",
                "ascending": "Ascending",
                "descending": "Descending",
                "auto_accept_match": "Auto accept match",
                "all_lanes": "All lanes",
                "mastery_score": "Mastery score",
                "mastery_rank": "Mastery rank",
                "top_value": "Top value",
                "champion_name": "Champion name",
                "search_champions": "Search champions…",
                "games_label": "GAMES",
                "avg_kills": "AVG KILLS",
                "avg_deaths": "AVG DEATHS",
                "avg_assists": "AVG ASSISTS",
                "blue_essence": "BLUE ESSENCE REQUIRED",
                "honor": "HONOR",
                "top5_mastery": "TOP 5 MASTERY",
                "mastery_level": "Mastery level",
                "mastery_points": "Mastery points",
                "best_grade": "Best grade",
                "collection": "CHAMPION COLLECTION",
                "owned": "Owned",
                "not_owned": "Not owned",
                "unknown": "Unknown",
                "collection_unavailable": "Ownership information is unavailable.",
                "collection_count": "{owned} of {total} owned · {percent}% complete",
                "collection_count_partial": "{owned} champions owned · completion is unavailable",
                "ownership": "Ownership",
                "lane": "Lane",
                "other": "Other",
                "unassigned": "Unassigned",
                "date": "Date",
                "duration": "Duration",
                "match_id": "Match ID",
                "mode": "Mode",
                "result": "Result",
                "kda": "K / D / A",
                "tft_history": "TFT MATCH HISTORY",
                "games_suffix": "games",
                "average_placement": "Average placement",
                "tft_top_four": "Top-four rate",
                "tft_first_place": "First-place rate",
                "matches_analyzed": "League matches analyzed",
                "match_stats_summary": "{matches} League matches · averages use {stats} complete records",
                "reports_loaded": "Reports loaded",
                "no_reports": "No reports yet",
                "no_missing_champions": "No missing champions",
                "no_report": "No report",
                "reload": "Reload",
                "open_browser": "Open in browser",
                "loldle_hint": "Daily LoLdle · The official site is embedded here.",
                "placement": "Placement",
                "placement_out_of_8": "{placement}. place / 8",
                "champions": "Champions",
                "entries": "Entries",
                "report_match_count": "playable matches",
                "average_kda": "Avg K/D/A",
                "match_result_win": "Victory",
                "match_result_loss": "Defeat",
                "match_result_unknown": "Unknown",
                "match_history_label": "MATCH HISTORY",
                "mastery_by_lane_label": "MASTERY BY LANE",
                "settings_tab": "Settings",
                "general": "General",
                "reset_all_data": "Reset all local data…",
                "reset_all_title": "Reset all Riftboard data?",
                "reset_all_confirm": "This permanently deletes Riftboard preferences, reports, champion and lane caches, and LoLdle cookies and site storage. The app will close now; the data will be erased before the next launch, which will show first-start settings. This cannot be undone.",
                "reset_all_confirm_button": "Reset everything",
                "reset_all_failed": "Could not schedule the full reset",
                "champion_data": "Champion data",
                "client_connection": "League Client connection",
                "language": "Language",
                "open_assigner": "Open Champion Lane Assigner",
                "auto_start": "Automatically start when the League Client lockfile becomes available",
                "source_data_info": "Champion names and icons use CommunityDragon and Data Dragon. Manual lane selections are stored separately.",
                "lockfile_path": "Lockfile path",
                "browse": "Browse",
                "save": "Save",
                "test_connection": "Test connection",
                "restore_auto": "Restore automatic detection",
                "connection_status": "Connection status",
                "automatic_detection": "Automatic detection",
                "client_connected": "Connected to League Client",
                "client_not_running": "League Client not running",
                "lockfile_invalid": "Lockfile is invalid",
                "lockfile_missing": "Lockfile is missing or unreadable",
                "client_refused": "Connection refused",
                "client_not_ready": "League Client is not ready yet",
                "connection_test_ok": "Connection successful",
                "connection_test_failed": "Connection test failed",
                "connection_saved": "Connection settings saved",
                "connection_save_failed": "Could not save connection settings",
                "select_lockfile": "Select League Client lockfile",
                "invalid_lockfile": "This is not a valid League Client lockfile.",
                "watcher_start_failed": "Automatic lockfile watcher could not start",
                "assigner_title": "Champion Lane Assigner",
                "assigner_search": "Search champions…",
                "icon": "Icon",
                "champion": "Champion",
                "top": "Top",
                "jungle": "Jungle",
                "mid": "Mid",
                "bot": "Bot",
                "support": "Support",
                "cancel": "Cancel",
                "close": "Close",
                "apply": "Apply",
                "reset_standard": "Reset to Standard",
                "pending_changes": "Unsaved changes",
                "discard_title": "Discard changes?",
                "discard_pending_question": "Discard unapplied lane changes and close?",
                "unsaved_title": "Unsaved lane changes",
                "unsaved_question": "Apply or discard the pending changes before closing?",
                "apply_close": "Apply changes and close",
                "discard_close": "Discard changes and close",
                "return_assigner": "Return to the assigner",
                "continue_editing": "Continue editing",
                "yes": "Yes",
                "no": "No",
                "save_failed": "Save failed",
                "save_failed_detail": "Your pending changes are still available. Please retry.",
                "reset_title": "Reset lane assignments",
                "reset_confirm": "Refresh the champion catalog from CommunityDragon/Data Dragon and stage the standard lane checkboxes? The reset will not be saved until Apply.",
                "reset_refresh_failed": "Champion reference data could not be refreshed. No lane assignments were changed.",
                "catalog_loading": "Loading champion reference data…",
                "catalog_unavailable": "Champion reference data is unavailable. Check your connection and reopen the assigner.",
                "auto_start_tooltip": "A small background watcher remains active after the companion closes. It does not enable Windows login startup and must be started again after a Windows restart.",
                "connection_checking": "Checking League Client connection…",
                "context_assigner": "Champion Lane Assigner",
            },
            "de": {
                "title": "RIFTBOARD",
                "subtitle": "Dein League-Profil auf einen Blick",
                "refresh": "Aktualisieren",
                "refresh_display": "Anzeige aktualisieren",
                "refresh_display_tooltip": "Gespeicherte Berichte neu laden und die Anzeige aktualisieren, ohne League-Daten abzurufen.",
                "status_loading": "Berichte werden geladen …",
                "status_update": "League-Daten werden aktualisiert …",
                "update_failed": "Aktualisierung fehlgeschlagen",
                "overview": "Übersicht",
                "mastery": "Meisterschaft",
                "matches": "Match-Historie",
                "tft": "TFT",
                "loldle": "Daily LoLdle",
                "player_account": "SPIELERKONTO",
                "games": "SPIELE",
                "kda": "Ø KDA",
                "be": "BLUE ESSENCE ERFORDERLICH",
                "honor": "EHRE",
                "lane_bias": "MEISTGESPIELTE LANE",
                "lane_bias_unavailable": "Keine Lane-Daten verfügbar",
                "lane_bias_value": "{lanes} · {count} Spiele",
                "lane_mastery_bias": "MEISTERSCHAFTS-LANE-FOKUS",
                "lane_mastery_bias_unavailable": "Keine Lane-Meisterschaftsdaten",
                "lane_mastery_bias_value": "{lanes} · {points} Meisterschaftspunkte",
                "lane_not_available": "Nicht verfügbar",
                "top5": "TOP 5 NACH SPIELEN",
                "missing": "FEHLENDE CHAMPIONS",
                "settings": "Einstellungen",
                "choose_lane": "Lane auswählen:",
                "sort_by": "Sortieren nach:",
                "ascending": "Aufsteigend",
                "descending": "Absteigend",
                "auto_accept_match": "Match automatisch annehmen",
                "all_lanes": "Alle Lanes",
                "mastery_score": "Meisterschaftspunkte",
                "mastery_rank": "Meisterschaftsrang",
                "top_value": "Topwertung",
                "champion_name": "Championname",
                "search_champions": "Champions suchen…",
                "games_label": "SPIELE",
                "avg_kills": "Ø KILLS",
                "avg_deaths": "Ø TODE",
                "avg_assists": "Ø ASSISTS",
                "blue_essence": "BENÖTIGTE BLAUE ESSENZ",
                "honor": "EHRE",
                "top5_mastery": "TOP 5 MEISTERSCHAFT",
                "mastery_level": "Meisterschaftsstufe",
                "mastery_points": "Meisterschaftspunkte",
                "best_grade": "Beste Wertung",
                "collection": "CHAMPION-SAMMLUNG",
                "owned": "Im Besitz",
                "not_owned": "Nicht im Besitz",
                "unknown": "Unbekannt",
                "collection_unavailable": "Besitzinformationen sind nicht verfügbar.",
                "collection_count": "{owned} von {total} im Besitz · {percent}% vollständig",
                "collection_count_partial": "{owned} Champions im Besitz · Vollständigkeit nicht verfügbar",
                "ownership": "Besitz",
                "lane": "Lane",
                "other": "Andere",
                "unassigned": "Nicht zugewiesen",
                "date": "Datum",
                "duration": "Dauer",
                "match_id": "Match-ID",
                "mode": "Modus",
                "result": "Ergebnis",
                "kda": "K / T / A",
                "tft_history": "TFT-MATCH-HISTORIE",
                "games_suffix": "Spiele",
                "average_placement": "Durchschnittliche Platzierung",
                "tft_top_four": "Top-4-Rate",
                "tft_first_place": "Siegesrate",
                "matches_analyzed": "League-Matches ausgewertet",
                "match_stats_summary": "{matches} League-Matches · Durchschnitte aus {stats} vollständigen Spielen",
                "reports_loaded": "Berichte geladen",
                "no_reports": "Noch keine Berichte vorhanden",
                "no_missing_champions": "Keine fehlenden Champions",
                "no_report": "Kein Bericht",
                "reload": "Neu laden",
                "open_browser": "Im Browser öffnen",
                "loldle_hint": "Daily LoLdle · Die offizielle Website ist hier eingebettet.",
                "placement": "Platzierung",
                "placement_out_of_8": "{placement}. Platz / 8",
                "champions": "Championen",
                "entries": "Einträge",
                "report_match_count": "spielbare Matches",
                "average_kda": "Ø K/D/A",
                "match_result_win": "Sieg",
                "match_result_loss": "Niederlage",
                "match_result_unknown": "Unbekannt",
                "match_history_label": "MATCH-HISTORIE",
                "mastery_by_lane_label": "MASTERY NACH LANE",
                "settings_tab": "Einstellungen",
                "general": "Allgemein",
                "reset_all_data": "Alle lokalen Daten zurücksetzen…",
                "reset_all_title": "Alle Riftboard-Daten zurücksetzen?",
                "reset_all_confirm": "Dadurch werden Riftboard-Einstellungen, Berichte, Champion- und Lane-Caches sowie LoLdle-Cookies und Website-Speicher dauerhaft gelöscht. Die Anwendung wird jetzt geschlossen; die Daten werden vor dem nächsten Start gelöscht und die Ersteinrichtung wird angezeigt. Dieser Vorgang kann nicht rückgängig gemacht werden.",
                "reset_all_confirm_button": "Alles zurücksetzen",
                "reset_all_failed": "Vollständiges Zurücksetzen konnte nicht vorgemerkt werden",
                "champion_data": "Champion-Daten",
                "client_connection": "League-Client-Verbindung",
                "language": "Sprache",
                "open_assigner": "Champion-Lane-Zuweisung öffnen",
                "auto_start": "Automatisch starten, sobald die League-Client-Lockfile verfügbar ist",
                "source_data_info": "Champion-Namen und Symbole stammen von CommunityDragon und Data Dragon. Manuelle Lane-Auswahlen werden getrennt gespeichert.",
                "lockfile_path": "Lockfile-Pfad",
                "browse": "Durchsuchen",
                "save": "Speichern",
                "test_connection": "Verbindung testen",
                "restore_auto": "Automatische Erkennung wiederherstellen",
                "connection_status": "Verbindungsstatus",
                "automatic_detection": "Automatische Erkennung",
                "client_connected": "Mit dem League Client verbunden",
                "client_not_running": "League Client wird nicht ausgeführt",
                "lockfile_invalid": "Lockfile ist ungültig",
                "lockfile_missing": "Lockfile fehlt oder ist nicht lesbar",
                "client_refused": "Verbindung abgelehnt",
                "client_not_ready": "League Client ist noch nicht bereit",
                "connection_test_ok": "Verbindung erfolgreich",
                "connection_test_failed": "Verbindungstest fehlgeschlagen",
                "connection_saved": "Verbindungseinstellungen gespeichert",
                "connection_save_failed": "Verbindungseinstellungen konnten nicht gespeichert werden",
                "select_lockfile": "League-Client-Lockfile auswählen",
                "invalid_lockfile": "Dies ist keine gültige League-Client-Lockfile.",
                "watcher_start_failed": "Automatischer Lockfile-Wächter konnte nicht gestartet werden",
                "assigner_title": "Champion-Lane-Zuweisung",
                "assigner_search": "Champions suchen…",
                "icon": "Symbol",
                "champion": "Champion",
                "top": "Top",
                "jungle": "Jungle",
                "mid": "Mitte",
                "bot": "Bot",
                "support": "Support",
                "cancel": "Abbrechen",
                "close": "Schließen",
                "apply": "Übernehmen",
                "reset_standard": "Standard wiederherstellen",
                "pending_changes": "Ungespeicherte Änderungen",
                "discard_title": "Änderungen verwerfen?",
                "discard_pending_question": "Nicht gespeicherte Lane-Änderungen verwerfen und schließen?",
                "unsaved_title": "Ungespeicherte Lane-Änderungen",
                "unsaved_question": "Änderungen vor dem Schließen übernehmen oder verwerfen?",
                "apply_close": "Änderungen übernehmen und schließen",
                "discard_close": "Änderungen verwerfen und schließen",
                "return_assigner": "Zur Zuweisung zurückkehren",
                "continue_editing": "Weiter bearbeiten",
                "yes": "Ja",
                "no": "Nein",
                "save_failed": "Speichern fehlgeschlagen",
                "save_failed_detail": "Die Änderungen sind weiterhin vorhanden. Bitte erneut versuchen.",
                "reset_title": "Lane-Zuweisungen zurücksetzen",
                "reset_confirm": "Champion-Katalog aus CommunityDragon/Data Dragon neu laden und Standard-Lanes vorläufig setzen? Gespeichert wird erst mit „Übernehmen“.",
                "reset_refresh_failed": "Champion-Referenzdaten konnten nicht aktualisiert werden. Lane-Zuweisungen wurden nicht geändert.",
                "catalog_loading": "Champion-Referenzdaten werden geladen…",
                "catalog_unavailable": "Champion-Referenzdaten sind nicht verfügbar. Verbindung prüfen und die Zuweisung erneut öffnen.",
                "auto_start_tooltip": "Ein kleiner Hintergrund-Wächter bleibt nach dem Schließen aktiv. Der Windows-Autostart wird nicht aktiviert; nach einem Windows-Neustart muss die Anwendung erneut gestartet werden.",
                "connection_checking": "League-Client-Verbindung wird geprüft…",
                "context_assigner": "Champion-Lane-Zuweisung",
            },
        }
        language = self.language if self.language in translations else "en"
        return translations[language].get(key, fallback if fallback is not None else key)

    def _translate_match_result(self, result):
        normalized = str(result or "").strip()
        if self.language == "de":
            return {
                "SIEG": "Sieg",
                "NIEDERLAGE": "Niederlage",
                "UNBEKANNT": self._t("match_result_unknown"),
            }.get(normalized, normalized)
        return {
            "SIEG": "Victory",
            "NIEDERLAGE": "Defeat",
            "UNBEKANNT": self._t("match_result_unknown"),
        }.get(normalized, normalized)

    def _translate_ownership(self, value):
        normalized = str(value or "").strip()
        if self.language == "de":
            return {"Owned": "Besessen", "Unowned": "Nicht im Besitz", "Unknown": "Unbekannt"}.get(normalized, normalized)
        return {"Owned": "Owned", "Unowned": "Unowned", "Unknown": "Unknown"}.get(normalized, normalized)

    def _toggle_auto_accept_match(self, checked):
        self.preferences["auto_accept_match"] = bool(checked)
        lol.set_auto_accept_match(bool(checked))
        lol.save_preferences(self.preferences)
        if checked:
            self.auto_accept_timer.start()
        else:
            self.auto_accept_timer.stop()

    def _maybe_auto_accept_match(self):
        if not getattr(self, "auto_accept_checkbox", None) or not self.auto_accept_checkbox.isChecked():
            return
        try:
            if lol.auto_accept_match():
                self.status_label.setText(self._t("auto_accept_match"))
        except Exception:
            pass

    def _apply_language(self):
        text = {
            "en": {
                "title": "RIFTBOARD",
                "subtitle": "Your League profile at a glance",
                "refresh": "Refresh",
                "status_loading": "Loading reports…",
                "settings": "Settings",
                "overview": "Overview",
                "mastery": "Mastery",
                "matches": "Match history",
                "tft": "TFT",
                "loldle": "Daily LoLdle",
                "choose_lane": "Select lane:",
                "sort_by": "Sort by:",
                "ascending": "Ascending",
                "descending": "Descending",
                "auto_accept_match": "Auto accept match",
                "all_lanes": "All lanes",
                "mastery_score": "Mastery score",
                "mastery_rank": "Mastery rank",
                "top_value": "Top value",
                "champions": "Champions",
                "entries": "Entries",
            },
            "de": {
                "title": "RIFTBOARD",
                "subtitle": "Dein League-Profil auf einen Blick",
                "refresh": "Aktualisieren",
                "status_loading": "Berichte werden geladen …",
                "settings": "Einstellungen",
                "overview": "Übersicht",
                "mastery": "Meisterschaft",
                "matches": "Match-Historie",
                "tft": "TFT",
                "loldle": "Daily LoLdle",
                "choose_lane": "Lane auswählen:",
                "sort_by": "Sortieren nach:",
                "ascending": "Aufsteigend",
                "descending": "Absteigend",
                "auto_accept_match": "Match automatisch annehmen",
                "all_lanes": "Alle Lanes",
                "mastery_score": "Meisterschaftspunkte",
                "mastery_rank": "Meisterschaftsrang",
                "top_value": "Topwertung",
                "champions": "Championen",
                "entries": "Einträge",
            },
        }.get(self.language, {
            "title": "RIFTBOARD",
            "subtitle": "Your League profile at a glance",
            "refresh": "Refresh",
            "status_loading": "Loading reports…",
            "settings": "Settings",
            "overview": "Overview",
            "mastery": "Mastery",
            "matches": "Match history",
            "tft": "TFT",
            "loldle": "Daily LoLdle",
            "choose_lane": "Select lane:",
            "sort_by": "Sort by:",
            "ascending": "Ascending",
            "descending": "Descending",
            "auto_accept_match": "Auto accept match",
            "all_lanes": "All lanes",
            "mastery_score": "Mastery score",
            "mastery_rank": "Mastery rank",
            "top_value": "Top value",
            "champions": "Champions",
            "entries": "Entries",
        })

        if hasattr(self, "title_label"):
            self.title_label.setText(text["title"])
        if hasattr(self, "subtitle_label"):
            self.subtitle_label.setText(text["subtitle"])
        if hasattr(self, "status_label"):
            self.status_label.setText(self._t(self._refresh_status_key))
        if hasattr(self, "refresh_button"):
            self.refresh_button.setText(text["refresh"])
        if hasattr(self, "refresh_display_button"):
            self.refresh_display_button.setText(self._t("refresh_display"))
            self.refresh_display_button.setToolTip(self._t("refresh_display_tooltip"))
        if hasattr(self, "settings_button"):
            self.settings_button.setText(text["settings"])
        if hasattr(self, "tabs"):
            self.tabs.setTabText(0, text["overview"])
            self.tabs.setTabText(1, text["mastery"])
            self.tabs.setTabText(2, text["matches"])
            self.tabs.setTabText(3, text["tft"])
            self.tabs.setTabText(4, text["loldle"])
            self.tabs.setTabText(5, self._t("settings_tab"))
        if hasattr(self, "lane_filter_label"):
            self.lane_filter_label.setText(text["choose_lane"])
        if hasattr(self, "sort_label"):
            self.sort_label.setText(text["sort_by"])
        if hasattr(self, "ascending_button"):
            self.ascending_button.setText(text["ascending"])
        if hasattr(self, "descending_button"):
            self.descending_button.setText(text["descending"])
        if hasattr(self, "auto_accept_checkbox"):
            self.auto_accept_checkbox.setText(text["auto_accept_match"])
            self.auto_accept_checkbox.setChecked(lol.AUTO_ACCEPT_MATCH)
        if hasattr(self, "lane_filter"):
            current = self.lane_filter.currentData()
            options = [("all_lanes", None), ("top", "Top"), ("jungle", "Jungle"),
                       ("mid", "Mid"), ("bot", "Bot"), ("support", "Support")]
            self.lane_filter.blockSignals(True)
            self.lane_filter.clear()
            for label_key, lane in options:
                self.lane_filter.addItem(self._t(label_key), lane)
            index = self.lane_filter.findData(current)
            self.lane_filter.setCurrentIndex(index if index >= 0 else 0)
            self.lane_filter.blockSignals(False)
        if hasattr(self, "sort_column"):
            current = self.sort_column.currentData()
            self.sort_column.blockSignals(True)
            options = [
                ("champion_name", "champion_name"),
                ("mastery_score", "mastery_score"),
                ("mastery_rank", "mastery_rank"),
                ("top_value", "top_value"),
            ]
            self.sort_column.clear()
            for key, label_key in options:
                self.sort_column.addItem(self._t(label_key), key)
            index = self.sort_column.findData(current)
            self.sort_column.setCurrentIndex(index if index >= 0 else 0)
            self.sort_column.blockSignals(False)
        if hasattr(self, "mastery_tree"):
            self._update_sort_headers()
            self._populate_mastery()
        if hasattr(self, "match_tree"):
            self._populate_matches()
        if hasattr(self, "tft_tree"):
            self._populate_tft()
        if hasattr(self, "collection_tree"):
            self._populate_collection()
        if hasattr(self, "top_tree"):
            self._populate_top_champions()
        self._retranslate_overview()
        self._retranslate_settings()

    def _queue_game(self, queue_id):
        try:
            ok = lol.queue_for_game(queue_id)
            if ok:
                self.status_label.setText(f"Queue {queue_id} started")
        except Exception as exc:
            QMessageBox.warning(self, "Queue launch failed", str(exc))

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        self.title_label = QLabel("RIFTBOARD")
        self.title_label.setObjectName("pageTitle")
        self.subtitle_label = QLabel("Dein League-Profil auf einen Blick")
        self.subtitle_label.setObjectName("muted")
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label)
        header.addLayout(titles)
        header.addStretch(1)
        self.status_label = QLabel("Berichte werden geladen …")
        self.status_label.setObjectName("muted")
        header.addWidget(self.status_label)
        self.refresh_button = QPushButton("Aktualisieren")
        self.refresh_button.setObjectName("accentButton")
        self.refresh_button.clicked.connect(self.refresh_reports)
        header.addWidget(self.refresh_button)
        self.refresh_display_button = QPushButton(self._t("refresh_display"))
        self.refresh_display_button.setToolTip(self._t("refresh_display_tooltip"))
        self.refresh_display_button.clicked.connect(self.refresh_display)
        header.addWidget(self.refresh_display_button)
        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(lambda: self.open_settings(False))
        header.addWidget(self.settings_button)
        root.addLayout(header)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.overview_tab = QWidget()
        self.mastery_tab = QWidget()
        self.matches_tab = QWidget()
        self.tft_tab = QWidget()
        self.loldle_tab = QWidget()
        self.tabs.addTab(self.overview_tab, "Übersicht")
        self.tabs.addTab(self.mastery_tab, "Meisterschaft")
        self.tabs.addTab(self.matches_tab, "Match-Historie")
        self.tabs.addTab(self.tft_tab, "TFT")
        self.tabs.addTab(self.loldle_tab, "Daily LoLdle")
        self.settings_tab = QWidget()
        self.tabs.addTab(self.settings_tab, "Settings")
        self._build_overview()
        self._build_mastery()
        self._build_matches()
        self._build_tft()
        self._build_loldle()
        self._build_settings()
        context_widgets = [
            self,
            central,
            self.tabs,
            self.overview_tab,
            self.mastery_tab,
            self.matches_tab,
            self.tft_tab,
            self.loldle_tab,
            self.settings_tab,
            self.mastery_tree,
            self.match_tree,
            self.tft_tree,
            self.collection_tree,
        ]
        for widget in context_widgets:
            widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(self._show_context_menu)
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; }}
            QLabel {{ color: {TEXT}; }}
            QLabel#muted {{ color: {MUTED}; }}
            QLabel#pageTitle {{ font-size: 24px; font-weight: 700; letter-spacing: 2px; }}
            QLabel#panelTitle {{ color: {ACCENT}; font-size: 11px; font-weight: 700; }}
            QLabel#metricValue {{ font-size: 20px; font-weight: 700; }}
            QFrame#panel, QFrame#metricCard {{ background: {PANEL}; border-radius: 9px; }}
            QTabWidget::pane {{ border: 0; background: {BG}; }}
            QTabBar::tab {{
                background: {PANEL}; color: {MUTED}; padding: 11px 17px;
                border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 3px;
            }}
            QTabBar::tab:selected {{ background: {PANEL_ALT}; color: {ACCENT}; }}
            QTreeWidget {{
                background: {PANEL}; alternate-background-color: {PANEL_ALT};
                color: {TEXT}; border: 0; border-radius: 6px; outline: 0;
                selection-background-color: #34445b;
            }}
            QTreeWidget::item {{ padding: 5px 4px; }}
            QHeaderView::section {{
                background: {PANEL_ALT}; color: {ACCENT}; padding: 9px 7px;
                border: 0; font-weight: 700;
            }}
            QComboBox {{
                background: {PANEL_ALT}; color: {TEXT}; padding: 7px 10px;
                border: 1px solid #30394a; border-radius: 5px;
            }}
            QPushButton {{
                background: {PANEL_ALT}; color: {TEXT}; padding: 8px 12px;
                border: 0; border-radius: 5px;
            }}
            QPushButton:hover {{ background: #30394a; }}
            QPushButton#accentButton {{ background: {ACCENT}; color: #151922; font-weight: 700; padding: 9px 16px; }}
            QPushButton#accentButton:hover {{ background: #dbc184; }}
            QPushButton#dangerButton {{ background: #6b303a; color: {TEXT}; }}
            QPushButton#dangerButton:hover {{ background: #8b3b49; }}
            """
        )

    def _build_overview(self):
        outer = QVBoxLayout(self.overview_tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        outer.addWidget(scroll)
        scroll.setWidget(content)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        upper = QHBoxLayout()
        profile_panel, profile_layout = self._panel("PLAYER ACCOUNT")
        profile_row = QHBoxLayout()
        profile_row.setSpacing(15)
        self.profile_icon_label = QLabel("?")
        self.profile_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_icon_label.setStyleSheet(
            f"background: {PANEL_ALT}; color: {ACCENT}; border-radius: 8px; font-size: 22px; font-weight: 700;"
        )
        self.profile_icon_label.setFixedSize(84, 84)
        profile_row.addWidget(self.profile_icon_label)
        details = QVBoxLayout()
        self.account_label = QLabel("Loading account data …")
        self.account_label.setFont(QFont("Segoe UI", 20, QFont.Weight.DemiBold))
        self.account_label.setWordWrap(True)
        self.level_label = QLabel("")
        self.level_label.setObjectName("muted")
        details.addWidget(self.account_label)
        details.addWidget(self.level_label)
        self.lane_bias_label = QLabel(self._t("lane_bias_unavailable"))
        self.lane_bias_label.setObjectName("muted")
        details.addWidget(self.lane_bias_label)
        self.lane_mastery_bias_label = QLabel(
            self._t("lane_mastery_bias_unavailable")
        )
        self.lane_mastery_bias_label.setObjectName("muted")
        details.addWidget(self.lane_mastery_bias_label)
        details.addStretch(1)
        profile_row.addLayout(details, 1)
        profile_layout.addLayout(profile_row, 1)
        upper.addWidget(profile_panel, 1)

        metrics = QHBoxLayout()
        metrics.setSpacing(9)
        self.games_value = self._metric_card(metrics, "GAMES")
        self.avg_kills_value = self._metric_card(metrics, "AVG KILLS")
        self.avg_deaths_value = self._metric_card(metrics, "AVG DEATHS")
        self.avg_assists_value = self._metric_card(metrics, "AVG ASSISTS")
        self.be_value = self._metric_card(metrics, "BLUE ESSENCE REQUIRED")
        self.honor_value = self._metric_card(metrics, "HONOR")
        upper.addLayout(metrics, 2)
        layout.addLayout(upper)

        self.auto_accept_checkbox = QCheckBox(self._t("auto_accept_match"))
        self.auto_accept_checkbox.setChecked(lol.AUTO_ACCEPT_MATCH)
        self.auto_accept_checkbox.toggled.connect(self._toggle_auto_accept_match)
        layout.addWidget(self.auto_accept_checkbox)

        lower = QHBoxLayout()
        lower.setSpacing(12)
        top_panel, top_layout = self._panel("TOP 5 MASTERY")
        self.top_tree = self._tree(top_layout, ("", "Champion", "Mastery level", "Mastery points", "Best grade"), (42, 145, 115, 130, 95))
        self.top_tree.setIconSize(self._icon_size(28))
        lower.addWidget(top_panel, 1)

        missing_panel, missing_layout = self._panel("MISSING CHAMPIONS")
        self.missing_tree = self._tree(missing_layout, ("", "Champion", "Price"), (42, 155, 105))
        self.missing_tree.setIconSize(self._icon_size(28))
        lower.addWidget(missing_panel, 1)
        layout.addLayout(lower, 1)

        collection_panel, collection_layout = self._panel("CHAMPION COLLECTION")
        self.collection_summary = QLabel("")
        self.collection_summary.setObjectName("muted")
        collection_layout.addWidget(self.collection_summary)
        self.collection_search = QLineEdit()
        self.collection_search.setPlaceholderText(self._t("search_champions"))
        self.collection_search.textChanged.connect(self._filter_collection)
        collection_layout.addWidget(self.collection_search)
        self.collection_tree = self._tree(
            collection_layout,
            ("", "Champion", "Ownership"),
            (42, 230, 160),
        )
        self.collection_tree.setIconSize(self._icon_size(24))
        layout.addWidget(collection_panel, 1)

    def _retranslate_overview(self):
        if not hasattr(self, "overview_tab"):
            return
        title_keys = {
            "PLAYER ACCOUNT": "player_account",
            "TOP 5 MASTERY": "top5_mastery",
            "MISSING CHAMPIONS": "missing",
            "CHAMPION COLLECTION": "collection",
            "MATCH HISTORY": "match_history_label",
            "MASTERY BY LANE": "mastery_by_lane_label",
            "TFT MATCH HISTORY": "tft_history",
        }
        for label in self.findChildren(QLabel, "panelTitle"):
            key = title_keys.get(label.property("source_title"))
            if key:
                label.setText(self._t(key))
        captions = (
            ("games_value", "games_label"),
            ("avg_kills_value", "avg_kills"),
            ("avg_deaths_value", "avg_deaths"),
            ("avg_assists_value", "avg_assists"),
            ("be_value", "blue_essence"),
            ("honor_value", "honor"),
        )
        for attribute, key in captions:
            metric = getattr(self, attribute, None)
            if metric is None:
                continue
            labels = metric.parentWidget().findChildren(QLabel)
            if labels:
                labels[0].setText(self._t(key))
        if hasattr(self, "top_tree"):
            self.top_tree.setHeaderLabels(
                (
                    "",
                    self._t("champion"),
                    self._t("mastery_level"),
                    self._t("mastery_points"),
                    self._t("best_grade"),
                )
            )
        if hasattr(self, "tft_tree"):
            self.tft_tree.setHeaderLabels(
                (
                    self._t("date"),
                    self._t("match_id"),
                    self._t("mode"),
                    self._t("placement"),
                )
            )
        if hasattr(self, "match_summary"):
            self.match_summary.setText(
                self._t("match_stats_summary").format(
                    matches=len(getattr(self, "match_rows", [])),
                    stats=getattr(self, "league_stat_match_count", 0),
                )
            )
        if hasattr(self, "lane_bias_label"):
            self._update_lane_bias_label()
        if hasattr(self, "lane_mastery_bias_label"):
            self._update_lane_mastery_bias_label()
        if hasattr(self, "collection_tree"):
            self.collection_tree.setHeaderLabels(
                ("", self._t("champion"), self._t("ownership"))
            )
        if hasattr(self, "collection_search"):
            self.collection_search.setPlaceholderText(self._t("search_champions"))
        if hasattr(self, "mastery_search"):
            self.mastery_search.setPlaceholderText(self._t("search_champions"))
        if hasattr(self, "match_tree"):
            self.match_tree.setHeaderLabels(
                (
                    "",
                    self._t("date"),
                    self._t("match_id"),
                    self._t("mode"),
                    self._t("result"),
                    self._t("champion"),
                    self._t("lane"),
                    self._t("duration"),
                    self._t("kda"),
                )
            )
        if hasattr(self, "loldle_hint"):
            self.loldle_hint.setText(self._t("loldle_hint"))
            self.loldle_reload_button.setText(self._t("reload"))
            self.loldle_open_button.setText(self._t("open_browser"))
        if hasattr(self, "mastery_tree"):
            self.mastery_tree.setHeaderLabels(
                (
                    "",
                    self._t("champion"),
                    self._t("lane"),
                    self._t("mastery_score"),
                    self._t("ownership"),
                    self._t("mastery_rank"),
                    self._t("top_value"),
                )
            )

    def _queue_game(self, queue_id):
        try:
            ok = lol.queue_for_game(queue_id)
            if ok:
                self.status_label.setText(f"Queue {queue_id} started")
        except Exception as exc:
            QMessageBox.warning(self, "Queue launch failed", str(exc))

    def _update_lane_bias_label(self):
        if not hasattr(self, "lane_bias_label"):
            return
        counts = getattr(self, "lane_bias_counts", {})
        highest = max(counts.values(), default=0)
        if highest <= 0:
            self.lane_bias_label.setText(self._t("lane_bias_unavailable"))
            return
        lanes = " / ".join(
            self._t(lane.casefold())
            for lane in lol.LANES
            if counts.get(lane) == highest
        )
        self.lane_bias_label.setText(
            f"{self._t('lane_bias')}: "
            f"{self._t('lane_bias_value').format(lanes=lanes, count=highest)}"
        )

    def _update_lane_mastery_bias_label(self):
        if not hasattr(self, "lane_mastery_bias_label"):
            return
        counts = getattr(self, "lane_mastery_bias_counts", {})
        highest = max(counts.values(), default=0)
        if highest <= 0:
            self.lane_mastery_bias_label.setText(
                self._t("lane_mastery_bias_unavailable")
            )
            return
        lanes = " / ".join(
            self._t(lane.casefold())
            for lane in lol.LANES
            if counts.get(lane) == highest
        )
        self.lane_mastery_bias_label.setText(
            f"{self._t('lane_mastery_bias')}: "
            f"{self._t('lane_mastery_bias_value').format(lanes=lanes, points=f'{highest:,}')}"
        )

    def _build_mastery(self):
        layout = QVBoxLayout(self.mastery_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        controls = QHBoxLayout()
        self.lane_filter_label = QLabel("Select lane:")
        controls.addWidget(self.lane_filter_label)
        self.lane_filter = QComboBox()
        for label, lane in (
            ("All lanes", None),
            ("Top", "Top"),
            ("Jungle", "Jungle"),
            ("Mid", "Mid"),
            ("Bot", "Bot"),
            ("Support", "Support"),
        ):
            self.lane_filter.addItem(label, lane)
        self.lane_filter.currentTextChanged.connect(self._populate_mastery)
        controls.addWidget(self.lane_filter)
        controls.addSpacing(14)
        self.sort_label = QLabel("Sort by:")
        controls.addWidget(self.sort_label)
        self.sort_column = QComboBox()
        for key in ("champion_name", "mastery_score", "mastery_rank", "top_value"):
            self.sort_column.addItem(self._t(key), key)
        self.sort_column.currentTextChanged.connect(self._on_sort_column_changed)
        controls.addWidget(self.sort_column)
        self.ascending_button = QPushButton("↑ Ascending")
        self.descending_button = QPushButton("↓ Descending")
        self.ascending_button.clicked.connect(lambda: self._set_mastery_sort(False))
        self.descending_button.clicked.connect(lambda: self._set_mastery_sort(True))
        controls.addWidget(self.ascending_button)
        controls.addWidget(self.descending_button)
        controls.addStretch(1)
        self.mastery_count = QLabel("")
        self.mastery_count.setObjectName("muted")
        controls.addWidget(self.mastery_count)
        self.mastery_search = QLineEdit()
        self.mastery_search.setPlaceholderText(self._t("search_champions"))
        self.mastery_search.textChanged.connect(self._populate_mastery)
        controls.addWidget(self.mastery_search, 1)
        layout.addLayout(controls)

        panel, panel_layout = self._panel("MASTERY BY LANE")
        self.mastery_tree = self._tree(
            panel_layout,
            ("", "Champion", "Lane", "Mastery score", "Owned", "Mastery rank", "Top value"),
            (42, 200, 145, 140, 95, 115, 110),
        )
        self.mastery_tree.setIconSize(self._icon_size(28))
        self.mastery_tree.setSortingEnabled(False)
        self.mastery_tree.header().sectionClicked.connect(self._mastery_header_clicked)
        layout.addWidget(panel, 1)

    def _build_matches(self):
        layout = QVBoxLayout(self.matches_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        self.match_summary = QLabel("")
        self.match_summary.setObjectName("muted")
        layout.addWidget(self.match_summary)
        panel, panel_layout = self._panel("MATCH HISTORY")
        self.match_tree = self._tree(
            panel_layout,
            ("", "Date", "Match ID", "Mode", "Result", "Champion", "Lane", "Duration", "K / D / A"),
            (42, 145, 135, 150, 110, 190, 90, 95, 130),
        )
        self.match_tree.setIconSize(self._icon_size(28))
        layout.addWidget(panel, 1)

    def _build_tft(self):
        layout = QVBoxLayout(self.tft_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        summary = QHBoxLayout()
        self.tft_count = QLabel("–")
        self.tft_count.setObjectName("metricValue")
        self.tft_average = QLabel("–")
        self.tft_average.setObjectName("muted")
        self.tft_top_four = QLabel("–")
        self.tft_first_place = QLabel("–")
        summary.addWidget(self.tft_count)
        summary.addStretch(1)
        summary.addWidget(self.tft_average)
        summary.addSpacing(18)
        summary.addWidget(self.tft_top_four)
        summary.addSpacing(18)
        summary.addWidget(self.tft_first_place)
        layout.addLayout(summary)
        panel, panel_layout = self._panel("TFT MATCH HISTORY")
        self.tft_tree = self._tree(
            panel_layout,
            ("Date", "Match ID", "Mode", "Placement"),
            (190, 200, 150, 150),
        )
        layout.addWidget(panel, 1)

    def _build_loldle(self):
        layout = QVBoxLayout(self.loldle_tab)
        layout.setContentsMargins(0, 8, 0, 0)
        toolbar = QHBoxLayout()
        self.loldle_hint = QLabel()
        self.loldle_hint.setObjectName("muted")
        toolbar.addWidget(self.loldle_hint)
        toolbar.addStretch(1)
        self.loldle_reload_button = QPushButton()
        self.loldle_reload_button.clicked.connect(
            lambda: self.loldle_view.setUrl(QUrl(LOLDLE_URL))
        )
        toolbar.addWidget(self.loldle_reload_button)
        self.loldle_open_button = QPushButton()
        self.loldle_open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(LOLDLE_URL))
        )
        toolbar.addWidget(self.loldle_open_button)
        layout.addLayout(toolbar)
        self.loldle_view = QWebEngineView()
        self.loldle_profile = create_loldle_profile(self)
        self.loldle_page = QWebEnginePage(self.loldle_profile, self.loldle_view)
        self.loldle_page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalStorageEnabled, True
        )
        self.loldle_view.setPage(self.loldle_page)
        self.loldle_view.setUrl(QUrl(LOLDLE_URL))
        layout.addWidget(self.loldle_view, 1)

    def _build_settings(self):
        layout = QVBoxLayout(self.settings_tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        general = QGroupBox()
        general_layout = QVBoxLayout(general)
        language_row = QHBoxLayout()
        self.language_label = QLabel()
        self.language_combo = QComboBox()
        self.language_combo.currentIndexChanged.connect(self._language_selection_changed)
        language_row.addWidget(self.language_label)
        language_row.addWidget(self.language_combo)
        language_row.addStretch(1)
        general_layout.addLayout(language_row)
        self.auto_start_checkbox = QCheckBox()
        self.auto_start_checkbox.toggled.connect(self._auto_start_changed)
        general_layout.addWidget(self.auto_start_checkbox)
        self.reset_all_data_button = QPushButton()
        self.reset_all_data_button.setObjectName("dangerButton")
        self.reset_all_data_button.clicked.connect(self._reset_all_data)
        general_layout.addWidget(
            self.reset_all_data_button, 0, Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(general)
        self.general_group = general

        champion_data = QGroupBox()
        champion_data_layout = QVBoxLayout(champion_data)
        self.champion_data_description = QLabel()
        self.champion_data_description.setWordWrap(True)
        champion_data_layout.addWidget(self.champion_data_description)
        self.open_assigner_button = QPushButton()
        self.open_assigner_button.clicked.connect(self.open_lane_assigner)
        champion_data_layout.addWidget(self.open_assigner_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(champion_data)
        self.champion_data_group = champion_data

        connection = QGroupBox()
        connection_layout = QVBoxLayout(connection)
        self.connection_status_label = QLabel()
        self.connection_status_label.setObjectName("muted")
        connection_layout.addWidget(self.connection_status_label)
        self.current_lockfile_label = QLabel()
        self.current_lockfile_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.current_lockfile_label.setWordWrap(True)
        connection_layout.addWidget(self.current_lockfile_label)

        lockfile_row = QHBoxLayout()
        self.lockfile_path_edit = QLineEdit()
        self.lockfile_browse_button = QPushButton()
        self.lockfile_browse_button.clicked.connect(self._browse_lockfile)
        lockfile_row.addWidget(self.lockfile_path_edit, 1)
        lockfile_row.addWidget(self.lockfile_browse_button)
        connection_layout.addLayout(lockfile_row)
        actions = QHBoxLayout()
        self.lockfile_save_button = QPushButton()
        self.lockfile_save_button.clicked.connect(self._save_connection_settings)
        self.lockfile_test_button = QPushButton()
        self.lockfile_test_button.clicked.connect(self._test_lockfile_connection)
        self.lockfile_auto_button = QPushButton()
        self.lockfile_auto_button.clicked.connect(self._restore_automatic_detection)
        actions.addWidget(self.lockfile_save_button)
        actions.addWidget(self.lockfile_test_button)
        actions.addWidget(self.lockfile_auto_button)
        actions.addStretch(1)
        connection_layout.addLayout(actions)
        layout.addWidget(connection)
        self.connection_group = connection
        layout.addStretch(1)

        self.lockfile_path_edit.setText(str(self.preferences.get("lockfile_path") or ""))
        self._connection_status_key = "client_not_running"
        self._retranslate_settings()

    def _retranslate_settings(self):
        if not hasattr(self, "settings_tab"):
            return
        self.general_group.setTitle(self._t("general"))
        self.champion_data_group.setTitle(self._t("champion_data"))
        self.connection_group.setTitle(self._t("client_connection"))
        self.language_label.setText(self._t("language") + ":")
        self.auto_start_checkbox.setText(self._t("auto_start"))
        self.auto_start_checkbox.setToolTip(
            self._t("auto_start_tooltip")
        )
        self.reset_all_data_button.setText(self._t("reset_all_data"))
        self.champion_data_description.setText(self._t("source_data_info"))
        self.open_assigner_button.setText(self._t("open_assigner"))
        self.lockfile_path_edit.setPlaceholderText(self._t("lockfile_path"))
        self.lockfile_browse_button.setText(self._t("browse"))
        self.lockfile_save_button.setText(self._t("save"))
        self.lockfile_test_button.setText(self._t("test_connection"))
        self.lockfile_auto_button.setText(self._t("restore_auto"))
        self.connection_status_label.setText(
            f"{self._t('connection_status')}: {self._t(self._connection_status_key)}"
        )
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        self.language_combo.addItems(["English", "Deutsch"])
        self.language_combo.setCurrentIndex(0 if self.language == "en" else 1)
        self.language_combo.blockSignals(False)
        self.auto_start_checkbox.blockSignals(True)
        self.auto_start_checkbox.setChecked(
            bool(self.preferences.get("auto_start_on_lockfile", False))
        )
        self.auto_start_checkbox.blockSignals(False)
        self._update_current_lockfile_label()

    def _set_connection_status(self, key):
        self._connection_status_key = key
        if hasattr(self, "connection_status_label"):
            self.connection_status_label.setText(
                f"{self._t('connection_status')}: {self._t(key)}"
            )

    def _update_current_lockfile_label(self, path=None):
        if not hasattr(self, "current_lockfile_label"):
            return
        if path is None:
            path = str(self.preferences.get("lockfile_path") or "").strip()
            if not path:
                current = self._find_active_lockfile_path()
                path = str(current) if current is not None else self._t("automatic_detection")
        self.current_lockfile_label.setText(
            f"{self._t('lockfile_path')}: {path or self._t('automatic_detection')}"
        )

    def _language_selection_changed(self, index):
        self._set_active_language("en" if index == 0 else "de")

    def _auto_start_changed(self, enabled):
        previous = bool(self.preferences.get("auto_start_on_lockfile", False))
        self.preferences["auto_start_on_lockfile"] = bool(enabled)
        try:
            lol.save_preferences(self.preferences)
        except OSError:
            self.preferences["auto_start_on_lockfile"] = previous
            self.auto_start_checkbox.blockSignals(True)
            self.auto_start_checkbox.setChecked(previous)
            self.auto_start_checkbox.blockSignals(False)
            QMessageBox.critical(
                self,
                self._t("connection_save_failed"),
                self._t("connection_save_failed"),
            )
            return
        if enabled and start_watcher() is None:
            self.preferences["auto_start_on_lockfile"] = False
            self.auto_start_checkbox.blockSignals(True)
            self.auto_start_checkbox.setChecked(False)
            self.auto_start_checkbox.blockSignals(False)
            try:
                lol.save_preferences(self.preferences)
            except OSError as error:
                QMessageBox.critical(
                    self, self._t("connection_save_failed"), str(error)
                )
            self._set_connection_status("watcher_start_failed")

    def _reset_all_data(self):
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Warning)
        confirmation.setWindowTitle(self._t("reset_all_title"))
        confirmation.setText(self._t("reset_all_confirm"))
        confirmation.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        confirmation.setDefaultButton(QMessageBox.StandardButton.No)
        confirmation.button(QMessageBox.StandardButton.Yes).setText(
            self._t("reset_all_confirm_button")
        )
        confirmation.button(QMessageBox.StandardButton.No).setText(self._t("no"))
        if confirmation.exec() != QMessageBox.StandardButton.Yes:
            return

        try:
            self.preferences = schedule_full_reset(self.preferences)
        except OSError as error:
            QMessageBox.critical(
                self,
                self._t("reset_all_failed"),
                f"{self._t('reset_all_failed')}: {error}",
            )
            return

        self.close()
        QApplication.quit()

    def _browse_lockfile(self):
        selected, _ = QFileDialog.getOpenFileName(
            self,
            self._t("select_lockfile"),
            str(Path.home()),
            "League Client lockfile (*)",
        )
        if not selected:
            return
        try:
            lol.validate_lockfile(selected)
        except (OSError, ValueError) as error:
            QMessageBox.warning(
                self, self._t("lockfile_invalid"), self._lockfile_error_text(error)
            )
            return
        self.lockfile_path_edit.setText(selected)

    def _save_connection_settings(self):
        path = self.lockfile_path_edit.text().strip()
        if path:
            try:
                lol.validate_lockfile(path)
            except (OSError, ValueError) as error:
                QMessageBox.warning(
                    self, self._t("lockfile_invalid"), self._lockfile_error_text(error)
                )
                return
        previous_path = self.preferences.get("lockfile_path", "")
        self.preferences["lockfile_path"] = path
        try:
            lol.save_preferences(self.preferences)
        except OSError as error:
            self.preferences["lockfile_path"] = previous_path
            QMessageBox.critical(
                self, self._t("connection_save_failed"), str(error)
            )
            return
        self._active_lockfile_signature = None
        self._update_current_lockfile_label(path or None)
        self._watch_lockfile()
        self._set_connection_status("connection_saved")

    def _restore_automatic_detection(self):
        previous_path = self.preferences.get("lockfile_path", "")
        self.preferences["lockfile_path"] = ""
        try:
            lol.save_preferences(self.preferences)
        except OSError as error:
            self.preferences["lockfile_path"] = previous_path
            QMessageBox.critical(
                self, self._t("connection_save_failed"), str(error)
            )
            return
        self.lockfile_path_edit.clear()
        self._active_lockfile_signature = None
        self._update_current_lockfile_label()
        self._set_connection_status("automatic_detection")
        self._watch_lockfile()

    def _test_lockfile_connection(self):
        if self._lockfile_test_running:
            return
        path = self.lockfile_path_edit.text().strip() or self.preferences.get("lockfile_path")
        if not path:
            found = self._find_active_lockfile_path()
            path = str(found) if found is not None else ""
        if not path:
            self._set_connection_status("lockfile_missing")
            return
        self._lockfile_test_running = True
        self.lockfile_test_button.setEnabled(False)
        self._set_connection_status("connection_checking")

        def test():
            try:
                port, auth_token = lol.read_lockfile_credentials(path)
            except FileNotFoundError:
                result = "lockfile_missing"
            except (OSError, ValueError, PermissionError):
                result = "lockfile_invalid"
            else:
                session = requests.Session()
                session.verify = False
                session.headers.update(
                    {
                        "Authorization": f"Basic {auth_token}",
                        "Accept": "application/json",
                    }
                )
                try:
                    response = session.get(
                        f"https://127.0.0.1:{port}/lol-summoner/v1/current-summoner",
                        timeout=3,
                    )
                    if response.status_code == 200:
                        result = "connection_test_ok"
                    elif response.status_code in (401, 403):
                        result = "lockfile_invalid"
                    elif response.status_code in (404, 503):
                        result = "client_not_ready"
                    else:
                        result = "connection_test_failed"
                except requests.ConnectionError:
                    result = "client_refused"
                except requests.Timeout:
                    result = "client_not_ready"
                except requests.RequestException:
                    result = "connection_test_failed"
                finally:
                    session.close()
            self.signals.lockfile_tested.emit(result)

        threading.Thread(target=test, daemon=True).start()

    def _lockfile_error_text(self, error):
        if isinstance(error, FileNotFoundError):
            return self._t("lockfile_missing")
        if isinstance(error, PermissionError):
            return self._t("lockfile_missing")
        return self._t("invalid_lockfile")

    def _lockfile_test_complete(self, status_key):
        self._lockfile_test_running = False
        self.lockfile_test_button.setEnabled(True)
        self._set_connection_status(status_key)

    def _find_active_lockfile_path(self):
        configured = str(self.preferences.get("lockfile_path") or "").strip()
        if configured:
            return Path(configured).expanduser()
        if self._auto_discovered_lockfile and self._auto_discovered_lockfile.is_file():
            return self._auto_discovered_lockfile
        for candidate in lol.default_lockfile_candidates():
            if candidate.is_file():
                return candidate
        return None

    def _start_lockfile_scan(self):
        if self._lockfile_scan_running:
            return
        self._lockfile_scan_running = True
        self._last_lockfile_scan = time.monotonic()

        def scan():
            try:
                path = lol.find_lockfile_path()
                result = str(path) if path is not None and path.is_file() else ""
            except OSError:
                result = ""
            self.signals.lockfile_discovered.emit(result)

        threading.Thread(target=scan, daemon=True).start()

    def _lockfile_discovered(self, path):
        self._lockfile_scan_running = False
        if path:
            self._auto_discovered_lockfile = Path(path)
            self._watch_lockfile()

    def _watch_lockfile(self):
        path = self._find_active_lockfile_path()
        if path is None or not path.is_file():
            if not str(self.preferences.get("lockfile_path") or "").strip():
                if time.monotonic() - self._last_lockfile_scan >= 15:
                    self._start_lockfile_scan()
            self._auto_discovered_lockfile = None
            self._active_lockfile_signature = None
            self._update_current_lockfile_label()
            configured = bool(str(self.preferences.get("lockfile_path") or "").strip())
            self._set_connection_status(
                "lockfile_missing" if configured else "client_not_running"
            )
            return
        try:
            port = lol.validate_lockfile(path)
            parts = path.read_text(encoding="utf-8").strip().split(":")
            signature = (str(path.resolve()), parts[1], str(port))
        except (OSError, ValueError, IndexError):
            self._set_connection_status("lockfile_invalid")
            return
        self._update_current_lockfile_label(str(path))
        if signature != self._active_lockfile_signature:
            self._active_lockfile_signature = signature
            self._set_connection_status("connection_checking")
            self.refresh_reports()

    def open_lane_assigner(self):
        if self.lane_assigner is None:
            self.lane_assigner = LaneAssignerDialog(
                self,
                self._t,
                lambda name: self.champion_icons.get(normalize_name(name), QIcon()),
                self._lane_assignments_applied,
                self._lane_catalog_updated,
            )
        self.lane_assigner.prepare_to_open()

    def open_settings(self, first_run=False):
        if hasattr(self, "settings_tab"):
            self.tabs.setCurrentWidget(self.settings_tab)
            if first_run:
                self._set_connection_status("client_not_running")

    def _lane_assignments_applied(self):
        lane_map = lol.get_champion_lane_map_with_manual_overrides()
        mastery_by_name = {}
        for rows in getattr(self, "mastery_lanes", {}).values():
            for champion, score, ownership, level, grade in rows:
                mastery_by_name.setdefault(
                    normalize_name(champion),
                    (champion, score, ownership, level, grade),
                )
        updated = {}
        for champion, score, ownership, level, grade in mastery_by_name.values():
            lanes = lane_map.get(normalize_name(champion), [])
            if not lanes:
                lanes = ["Unassigned"]
            for lane in lanes:
                updated.setdefault(lane, []).append(
                    (champion, score, ownership, level, grade)
                )
        self.mastery_lanes = updated
        self.lane_mastery_bias_counts = parse_lane_mastery_bias(updated)
        self._update_lane_mastery_bias_label()
        self._populate_mastery()
        self._populate_top_champions()
        self.refresh_reports()

    def _lane_catalog_updated(self, catalog):
        self.champion_catalog = catalog
        self._populate_collection()
        self._icon_load_started = False
        self._load_champion_icons()
        if self.data_ready:
            self.refresh_reports()

    def _show_context_menu(self, position):
        menu = QMenu(self)
        action = menu.addAction(self._t("context_assigner"))
        action.triggered.connect(self.open_lane_assigner)
        source = self.sender()
        menu.exec(source.mapToGlobal(position) if source is not None else self.mapToGlobal(position))

    @staticmethod
    def _panel(title, parent_layout=None):
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QLabel(title)
        heading.setObjectName("panelTitle")
        heading.setProperty("source_title", title)
        layout.addWidget(heading)
        if parent_layout is not None:
            parent_layout.addWidget(panel)
        return panel, layout

    @staticmethod
    def _tree(parent_layout, columns, widths=None):
        tree = QTreeWidget()
        tree.setColumnCount(len(columns))
        tree.setHeaderLabels(columns)
        tree.setAlternatingRowColors(True)
        tree.setRootIsDecorated(False)
        tree.setUniformRowHeights(True)
        tree.setIconSize(tree.iconSize())
        tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        tree.header().setStretchLastSection(True)
        if widths:
            for index, width in enumerate(widths):
                tree.setColumnWidth(index, width)
        parent_layout.addWidget(tree, 1)
        return tree

    @staticmethod
    def _metric_card(parent_layout, label, value="–"):
        card = QFrame()
        card.setObjectName("metricCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(13, 11, 13, 11)
        caption = QLabel(label)
        caption.setObjectName("muted")
        metric = QLabel(value)
        metric.setObjectName("metricValue")
        layout.addWidget(caption)
        layout.addWidget(metric)
        parent_layout.addWidget(card, 1)
        return metric

    @staticmethod
    def _icon_size(size):
        from PySide6.QtCore import QSize
        return QSize(size, size)

    def refresh_display(self):
        self.load_reports(force=True)

    def load_reports(self, *, force=False):
        if not force and not getattr(self, "data_ready", False):
            return
        account = read_report(lol.REPORT_PATH)
        tft_lines = read_report(lol.TFT_REPORT_PATH)
        mastery_lines = read_report(lol.MASTERY_REPORT_PATH)

        player_line = find_line(account, "Spieler:")
        display_name = player_line.partition(":")[2].strip() if player_line else self._t("no_report")
        self.account_label.setText(display_name)
        name_font = self.account_label.font()
        name_font.setPointSize(20)
        self.account_label.setFont(name_font)
        icon_line = find_line(account, "Profilbild-ID:")
        self._load_profile_icon(icon_line.partition(":")[2].strip() if icon_line else "")
        level_line = find_line(account, "Beschwörer-Level:")
        honor_line = find_line(account, "Ehrenstufe:")
        self.level_label.setText(
            "  ·  ".join(
                line.partition(":")[2].strip()
                for line in (level_line, honor_line)
                if line
            )
        )
        metrics_line = next(
            (
                int(match.group(1))
                for line in account
                if (match := re.search(r"--- MATCH-HISTORIE \((\d+)", line))
            ),
            0,
        )
        average_line = find_line(account, "Durchschnitt pro Spiel K/D/A:")
        average_kda = average_line.partition(":")[2].strip() if average_line else "–"
        self.games_value.setText(str(metrics_line))
        averages = average_kda.split("/")
        average_values = averages if len(averages) == 3 else ("–", "–", "–")
        self.avg_kills_value.setText(average_values[0])
        self.avg_deaths_value.setText(average_values[1])
        self.avg_assists_value.setText(average_values[2])
        self.honor_value.setText(honor_line.partition(":")[2].strip() if honor_line else "–")
        be_line = next(
            (
                line
                for line in account
                if line.startswith("Benötigte Blaue Essenzen gesamt:") or line.startswith("Bekannte Preise zusammen:")
            ),
            "",
        )
        be_value = re.search(r"([\d,]+)\s+BE", be_line)
        self.be_value.setText(be_value.group(1) if be_value else "–")

        self.missing_champion_rows = []
        for entry in parse_missing_champions(account):
            match = re.match(r"(.+?)\s+\((.+)\)$", entry)
            self.missing_champion_rows.append(match.groups() if match else (entry, ""))
        self.mastery_lanes = parse_mastery_lanes(mastery_lines)
        self.match_rows = parse_match_history(account)
        self.lane_bias_counts = parse_lane_bias(account)
        self.lane_mastery_bias_counts = parse_lane_mastery_bias(self.mastery_lanes)
        self._update_lane_bias_label()
        self._update_lane_mastery_bias_label()
        stats_count_line = find_line(
            account, "Matches mit vollständigen K/D/A-Werten:"
        )
        if stats_count_line:
            try:
                self.league_stat_match_count = int(
                    stats_count_line.partition(":")[2].strip()
                )
            except ValueError:
                self.league_stat_match_count = 0
        else:
            self.league_stat_match_count = sum(
                all(value != "–" for value in row[6:9]) for row in self.match_rows
            )
        self.champion_catalog = lol.get_champion_catalog(fetch_remote=False)
        self.collection_ownership = {}
        try:
            with lol.CHAMPION_COLLECTION_PATH.open("r", encoding="utf-8") as handle:
                collection_data = json.load(handle)
            entries = collection_data.get("champions", []) if isinstance(collection_data, dict) else []
            if isinstance(entries, list):
                self.collection_ownership = {
                    str(entry.get("id")): entry["owned"]
                    for entry in entries
                    if isinstance(entry, dict)
                    and entry.get("id") is not None
                    and isinstance(entry.get("owned"), bool)
                }
        except (OSError, ValueError):
            self.collection_ownership = {}
        tft_matches, self.tft_average_placement = parse_tft_history(tft_lines)
        self.tft_matches = tft_matches

        self._populate_top_champions()
        self._populate_missing_champions()
        self._populate_collection()
        self._populate_mastery()
        self._populate_matches()
        self._populate_tft()
        self._load_champion_icons()
        self.match_summary.setText(
            self._t("match_stats_summary").format(
                matches=len(self.match_rows), stats=self.league_stat_match_count
            )
        )
        self._refresh_status_key = (
            "reports_loaded" if account or tft_lines or mastery_lines else "no_reports"
        )
        self.status_label.setText(self._t(self._refresh_status_key))

    def _clear_tree(self, tree):
        tree.clear()

    def _populate_top_champions(self):
        if not hasattr(self, "top_tree"):
            return
        self.top_tree.clear()
        by_champion = {}
        for champions in getattr(self, "mastery_lanes", {}).values():
            for champion, score, _ownership, level, grade in champions:
                if int(score) <= 0 and str(level) in ("", "0"):
                    continue
                key = normalize_name(champion)
                existing = by_champion.get(key)
                if existing is None or int(score) > existing[1]:
                    by_champion[key] = (champion, int(score), level, grade)
        for champion, score, level, grade in sorted(
            by_champion.values(), key=lambda row: (-row[1], row[0].casefold())
        )[:5]:
            row = QTreeWidgetItem(("", champion, str(level), f"{score:,}", grade))
            self._set_champion_icon(row, champion)
            self.top_tree.addTopLevelItem(row)

    def _populate_collection(self):
        if not hasattr(self, "collection_tree"):
            return
        self.collection_tree.clear()
        catalog = getattr(self, "champion_catalog", [])
        known = getattr(self, "collection_ownership", {})
        ids = [str(champion["id"]) for champion in catalog if champion.get("id") is not None]
        known_ids = [champion_id for champion_id in ids if champion_id in known]
        owned_count = sum(known[champion_id] is True for champion_id in known_ids)
        if not known_ids:
            self.collection_summary.setText(self._t("collection_unavailable"))
        elif len(known_ids) == len(ids) and ids:
            percentage = owned_count / len(ids) * 100
            self.collection_summary.setText(
                self._t("collection_count").format(
                    owned=owned_count, total=len(ids), percent=f"{percentage:.1f}"
                )
            )
        else:
            self.collection_summary.setText(
                self._t("collection_count_partial").format(owned=owned_count)
            )

        for champion in catalog:
            champion_id = champion.get("id")
            name = str(champion.get("name") or "")
            owned = known.get(str(champion_id))
            ownership = (
                self._t("owned")
                if owned is True
                else self._t("not_owned")
                if owned is False
                else self._t("unknown")
            )
            row = QTreeWidgetItem(("", name, ownership))
            self._set_champion_icon(row, name)
            self.collection_tree.addTopLevelItem(row)
        self._filter_collection(self.collection_search.text())

    def _filter_collection(self, text):
        if not hasattr(self, "collection_tree"):
            return
        query = text.strip().casefold()
        for row in range(self.collection_tree.topLevelItemCount()):
            item = self.collection_tree.topLevelItem(row)
            item.setHidden(bool(query and query not in item.text(1).casefold()))

    def _populate_missing_champions(self):
        if not hasattr(self, "missing_tree"):
            return
        self.missing_tree.clear()
        rows = getattr(self, "missing_champion_rows", [])
        if not rows:
            self.missing_tree.addTopLevelItem(
                QTreeWidgetItem(("", self._t("no_missing_champions"), ""))
            )
            return
        for champion, price in rows:
            row = QTreeWidgetItem(("", champion, price))
            self._set_champion_icon(row, champion)
            self.missing_tree.addTopLevelItem(row)

    def _populate_mastery(self, *_args):
        if not hasattr(self, "mastery_tree"):
            return
        self.mastery_tree.clear()
        selected_lane = self.lane_filter.currentData()
        search_text = self.mastery_search.text().strip().casefold()
        champions_by_name = {}
        for lane, champions in getattr(self, "mastery_lanes", {}).items():
            if selected_lane is not None and selected_lane != lane:
                continue
            for champion, score, ownership, rank, grade in champions:
                key = normalize_name(champion)
                entry = champions_by_name.setdefault(
                    key,
                    {
                        "name": champion,
                        "lanes": [],
                        "score": score,
                        "ownership": ownership,
                        "rank": rank,
                        "grade": grade,
                    },
                )
                entry["lanes"].append(lane)

        rows = []
        for entry in champions_by_name.values():
            if search_text and search_text not in entry["name"].casefold():
                continue
            lanes = " / ".join(self._t(lane.casefold()) for lane in entry["lanes"])
            rows.append(
                (
                    entry["name"],
                    lanes,
                    entry["score"],
                    entry["ownership"],
                    entry["rank"],
                    entry["grade"],
                )
            )

        sort_column = self.sort_column.currentData() if hasattr(self, "sort_column") else "mastery_score"
        descending = self.mastery_sort_descending
        if sort_column == "top_value":
            rows.sort(
                key=lambda row: (
                    *mastery_grade_sort_key(row[5], best_first=descending),
                    row[0].casefold(),
                )
            )
        elif sort_column == "mastery_rank":
            rows.sort(
                key=lambda row: (int(row[4]) if str(row[4]).isdigit() else -1, row[0].casefold()),
                reverse=descending,
            )
        elif sort_column == "champion_name":
            rows.sort(key=lambda row: row[0].casefold(), reverse=descending)
        else:
            rows.sort(key=lambda row: (row[2], row[0].casefold()), reverse=descending)

        for champion, lanes, score, ownership, rank, grade in rows:
            row = QTreeWidgetItem(
                (
                    "",
                    champion,
                    lanes,
                    f"{score:,}",
                    self._translate_ownership(ownership),
                    str(rank),
                    grade,
                )
            )
            self._set_champion_icon(row, champion)
            self.mastery_tree.addTopLevelItem(row)
        count_label = self._t("champions") if selected_lane is None else self._t("entries")
        self.mastery_count.setText(f"{len(rows)} {count_label}")
        self._update_sort_headers()

    def _populate_matches(self):
        if not hasattr(self, "match_tree"):
            return
        self.match_tree.clear()
        for date, match_id, mode, result, champion, duration, kills, deaths, assists, lane in getattr(self, "match_rows", []):
            row = QTreeWidgetItem(
                (
                    "",
                    date,
                    match_id,
                    mode,
                    self._translate_match_result(result),
                    champion,
                    self._t("lane_not_available") if lane == "–" else lane,
                    duration,
                    f"{kills}/{deaths}/{assists}",
                )
            )
            self._set_champion_icon(row, champion)
            if result == "SIEG":
                row.setForeground(4, Qt.GlobalColor.green)
            elif result == "NIEDERLAGE":
                row.setForeground(4, Qt.GlobalColor.red)
            self.match_tree.addTopLevelItem(row)

    def _populate_tft(self):
        if not hasattr(self, "tft_tree"):
            return
        self.tft_tree.clear()
        for date, match_id, mode, placement in getattr(self, "tft_matches", []):
            row = QTreeWidgetItem(
                (
                    date,
                    match_id,
                    mode,
                    self._t("placement_out_of_8").format(placement=placement),
                )
            )
            row.setForeground(
                3, Qt.GlobalColor.green if int(placement) <= 4 else Qt.GlobalColor.red
            )
            self.tft_tree.addTopLevelItem(row)
        placements = []
        for _date, _match_id, _mode, placement in getattr(self, "tft_matches", []):
            try:
                value = int(placement)
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 8:
                placements.append(value)
        count = len(placements)
        self.tft_count.setText(f"{count} {self._t('games_suffix')}")
        average = sum(placements) / count if count else None
        self.tft_average.setText(
            f"{self._t('average_placement')}: {average:.2f}/8"
            if average is not None
            else f"{self._t('average_placement')}: –"
        )
        self.tft_top_four.setText(
            f"{self._t('tft_top_four')}: {sum(value <= 4 for value in placements) / count * 100:.1f}%"
            if count
            else f"{self._t('tft_top_four')}: –"
        )
        self.tft_first_place.setText(
            f"{self._t('tft_first_place')}: {sum(value == 1 for value in placements) / count * 100:.1f}%"
            if count
            else f"{self._t('tft_first_place')}: –"
        )

    def _mastery_header_clicked(self, index):
        columns = {
            1: "champion_name",
            3: "mastery_score",
            5: "mastery_rank",
            6: "top_value",
        }
        sort_key = columns.get(index)
        if not sort_key:
            return
        if self.sort_column.currentData() == sort_key:
            self.mastery_sort_descending = not self.mastery_sort_descending
        else:
            self.sort_column.setCurrentIndex(self.sort_column.findData(sort_key))
            self.mastery_sort_descending = True
        self._populate_mastery()

    def _on_sort_column_changed(self, _text):
        self.mastery_sort_descending = True
        self._populate_mastery()

    def _set_mastery_sort(self, descending):
        self.mastery_sort_descending = descending
        self._populate_mastery()

    def _update_sort_headers(self):
        labels = {
            "mastery_score": (3, self._t("mastery_score")),
            "mastery_rank": (5, self._t("mastery_rank")),
            "top_value": (6, self._t("top_value")),
        }
        for key, (column, title) in labels.items():
            if key == self.sort_column.currentData():
                title += " ↓" if self.mastery_sort_descending else " ↑"
            self.mastery_tree.headerItem().setText(column, title)

    def _set_champion_icon(self, item, champion):
        icon = self.champion_icons.get(normalize_name(champion))
        if icon:
            item.setIcon(0, icon)

    def _load_champion_icons(self):
        if self._icon_load_started or (
            not getattr(self, "mastery_lanes", None)
            and not getattr(self, "champion_catalog", None)
        ):
            return
        self._icon_load_started = True
        names = {
            champion
            for rows in getattr(self, "mastery_lanes", {}).values()
            for champion, *_rest in rows
        }
        names.update(
            champion.get("name", "")
            for champion in getattr(self, "champion_catalog", [])
            if champion.get("name")
        )

        def load_icons():
            try:
                response = requests.get(lol.CHAMPION_SUMMARY_URL, timeout=12)
                response.raise_for_status()
                summary = response.json()
            except (requests.RequestException, ValueError):
                return
            by_name = champion_icon_urls(summary)
            try:
                self.icon_cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                return

            def load_icon(name):
                url = by_name.get(normalize_name(name))
                if not url:
                    return None
                path = self.icon_cache_dir / f"{normalize_name(name)}.png"
                try:
                    if path.is_file():
                        data = path.read_bytes()
                    else:
                        image_response = requests.get(url, timeout=12)
                        image_response.raise_for_status()
                        data = image_response.content
                        path.write_bytes(data)
                except (OSError, requests.RequestException):
                    return None
                return name, data

            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    image_data = dict(
                        result
                        for result in executor.map(load_icon, names)
                        if result is not None
                    )
            except RuntimeError:
                if self._shutdown.is_set():
                    return
                raise
            if self._shutdown.is_set():
                return
            self.signals.champion_icons.emit(image_data)

        threading.Thread(target=load_icons, daemon=True).start()

    def _apply_champion_icons(self, image_data):
        for name, data in image_data.items():
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self.champion_icons[normalize_name(name)] = QIcon(pixmap)
        self._populate_mastery()
        self._populate_top_champions()
        self._populate_missing_champions()
        self._populate_collection()
        self._populate_matches()
        if self.lane_assigner is not None:
            self.lane_assigner.refresh_icons()

    def _load_profile_icon(self, icon_id):
        if not icon_id.isdigit() or icon_id == "0" or icon_id == self._profile_icon_id:
            return
        self._profile_icon_id = icon_id

        def load_icon():
            path = self.icon_cache_dir / f"profile-{icon_id}.png"
            try:
                if path.is_file():
                    data = path.read_bytes()
                else:
                    versions_response = requests.get(
                        "https://ddragon.leagueoflegends.com/api/versions.json",
                        timeout=12,
                    )
                    versions_response.raise_for_status()
                    versions = versions_response.json()
                    if not versions:
                        return
                    url = (
                        f"https://ddragon.leagueoflegends.com/cdn/{versions[0]}/"
                        f"img/profileicon/{icon_id}.png"
                    )
                    response = requests.get(url, timeout=12)
                    response.raise_for_status()
                    data = response.content
                    self.icon_cache_dir.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            except (OSError, ValueError, requests.RequestException):
                self.signals.profile_icon_error.emit(icon_id)
                return
            if not self._shutdown.is_set():
                self.signals.profile_icon.emit(data)

        threading.Thread(target=load_icon, daemon=True).start()

    def _apply_profile_icon(self, data):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        name_width = QFontMetrics(self.account_label.font()).horizontalAdvance(self.account_label.text())
        icon_size = max(72, min(112, 64 + name_width // 15))
        scaled = pixmap.scaled(icon_size, icon_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.profile_icon_label.setFixedSize(icon_size, icon_size)
        self.profile_icon_label.setPixmap(scaled)
        self.profile_icon_label.setText("")

    def _profile_icon_failed(self, icon_id):
        if self._profile_icon_id == icon_id:
            self._profile_icon_id = None

    def refresh_reports(self):
        if self._refresh_running:
            return
        self._refresh_running = True
        self.refresh_button.setEnabled(False)
        self._refresh_status_key = "status_update"
        self.status_label.setText(self._t(self._refresh_status_key))

        def worker():
            try:
                lol.fetch_player_data()
            except FileNotFoundError:
                if not self._shutdown.is_set():
                    self.signals.refreshed.emit(False, "lockfile_missing")
            except ValueError:
                if not self._shutdown.is_set():
                    self.signals.refreshed.emit(False, "lockfile_invalid")
            except requests.ConnectionError:
                if not self._shutdown.is_set():
                    self.signals.refreshed.emit(False, "client_refused")
            except requests.Timeout:
                if not self._shutdown.is_set():
                    self.signals.refreshed.emit(False, "client_not_ready")
            except Exception:
                if not self._shutdown.is_set():
                    self.signals.refreshed.emit(False, "connection_test_failed")
            else:
                if not self._shutdown.is_set():
                    self.signals.refreshed.emit(True, "Reports updated")

        threading.Thread(target=worker, daemon=True).start()

    def closeEvent(self, event):
        self._shutdown.set()
        self.refresh_timer.stop()
        self.lockfile_timer.stop()
        self.auto_accept_timer.stop()
        self.loldle_view.stop()
        event.accept()

    def _refresh_complete(self, success, message):
        self._refresh_running = False
        self.refresh_button.setEnabled(True)
        if success:
            self.data_ready = True
            self._retry_delay_seconds = 3
            self.load_reports()
            self._refresh_status_key = "client_connected"
            self.status_label.setText(self._t(self._refresh_status_key))
            self._set_connection_status("client_connected")
            return
        status_key = message if message in {
            "lockfile_missing",
            "lockfile_invalid",
            "client_refused",
            "client_not_ready",
        } else "connection_test_failed"
        self._refresh_status_key = status_key
        self.status_label.setText(self._t(self._refresh_status_key))
        self._set_connection_status(status_key)
        if status_key not in ("lockfile_missing", "lockfile_invalid") and self._active_lockfile_signature:
            delay = self._retry_delay_seconds
            self._retry_delay_seconds = min(self._retry_delay_seconds * 2, 30)
            QTimer.singleShot(delay * 1000, self.refresh_reports)


def main():
    if "--lockfile-watcher" in sys.argv:
        from PySide6.QtCore import QCoreApplication
        from lockfile_watcher import run_watcher
        application = QCoreApplication(sys.argv)
        return run_watcher()

    app = QApplication(sys.argv)
    app.setApplicationName("Riftboard")

    def activate_existing_instance():
        existing = QLocalSocket()
        existing.connectToServer(INSTANCE_SERVER_NAME)
        if not existing.waitForConnected(300):
            return False
        existing.write(b"activate")
        existing.flush()
        existing.disconnectFromServer()
        return True

    if activate_existing_instance():
        return 0

    server = QLocalServer()
    if not server.listen(INSTANCE_SERVER_NAME):
        if activate_existing_instance():
            return 0
        QLocalServer.removeServer(INSTANCE_SERVER_NAME)
        if not server.listen(INSTANCE_SERVER_NAME):
            if activate_existing_instance():
                return 0
            return 1

    try:
        complete_pending_reset()
    except OSError as error:
        QMessageBox.critical(
            None,
            "Riftboard reset failed",
            f"Local application data could not be fully cleared: {error}",
        )
        server.close()
        QLocalServer.removeServer(INSTANCE_SERVER_NAME)
        return 1

    dashboard = Dashboard()

    def activate_existing_window():
        while server.hasPendingConnections():
            client = server.nextPendingConnection()
            if client is None:
                continue
            client.waitForReadyRead(250)
            client.readAll()
            dashboard.showNormal()
            dashboard.raise_()
            dashboard.activateWindow()
            client.disconnectFromServer()

    server.newConnection.connect(activate_existing_window)
    if server.hasPendingConnections():
        activate_existing_window()
    dashboard.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
