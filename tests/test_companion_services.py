import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import lol
from lane_assigner import LaneAssignerDialog
from lockfile_watcher import _find_path, run_watcher
from main import (
    champion_icon_urls,
    complete_pending_reset,
    create_loldle_profile,
    mastery_grade_sort_key,
    parse_lane_bias,
    parse_lane_mastery_bias,
    parse_match_history,
    parse_tft_history,
    reset_marker_path,
    schedule_full_reset,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEngineProfile
from PySide6.QtWidgets import QApplication, QWidget


class CompanionServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_preferences_round_trip_preserves_existing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(lol, "STORAGE_ROOT", root),
                patch.object(lol, "PREFERENCES_PATH", root / "preferences.json"),
            ):
                lol.save_preferences(
                    {"language": "de", "custom_value": "preserved", "lockfile_path": ""}
                )
                loaded = lol.load_preferences()
                self.assertEqual(loaded["language"], "de")
                self.assertEqual(loaded["custom_value"], "preserved")
                self.assertFalse(loaded["auto_start_on_lockfile"])

    def test_loldle_profile_persists_site_storage_and_disk_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = QWidget()
            profile_factory = Mock()
            profile_factory.HttpCacheType = QWebEngineProfile.HttpCacheType
            profile_factory.PersistentCookiesPolicy = QWebEngineProfile.PersistentCookiesPolicy
            with (
                patch.object(lol, "STORAGE_ROOT", root),
                patch("main.QWebEngineProfile", profile_factory),
            ):
                profile = create_loldle_profile(parent)
                profile_factory.assert_called_once_with("Riftboard-LoLdle", parent)
                profile.setCachePath.assert_called_once_with(
                    str(root / "loldle-browser" / "cache")
                )
                profile.setPersistentStoragePath.assert_called_once_with(
                    str(root / "loldle-browser" / "storage")
                )
                profile.setHttpCacheType.assert_called_once_with(
                    QWebEngineProfile.HttpCacheType.DiskHttpCache
                )
                profile.setPersistentCookiesPolicy.assert_called_once_with(
                    QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
                )

    def test_full_reset_clears_owned_data_and_restores_first_run_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            storage = parent / "Riftdata"
            icon_cache = parent / "Local" / "Riftboard" / "champion-icons"
            marker = parent / ".Riftdata-reset-pending"
            preferences_path = storage / "preferences.json"
            storage.mkdir()
            icon_cache.mkdir(parents=True)
            (storage / "lcu_combined_account_report.txt").write_text(
                "private report", encoding="utf-8"
            )
            (icon_cache / "Aatrox.png").write_bytes(b"icon")
            unrelated = parent / "unrelated.txt"
            unrelated.write_text("keep", encoding="utf-8")

            with (
                patch.object(lol, "STORAGE_ROOT", storage),
                patch.object(lol, "PREFERENCES_PATH", preferences_path),
                patch("main.app_icon_cache_dir", return_value=icon_cache),
            ):
                self.assertEqual(reset_marker_path(), marker)
                updated = schedule_full_reset(
                    {
                        "language": "de",
                        "first_run": False,
                        "auto_start_on_lockfile": True,
                    }
                )
                self.assertTrue(updated["first_run"])
                self.assertFalse(updated["auto_start_on_lockfile"])
                self.assertTrue(marker.is_file())
                self.assertTrue(complete_pending_reset())
                self.assertFalse(storage.exists())
                self.assertFalse(icon_cache.exists())
                self.assertFalse(marker.exists())
                self.assertTrue(unrelated.is_file())
                self.assertTrue(lol.load_preferences()["first_run"])

    def test_malformed_auto_start_preference_does_not_enable_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "preferences.json"
            path.write_text(
                json.dumps({"auto_start_on_lockfile": "false", "language": "fr"}),
                encoding="utf-8",
            )
            with patch.object(lol, "PREFERENCES_PATH", path):
                loaded = lol.load_preferences()
            self.assertFalse(loaded["auto_start_on_lockfile"])
            self.assertEqual(loaded["language"], "en")

    def test_champion_catalog_and_icons_exclude_duplicate_jade_variants(self):
        records = [
            {
                "id": 266,
                "name": "Aatrox",
                "alias": "Aatrox",
                "squarePortraitPath": "/lol-game-data/assets/v1/champion-icons/266.png",
            },
            {
                "id": 60266,
                "name": "Aatrox",
                "alias": "Jade_Aatrox",
                "squarePortraitPath": "/lol-game-data/assets/v1/champion-icons/60266.png",
            },
        ]
        catalog = lol._valid_champion_records(records)
        icon_urls = champion_icon_urls(records)
        self.assertEqual([(record["id"], record["name"]) for record in catalog], [(266, "Aatrox")])
        self.assertEqual(len(icon_urls), 1)
        self.assertTrue(icon_urls["aatrox"].endswith("/v1/champion-icons/266.png"))

    def test_standard_refresh_prefers_data_dragon_and_updates_catalog_cache(self):
        records = [{"id": 266, "name": "Aatrox", "key": "266"}]
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "champion-catalog.json"
            with (
                patch.object(lol, "CHAMPION_CATALOG_CACHE_PATH", cache_path),
                patch.object(
                    lol,
                    "_fetch_champion_catalog_from_datadragon",
                    return_value=records,
                ) as fetch_data_dragon,
                patch.object(
                    lol,
                    "_fetch_champion_catalog_from_communitydragon",
                ) as fetch_communitydragon,
            ):
                refreshed = lol.refresh_champion_catalog(prefer_data_dragon=True)
            self.assertEqual([(item["id"], item["name"]) for item in refreshed], [(266, "Aatrox")])
            fetch_data_dragon.assert_called_once()
            fetch_communitydragon.assert_not_called()
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cached["champions"], refreshed)

    def test_mastery_grade_sort_is_best_to_worst_and_reverse(self):
        grades = ["F", "S-", "D", "S+", "E", "A", "C", "S", "B", "–"]
        best_first = sorted(grades, key=mastery_grade_sort_key)
        worst_first = sorted(
            grades,
            key=lambda grade: mastery_grade_sort_key(grade, best_first=False),
        )
        self.assertEqual(best_first, ["S+", "S", "S-", "A", "B", "C", "D", "E", "F", "–"])
        self.assertEqual(worst_first, ["F", "E", "D", "C", "B", "A", "S-", "S", "S+", "–"])

    def test_lockfile_validation_and_credentials_are_separated(self):
        secret = "test-only-password"
        with tempfile.TemporaryDirectory() as directory:
            lockfile = Path(directory) / "lockfile"
            lockfile.write_text(f"LeagueClient:1234:2999:{secret}:https", encoding="utf-8")
            self.assertEqual(lol.validate_lockfile(lockfile), 2999)
            port, token = lol.read_lockfile_credentials(lockfile)
            self.assertEqual(port, 2999)
            self.assertEqual(
                base64.b64decode(token).decode("utf-8"), f"riot:{secret}"
            )
            lockfile.write_text(f"not-a-lockfile:{secret}", encoding="utf-8")
            with self.assertRaises(ValueError) as error:
                lol.validate_lockfile(lockfile)
            self.assertNotIn(secret, str(error.exception))

    def test_custom_lockfile_does_not_silently_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory) / "missing-lockfile"
            automatic = Path(directory) / "automatic-lockfile"
            with patch.object(lol, "default_lockfile_candidates", return_value=(automatic,)):
                self.assertEqual(lol.find_lockfile_path(custom), custom)

    def test_explicit_empty_manual_assignment_overrides_standard(self):
        with tempfile.TemporaryDirectory() as directory:
            override_path = Path(directory) / "manual.json"
            with patch.object(lol, "LANE_OVERRIDES_PATH", override_path):
                lol.save_lane_manual_overrides({"266": ["Jungle", "Mid"], "103": []})
                standard, effective = lol.get_lane_assignments(
                    [
                        {"id": 266, "name": "Aatrox"},
                        {"id": 103, "name": "Ahri"},
                    ]
                )
            self.assertEqual(standard[266], ["Top"])
            self.assertEqual(effective[266], ["Jungle", "Mid"])
            self.assertEqual(standard[103], ["Mid"])
            self.assertEqual(effective[103], [])
            saved = json.loads(override_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["overrides"]["103"], [])

    def test_tft_history_averages_only_valid_placements(self):
        matches, average = parse_tft_history(
            [
                "2026-01-01 12:00 | Match 1 | Mode TFT | Platz 1/8",
                "2026-01-02 12:00 | Match 2 | Platz 8/8",
                "2026-01-03 12:00 | Match 3 | Platz 0/8",
                "2026-01-04 12:00 | Match 4 | Platz 9/8",
                "2026-01-05 12:00 | Match 5 | Platz nope/8",
            ]
        )
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0][2], "TFT")
        self.assertEqual(average, "4.50/8")

    def test_match_history_supports_optional_duration(self):
        matches = parse_match_history(
            [
                "--- MATCH-HISTORIE (2 auswertbare Spiele) ---",
                "2026-01-01 12:00 | Match 1 | Ranked Solo/Duo | SIEG | Aatrox | Dauer 30:42 | KDA 5/3/9 | Lane Mid",
                "2026-01-02 12:00 | Match 2 | ARAM | NIEDERLAGE | Ahri | KDA 2/8/4 | Lane nicht verfügbar",
            ]
        )
        self.assertEqual(matches[0][5], "30:42")
        self.assertEqual(matches[0][9], "Mid")
        self.assertEqual(matches[1][5], "–")
        self.assertEqual(matches[1][9], "–")

    def test_match_report_rows_include_recorded_lane(self):
        game = {
            "gameId": 7,
            "queueId": 420,
            "gameMode": "CLASSIC",
            "gameCreation": 1_700_000_000_000,
            "participantIdentities": [
                {"participantId": 1, "player": {"puuid": "player-puuid"}}
            ],
            "participants": [
                {
                    "participantId": 1,
                    "championId": 266,
                    "individualPosition": "TOP",
                    "stats": {"kills": 1, "deaths": 2, "assists": 3, "win": True},
                }
            ],
        }
        with patch.object(lol, "request_json", return_value={"games": {"games": [game]}}):
            rows, *_ = lol.fetch_matches(
                object(), "https://local", "player-puuid", 7, 20, {266: "Aatrox"}
            )
        self.assertTrue(rows[0].endswith("| Lane Top"), rows[0])

    def test_non_lane_modes_report_lane_unavailable(self):
        game = {
            "gameId": 8,
            "queueId": 450,
            "gameMode": "ARAM",
            "participantIdentities": [
                {"participantId": 1, "player": {"puuid": "player-puuid"}}
            ],
            "participants": [
                {
                    "participantId": 1,
                    "championId": 103,
                    "individualPosition": "MIDDLE",
                    "stats": {},
                }
            ],
        }
        with patch.object(lol, "request_json", return_value={"games": {"games": [game]}}):
            rows, *_ = lol.fetch_matches(
                object(), "https://local", "player-puuid", 7, 20, {103: "Ahri"}
            )
        self.assertTrue(rows[0].endswith("| Lane nicht verfügbar"), rows[0])

    def test_match_duration_formatter_rejects_invalid_values(self):
        self.assertEqual(lol.format_duration(181), "3:01")
        self.assertIsNone(lol.format_duration(-1))
        self.assertIsNone(lol.format_duration("unknown"))

    def test_league_averages_exclude_incomplete_kda_records(self):
        def game(game_id, stats):
            return {
                "gameId": game_id,
                "queueId": 420,
                "gameCreation": 1_700_000_000_000,
                "gameDuration": 120,
                "participantIdentities": [
                    {
                        "participantId": 1,
                        "player": {"puuid": "player-puuid", "summonerId": 7},
                    }
                ],
                "participants": [
                    {"participantId": 1, "championId": 266, "stats": stats}
                ],
            }

        payload = {
            "games": {
                "games": [
                    game(1, {"kills": 4, "deaths": 2, "assists": 8, "win": True}),
                    game(2, {"kills": 9, "assists": 3, "win": False}),
                ]
            }
        }
        with patch.object(lol, "request_json", return_value=payload):
            rows, _counts, lane_counts, kills, deaths, assists, complete = lol.fetch_matches(
                object(), "https://local", "player-puuid", 7, 20, {266: "Aatrox"}
            )
        self.assertEqual(complete, 1)
        self.assertEqual(lane_counts, {})
        self.assertEqual((kills, deaths, assists), (4, 2, 8))
        self.assertIn("Dauer 2:00", rows[0])
        self.assertIn("KDA –/–/–", rows[1])

    def test_lane_bias_normalizes_positions_and_excludes_special_modes(self):
        self.assertEqual(
            lol.normalize_match_lane(
               {"timeline": {"lane": "BOTTOM", "role": "DUO_SUPPORT"}}
            ),
            "Support",
        )
        self.assertEqual(lol.normalize_match_lane({"individualPosition": "MIDDLE"}), "Mid")
        self.assertEqual(
            lol.normalize_match_lane(
                {"individualPosition": "MIDDLE", "timeline": {"role": "DUO_SUPPORT"}}
            ),
            "Mid",
        )
        self.assertEqual(
            lol.normalize_match_lane(
                {"teamPosition": "TOP", "timeline": {"role": "DUO_SUPPORT"}}
            ),
            "Top",
        )
        for game in (
            {"queueId": 450, "gameMode": "ARAM"},
            {"queueId": 2400, "gameMode": "CHERRY"},
            {"queueId": 1700, "gameMode": "CLASSIC"},
            {"queueId": 420, "gameMode": "ARAM_MAYHEM"},
            {"queueId": 420, "gameMode": "ARENA"},
            {"queueId": 900, "gameMode": "URF"},
            {"queueId": 999, "gameMode": "CLASSIC"},
            {"queueId": 420, "gameMode": "URF"},
        ):
            self.assertFalse(lol.is_lane_bias_game(game), game)
        self.assertTrue(lol.is_lane_bias_game({"queueId": 420, "gameMode": "CLASSIC"}))
        self.assertTrue(lol.is_lane_bias_game({"queueId": 490, "gameMode": "CLASSIC"}))
        self.assertTrue(lol.is_lane_bias_game({"queueId": 700, "gameMode": "CLASSIC"}))

    def test_lane_bias_counts_only_standard_lane_queues(self):
        def game(game_id, queue_id, game_mode, lane):
            return {
                "gameId": game_id,
                "queueId": queue_id,
                "gameMode": game_mode,
                "participantIdentities": [
                    {"participantId": 1, "player": {"puuid": "player-puuid"}}
                ],
                "participants": [
                    {
                        "participantId": 1,
                        "individualPosition": lane,
                        "stats": {},
                    }
                ],
            }

        payload = {
            "games": {
                "games": [
                    game(1, 420, "CLASSIC", "TOP"),
                    game(2, 450, "ARAM", "TOP"),
                    game(3, 1700, "CLASSIC", "MIDDLE"),
                    game(4, 420, "ARAM_MAYHEM", "BOTTOM"),
                    game(5, 420, "CHERRY_ARENA", "JUNGLE"),
                ]
            }
        }
        with patch.object(lol, "request_json", return_value=payload):
            _rows, _champions, lane_counts, *_stats = lol.fetch_matches(
                object(), "https://local", "player-puuid", 7, 20, {}
            )
        self.assertEqual(lane_counts, {"Top": 1})

    def test_lane_bias_report_parsing(self):
        self.assertEqual(
            parse_lane_bias(
                [
                    "LANE_BIAS:Top=4;Mid=4;Bot=2;Support=0;NotALane=9;Jungle=bad",
                ]
            ),
            {"Top": 4, "Mid": 4, "Bot": 2},
        )

    def test_lane_mastery_bias_totals_points_per_assigned_lane(self):
        mastery = {
            "Top": [
                ("Aatrox", 100, "Owned", "7", "S"),
                ("Aatrox", 100, "Owned", "7", "S"),
                ("Camille", 50, "Owned", "5", "-"),
            ],
            "Mid": [("Aatrox", 100, "Owned", "7", "S")],
            "Jungle": [("Lee Sin", 500, "Owned", "7", "S+")],
            "Unassigned": [("Ahri", 9999, "Owned", "7", "S+")],
        }
        self.assertEqual(
            parse_lane_mastery_bias(mastery),
            {"Top": 150, "Jungle": 500, "Mid": 100},
        )

    def test_watcher_honors_custom_path(self):
        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory) / "custom-lockfile"
            automatic = Path(directory) / "automatic-lockfile"
            with patch.object(lol, "default_lockfile_candidates", return_value=(automatic,)):
                found, next_scan = _find_path(str(custom), 1000)
            self.assertEqual(found, custom)
            self.assertEqual(next_scan, 1000)

    def test_disabled_watcher_exits_without_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(lol, "APP_DIR", Path(directory)),
                patch.object(lol, "load_preferences", return_value={"auto_start_on_lockfile": False}),
                patch("lockfile_watcher._launch_companion") as launch,
            ):
                self.assertEqual(run_watcher(), 0)
                launch.assert_not_called()

    def test_assigner_footer_order_and_apply_persists_changes(self):
        catalog = [{"id": 266, "name": "Aatrox"}]
        parent = QWidget()
        saved = []
        applied = []
        catalog_updates = []
        with (
            patch.object(lol, "get_champion_catalog", return_value=catalog),
            patch.object(lol, "load_lane_manual_overrides", return_value={}),
            patch.object(
                lol,
                "get_lane_assignments",
                return_value=({266: ["Top"]}, {266: ["Top"]}),
            ),
            patch.object(lol, "save_lane_manual_overrides", side_effect=saved.append),
        ):
            window = LaneAssignerDialog(
                parent,
                lambda key: key,
                lambda _name: QIcon(),
                lambda: applied.append(True),
                catalog_updates.append,
            )
            window._catalog_loaded(catalog + [{"id": 103, "name": "Ahri"}])
            self.assertEqual(window.table.rowCount(), 2)
            self.assertEqual(catalog_updates[0][-1]["name"], "Ahri")
            self.assertEqual(
                [window.action_bar.itemAt(i).widget().text() for i in range(4)],
                ["cancel", "close", "apply", "reset_standard"],
            )
            window.table.item(0, 3).setCheckState(
                Qt.CheckState.Checked
            )
            self.assertTrue(window.apply_button.isEnabled())
            self.assertTrue(window.apply_changes())
            self.assertEqual(saved, [{"266": ["Top", "Jungle"]}])
            self.assertEqual(applied, [True])
            self.assertFalse(window.apply_button.isEnabled())
            window.hide()
            window.deleteLater()
        parent.deleteLater()

    def test_assigner_reset_stages_refreshed_standard_without_saving_early(self):
        catalog = [{"id": 266, "name": "Aatrox"}]
        parent = QWidget()
        saved = []
        with (
            patch.object(lol, "get_champion_catalog", return_value=catalog),
            patch.object(
                lol,
                "load_lane_manual_overrides",
                return_value={"266": ["Jungle"]},
            ),
            patch.object(
                lol,
                "get_lane_assignments",
                return_value=({266: ["Top"]}, {266: ["Jungle"]}),
            ),
            patch.object(lol, "save_lane_manual_overrides", side_effect=saved.append),
        ):
            window = LaneAssignerDialog(
                parent,
                lambda key: key,
                lambda _name: QIcon(),
                lambda: None,
            )
            window._set_reset_refreshing(True)
            window._standard_catalog_loaded(catalog)
            self.assertEqual(window._working["266"], ["Top"])
            self.assertEqual(window.table.item(0, 2).checkState(), Qt.CheckState.Checked)
            self.assertEqual(window.table.item(0, 3).checkState(), Qt.CheckState.Unchecked)
            self.assertTrue(window.apply_button.isEnabled())
            self.assertEqual(saved, [])
            self.assertTrue(window.apply_changes())
            self.assertEqual(saved, [{}])
            window.hide()
            window.deleteLater()
        parent.deleteLater()


if __name__ == "__main__":
    unittest.main()
