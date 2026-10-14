import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import lol
from lane_assigner import LaneAssignerDialog
from lockfile_watcher import _find_path, run_watcher
from main import parse_match_history, parse_tft_history
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
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
                "2026-01-01 12:00 | Match 1 | Ranked Solo/Duo | SIEG | Aatrox | Dauer 30:42 | KDA 5/3/9",
                "2026-01-02 12:00 | Match 2 | ARAM | NIEDERLAGE | Ahri | KDA 2/8/4",
            ]
        )
        self.assertEqual(matches[0][5], "30:42")
        self.assertEqual(matches[1][5], "–")

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
            rows, _counts, kills, deaths, assists, complete = lol.fetch_matches(
                object(), "https://local", "player-puuid", 7, 20, {266: "Aatrox"}
            )
        self.assertEqual(complete, 1)
        self.assertEqual((kills, deaths, assists), (4, 2, 8))
        self.assertIn("Dauer 2:00", rows[0])
        self.assertIn("KDA –/–/–", rows[1])

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


if __name__ == "__main__":
    unittest.main()
