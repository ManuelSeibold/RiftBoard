import base64
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

QUEUE_NAMES = {
    400: "Normal Draft",
    420: "Ranked Solo/Duo",
    430: "Normal Blind",
    440: "Ranked Flex",
    450: "ARAM",
    480: "Quickplay",
    490: "Normal Draft",
    700: "Clash",
    900: "URF",
    1700: "Arena",
    2400: "Arena",
}
CHAMPION_SUMMARY_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/default/v1/champion-summary.json"
)

LANE_CHAMPIONS = {
    "Top": [
        "Aatrox", "Camille", "Darius", "Fiora", "Garen", "Gnar", "Illaoi",
        "Jax", "Kennen", "Mordekaiser", "Nasus", "Olaf", "Ornn", "Riven",
        "Sett", "Shen", "Singed", "Tahm Kench", "Teemo", "Trundle", "Tryndamere",
        "Urgot", "Vladimir", "Yone", "Yorick", "Rumble",
    ],
    "Jungle": [
        "Amumu", "Bel'Veth", "Ekko", "Elise", "Evelynn", "Gragas", "Graves",
        "Ivern", "Jarvan IV", "Karthus", "Kha'Zix", "Kindred", "Lee Sin",
        "Lillia", "Maokai", "Nautilus", "Nidalee", "Nocturne", "Poppy", "Rammus",
        "Rek'Sai", "Sejuani", "Shyvana", "Skarner", "Trundle", "Vi", "Viego",
        "Warwick", "Wukong", "Xin Zhao", "Zac",
    ],
    "Mid": [
        "Ahri", "Aurelion Sol", "Akali", "Anivia", "Annie", "Azir", "Brand",
        "Cassiopeia", "Cho'Gath", "Corki", "Diana", "Fizz", "Galio", "Heimerdinger",
        "Kassadin", "Katarina", "LeBlanc", "Lissandra", "Lux", "Malphite", "Morgana",
        "Orianna", "Pantheon", "Qiyana", "Ryze", "Sylas", "Twisted Fate", "Veigar",
        "Vel'Koz", "Vex", "Viktor", "Xerath", "Yasuo", "Zed", "Zoe",
    ],
    "Bot": [
        "Aphelios", "Ashe", "Caitlyn", "Draven", "Ezreal", "Jhin", "Jinx",
        "Kai'Sa", "Kalista", "Kog'Maw", "Lucian", "Miss Fortune", "Samira",
        "Senna", "Sivir", "Swain", "Talon", "Tristana", "Twitch", "Varus",
        "Vayne", "Xayah",
    ],
    "Support": [
        "Alistar", "Bard", "Blitzcrank", "Braum", "Fiddlesticks", "Janna",
        "Leona", "Lulu", "Nami", "Nautilus", "Pyke", "Rakan", "Rell",
        "Renata Glasc", "Seraphine", "Sona", "Soraka", "Thresh", "Yuumi",
        "Zilean", "Zyra",
    ],
    "Other": [],
}

AUTO_ACCEPT_MATCH = False


def set_auto_accept_match(enabled):
    global AUTO_ACCEPT_MATCH
    AUTO_ACCEPT_MATCH = bool(enabled)
    return AUTO_ACCEPT_MATCH


def auto_accept_match():
    if not AUTO_ACCEPT_MATCH:
        return False

    try:
        port, auth_token = get_lcu_credentials()
    except Exception:
        return False

    base_url = f"https://127.0.0.1:{port}"
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Basic {auth_token}",
        "Accept": "application/json",
    })
    session.verify = False

    for endpoint in (
        f"{base_url}/lol-matchmaking/v1/ready-check",
        f"{base_url}/lol-lobby/v2/lobby/matchmaking/ready-check",
    ):
        try:
            payload = request_json(session, endpoint)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("state") not in ("InProgress", "Ready"):
            continue
        for accept_endpoint in (
            f"{base_url}/lol-matchmaking/v1/ready-check/accept",
            f"{base_url}/lol-lobby/v2/lobby/matchmaking/ready-check/accept",
        ):
            try:
                response = session.post(accept_endpoint, timeout=8)
                if response.status_code in (200, 204):
                    return True
            except Exception:
                continue
    return False


def app_storage_root():
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        root = base / "Riftdata"
    elif getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path.home() / ".local" / "share"
        root = base / "Riftdata"
    else:
        root = Path(__file__).resolve().parent / "Riftdata"
    root.mkdir(parents=True, exist_ok=True)
    return root


STORAGE_ROOT = app_storage_root()
CACHE_DIR = STORAGE_ROOT / "cache"
COOKIES_DIR = STORAGE_ROOT / "cookies"
PREFERENCES_PATH = STORAGE_ROOT / "preferences.json"
CHAMPION_CATALOG_CACHE_PATH = CACHE_DIR / "champion-catalog.json"
APP_DIR = STORAGE_ROOT
LANE_OVERRIDES_PATH = APP_DIR / "champion_lanes_manual.json"
REPORT_PATH = APP_DIR / "lcu_combined_account_report.txt"
TFT_REPORT_PATH = APP_DIR / "lcu_tft_match_history.txt"
MASTERY_REPORT_PATH = APP_DIR / "lcu_champion_mastery.txt"
CHAMPION_COLLECTION_PATH = APP_DIR / "champion_collection.json"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_DIR.mkdir(parents=True, exist_ok=True)


def load_preferences():
    defaults = {
        "language": "en",
        "refresh_interval": 300,
        "lockfile_path": "",
        "first_run": True,
        "auto_start_on_lockfile": False,
    }
    if not PREFERENCES_PATH.exists():
        return defaults
    try:
        with PREFERENCES_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return defaults
    merged = defaults.copy()
    if isinstance(data, dict):
        merged.update({key: value for key, value in data.items() if value is not None})
    if merged.get("language") not in ("en", "de"):
        merged["language"] = "en"
    if not isinstance(merged.get("lockfile_path"), str):
        merged["lockfile_path"] = ""
    if not isinstance(merged.get("auto_start_on_lockfile"), bool):
        merged["auto_start_on_lockfile"] = False
    if not isinstance(merged.get("first_run"), bool):
        merged["first_run"] = True
    try:
        merged["refresh_interval"] = max(5, int(merged["refresh_interval"]))
    except (TypeError, ValueError, OverflowError):
        merged["refresh_interval"] = 300
    return merged


def save_preferences(preferences):
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = PREFERENCES_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(preferences, handle, indent=2, ensure_ascii=False)
    temporary_path.replace(PREFERENCES_PATH)


def default_lockfile_candidates():
    if sys.platform == "darwin":
        return (
            Path("/Applications/League of Legends.app/Contents/LoL/lockfile"),
            Path.home() / "Library/Application Support/Riot Games/League of Legends/lockfile",
        )

    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    program_data = Path(os.environ.get("PROGRAMDATA") or (Path("C:/ProgramData")))
    home = Path.home()

    candidates = (
        Path(r"C:\Riot Games\League of Legends\lockfile"),
        Path(r"D:\Riot Games\League of Legends\lockfile"),
        Path(r"C:\Program Files\Riot Games\League of Legends\lockfile"),
        Path(r"C:\Program Files (x86)\Riot Games\League of Legends\lockfile"),
        local_app_data / "Riot Games" / "League of Legends" / "lockfile",
        program_data / "Riot Games" / "League of Legends" / "lockfile",
        home / "AppData" / "Local" / "Riot Games" / "League of Legends" / "lockfile",
        home / "AppData" / "Roaming" / "Riot Games" / "League of Legends" / "lockfile",
    )
    seen = set()
    unique = []
    for candidate in candidates:
        normalized = str(candidate).lower()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(candidate)
    return tuple(unique)


def find_lockfile_path(configured_path=None):
    candidates = []
    if configured_path:
        return Path(configured_path).expanduser()
    candidates.extend(default_lockfile_candidates())

    seen = set()
    unique = []
    for candidate in candidates:
        normalized = str(candidate.resolve(strict=False)).lower() if candidate.is_absolute() else str(candidate).lower()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(candidate)

    for candidate in unique:
        if candidate.is_file():
            return candidate

    riot_roots = (
        Path.home() / "AppData" / "Local" / "Riot Games",
        Path.home() / "AppData" / "Roaming" / "Riot Games",
        Path("C:/ProgramData/Riot Games"),
        Path("C:/Riot Games"),
    )
    for root in riot_roots:
        if root.exists():
            for match in root.rglob("lockfile"):
                if match.is_file():
                    return match

    return unique[0] if unique else None


def get_lcu_credentials():
    """Read the local League Client port and password from its lockfile."""
    preferences = load_preferences()
    configured_path = os.environ.get("LEAGUE_LOCKFILE") or preferences.get("lockfile_path")
    lockfile_path = find_lockfile_path(configured_path)
    if lockfile_path is None or not lockfile_path.is_file():
        if configured_path:
            raise FileNotFoundError(
                f"Configured League Client lockfile does not exist: {lockfile_path}"
            )
        raise FileNotFoundError(
            "League Client lockfile not found. Start the League Client or configure "
            "the lockfile path in Settings."
        )

    return read_lockfile_credentials(lockfile_path)


def read_lockfile_credentials(path):
    fields = Path(path).expanduser().read_text(encoding="utf-8").strip().split(":")
    if len(fields) != 5 or fields[0] != "LeagueClient":
        raise ValueError("The selected file is not a valid League Client lockfile.")
    try:
        process_id = int(fields[1])
        port = int(fields[2])
    except ValueError as error:
        raise ValueError("The League Client lockfile has an invalid process or port.") from error
    if process_id <= 0 or not 1 <= port <= 65535 or not fields[3] or fields[4] != "https":
        raise ValueError("The League Client lockfile has an invalid format.")

    token = base64.b64encode(f"riot:{fields[3]}".encode("utf-8")).decode("ascii")
    return port, token


def validate_lockfile(path):
    """Return the local port when a lockfile is readable and structurally valid."""
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise FileNotFoundError("The selected lockfile does not exist.")
    try:
        with candidate.open("r", encoding="utf-8") as handle:
            fields = handle.read(4096).strip().split(":")
    except OSError as error:
        raise PermissionError("The selected lockfile cannot be read.") from error
    if len(fields) != 5 or fields[0] != "LeagueClient":
        raise ValueError("The selected file is not a valid League Client lockfile.")
    try:
        process_id = int(fields[1])
        port = int(fields[2])
    except ValueError as error:
        raise ValueError("The League Client lockfile has an invalid process or port.") from error
    if process_id <= 0 or not 1 <= port <= 65535 or not fields[3] or fields[4] != "https":
        raise ValueError("The League Client lockfile has an invalid format.")
    return port


def request_json(session, url, *, required=False):
    response = session.get(url, timeout=10)
    if response.status_code != 200:
        if required:
            raise RuntimeError(
                f"LCU-Anfrage fehlgeschlagen ({response.status_code}): {url}"
            )
        return None
    return response.json()


def get_champion_mapping():
    """Load canonical champion names from the community dataset or Data Dragon."""
    return {
        champion["id"]: champion["name"]
        for champion in get_champion_catalog()
        if champion.get("id") and champion.get("name")
    }


def _valid_champion_records(payload):
    if not isinstance(payload, (dict, list)):
        return []
    records = []
    values = payload.values() if isinstance(payload, dict) else payload
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            champion_id_value = int(item.get("id"))
        except (TypeError, ValueError):
            try:
                champion_id_value = int(item.get("key"))
            except (TypeError, ValueError):
                continue
        name = item.get("name") or item.get("displayName") or item.get("alias")
        if champion_id_value <= 0 or not isinstance(name, str) or not name.strip():
            continue
        record = {
            "id": champion_id_value,
            "name": name.strip(),
            "alias": str(item.get("alias") or item.get("id") or name).strip(),
            "squarePortraitPath": item.get("squarePortraitPath"),
        }
        records.append(record)
    return records


def _read_champion_catalog_cache():
    try:
        with CHAMPION_CATALOG_CACHE_PATH.open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return []
    records = _valid_champion_records(cached.get("champions")) if isinstance(cached, dict) else []
    return records


def get_champion_catalog(*, fetch_remote=True, force_refresh=False):
    cached = _read_champion_catalog_cache()
    if cached and not fetch_remote:
        return cached
    if cached and fetch_remote:
        try:
            cache_age = time.time() - CHAMPION_CATALOG_CACHE_PATH.stat().st_mtime
        except OSError:
            cache_age = float("inf")
        freshness = 300 if force_refresh else 3600
        if cache_age < freshness:
            return cached

    records = []
    try:
        response = requests.get(CHAMPION_SUMMARY_URL, timeout=8)
        response.raise_for_status()
        records = _valid_champion_records(response.json())
    except (requests.RequestException, ValueError):
        pass
    if not records:
        try:
            versions_response = requests.get(
                "https://ddragon.leagueoflegends.com/api/versions.json", timeout=8
            )
            versions_response.raise_for_status()
            versions = versions_response.json()
            if not isinstance(versions, list) or not versions:
                raise ValueError("Data Dragon did not return a version list.")
            response = requests.get(
                f"https://ddragon.leagueoflegends.com/cdn/{versions[0]}/data/en_US/champion.json",
                timeout=8,
            )
            response.raise_for_status()
            payload = response.json()
            records = _valid_champion_records(payload.get("data") if isinstance(payload, dict) else None)
        except (requests.RequestException, ValueError):
            pass

    if records:
        records.sort(key=lambda item: item["name"].casefold())
        try:
            CHAMPION_CATALOG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = CHAMPION_CATALOG_CACHE_PATH.with_suffix(".json.tmp")
            with temporary_path.open("w", encoding="utf-8") as handle:
                json.dump({"champions": records}, handle, ensure_ascii=False)
            temporary_path.replace(CHAMPION_CATALOG_CACHE_PATH)
        except OSError:
            pass
        return records
    return cached


def champion_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def nonnegative_int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def champion_owned(champion):
    ownership = champion.get("ownership") if isinstance(champion, dict) else None
    owned = ownership.get("owned") if isinstance(ownership, dict) else None
    return owned if isinstance(owned, bool) else None


def format_duration(seconds):
    try:
        total_seconds = int(seconds)
    except (TypeError, ValueError, OverflowError):
        return None
    if total_seconds < 0:
        return None
    minutes, remainder = divmod(total_seconds, 60)
    return f"{minutes}:{remainder:02d}"


def get_champion_name(champ_map, champ_id):
    normalized_id = champion_id(champ_id)
    return champ_map.get(normalized_id, f"Champion ID {champ_id}")


def normalize_champion_name(name):
    return "".join(character.casefold() for character in name if character.isalnum())


LANES = ("Top", "Jungle", "Mid", "Bot", "Support")


def get_champion_lane_map():
    lane_map = {}
    for lane, champions in LANE_CHAMPIONS.items():
        for name in champions:
            lane_map.setdefault(normalize_champion_name(name), []).append(lane)
    return lane_map


def get_champion_lane_map_with_manual_overrides():
    lane_map = get_champion_lane_map()
    overrides = load_lane_manual_overrides()
    if not overrides:
        return lane_map

    names_by_id = get_champion_mapping()
    for key, lanes in overrides.items():
        champion_name = names_by_id.get(int(key)) if str(key).isdigit() else key
        if champion_name:
            lane_map[normalize_champion_name(champion_name)] = list(lanes)
    return lane_map


def get_lane_assignments(catalog):
    """Return standard and effective lane selections keyed by champion ID."""
    standard_by_name = get_champion_lane_map()
    records_by_id = {
        str(record.get("id")): record
        for record in catalog
        if isinstance(record, dict) and record.get("id") is not None
    }
    ids_by_name = {
        normalize_champion_name(record.get("name", "")): champion_id_value
        for champion_id_value, record in records_by_id.items()
        if record.get("name")
    }
    standard = {
        int(champion_id_value): list(
            standard_by_name.get(normalize_champion_name(record.get("name", "")), [])
        )
        for champion_id_value, record in records_by_id.items()
    }
    effective = {key: list(lanes) for key, lanes in standard.items()}
    for key, lanes in load_lane_manual_overrides().items():
        champion_id_value = key if key in records_by_id else ids_by_name.get(
            normalize_champion_name(key)
        )
        if champion_id_value is not None:
            effective[int(champion_id_value)] = list(lanes)
    return standard, effective


def load_lane_manual_overrides():
    if LANE_OVERRIDES_PATH.is_file():
        try:
            with LANE_OVERRIDES_PATH.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return {}
        values = payload.get("overrides") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            return {}
        overrides = {}
        for key, raw_lanes in values.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(raw_lanes, list):
                continue
            if any(not isinstance(lane, str) or lane not in LANES for lane in raw_lanes):
                continue
            overrides[key] = list(dict.fromkeys(raw_lanes))
        return overrides

    path = lane_manual_overrides_path()
    overrides = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            parsed = parse_lane_override_line(raw_line)
            if parsed is None:
                continue
            if any(lane not in LANES for lane in parsed["lanes"]):
                continue
            key = str(parsed["internal_id"]) if parsed["internal_id"] is not None else parsed["name"]
            overrides[key] = parsed["lanes"]
    except OSError:
        return {}
    return overrides


def save_lane_manual_overrides(overrides):
    if not isinstance(overrides, dict):
        raise ValueError("Champion lane assignments must be a mapping.")
    validated = {}
    for champion_id_value, lanes in overrides.items():
        key = str(champion_id_value).strip()
        if not key or not isinstance(lanes, (list, tuple, set)):
            raise ValueError("Each champion must have a list of lane assignments.")
        selected = list(lanes)
        if any(lane not in LANES for lane in selected):
            raise ValueError("Lane assignments contain an unsupported lane.")
        validated[key] = [lane for lane in LANES if lane in selected]

    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = LANE_OVERRIDES_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {"version": 1, "overrides": validated},
            handle,
            indent=2,
            ensure_ascii=False,
        )
    temporary_path.replace(LANE_OVERRIDES_PATH)


def ensure_lane_manual_overrides_file():
    path = lane_manual_overrides_path()
    if path.exists():
        return path

    default_lines = [
        "-1,Demo Champ [Top,Jungle,Mid,Bot,Support,Other]",
        "# Format: internal_id,name [Lane 1,Lane 2,...]",
        "# Example: 266,Aatrox [Top,Mid]",
        "# Edit this file manually to override lane assignments.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(default_lines) + "\n", encoding="utf-8")
    return path


def parse_lane_override_line(line):
    cleaned = line.strip()
    if not cleaned or cleaned.startswith("#"):
        return None
    match = re.match(r"^\s*(-?\d+)\s*,\s*([^\[]+)\s*\[\s*(.*?)\s*\]\s*$", cleaned)
    if not match:
        return None
    internal_id, name, lane_csv = match.groups()
    lanes = [lane.strip() for lane in lane_csv.split(",") if lane.strip()]
    return {
        "internal_id": int(internal_id),
        "name": name.strip(),
        "lanes": lanes,
    }


def lane_manual_overrides_path():
    return APP_DIR / "champion_lanes_manual.txt"


def get_local_player_participant(game, puuid, summoner_id):
    identities = game.get("participantIdentities", [])
    if not isinstance(identities, list):
        return None
    for identity in identities:
        if not isinstance(identity, dict):
            continue
        player = identity.get("player")
        if not isinstance(player, dict):
            continue
        if puuid and player.get("puuid") == puuid:
            return identity.get("participantId")
    for identity in identities:
        if not isinstance(identity, dict):
            continue
        player = identity.get("player")
        if not isinstance(player, dict):
            continue
        if summoner_id is not None and str(player.get("summonerId")) == str(summoner_id):
            return identity.get("participantId")
    return None


def fetch_matches(session, base_url, puuid, summoner_id, max_games, champ_map):
    games_by_id = {}
    for start in range(0, max_games, 20):
        end = start + 20
        url = (
            f"{base_url}/lol-match-history/v1/products/lol/{puuid}/matches"
            f"?begIndex={start}&endIndex={end}"
        )
        payload = request_json(session, url)
        if payload is None:
            print(f"[Warnung] Match-Historie konnte ab Eintrag {start} nicht geladen werden.")
            break

        matches = payload.get("games", {}).get("games", [])
        if not matches:
            break

        new_matches = 0
        for game in matches:
            game_id = game.get("gameId")
            key = str(game_id) if game_id is not None else repr(game)
            if key not in games_by_id:
                games_by_id[key] = game
                new_matches += 1
        if new_matches == 0:
            break

    match_rows = []
    champion_counts = Counter()
    total_kills = total_deaths = total_assists = 0
    complete_stat_matches = 0

    for game in list(games_by_id.values())[:max_games]:
        participant_id = get_local_player_participant(game, puuid, summoner_id)
        if participant_id is None:
            continue

        participant = next(
            (
                player
                for player in game.get("participants", [])
                if isinstance(player, dict)
                if str(player.get("participantId")) == str(participant_id)
            ),
            None,
        )
        if participant is None:
            continue

        stats = participant.get("stats", {})
        stats = stats if isinstance(stats, dict) else {}
        kills = nonnegative_int(stats.get("kills"))
        deaths = nonnegative_int(stats.get("deaths"))
        assists = nonnegative_int(stats.get("assists"))
        complete_stats = all(value is not None for value in (kills, deaths, assists))
        champ_id = participant.get("championId")
        normalized_champ_id = champion_id(champ_id)
        if normalized_champ_id is not None and normalized_champ_id > 0:
            champion_counts[normalized_champ_id] += 1
        if complete_stats:
            total_kills += kills
            total_deaths += deaths
            total_assists += assists
            complete_stat_matches += 1

        queue_id = champion_id(game.get("queueId"))
        mode = QUEUE_NAMES.get(queue_id, f"Spielmodus {queue_id or 'unbekannt'}")
        timestamp = game.get("gameCreation")
        played_at = (
            datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M")
            if isinstance(timestamp, (int, float)) and timestamp > 0
            else "Zeit unbekannt"
        )
        duration = format_duration(game.get("gameDuration"))
        duration_part = f"Dauer {duration} | " if duration else ""
        win = stats.get("win")
        result = "SIEG" if win is True else "NIEDERLAGE" if win is False else "UNBEKANNT"
        kda = f"{kills}/{deaths}/{assists}" if complete_stats else "–/–/–"
        match_rows.append(
            f"{played_at} | Match {game.get('gameId', 'unbekannt')} | {mode} | "
            f"{result} | {get_champion_name(champ_map, champ_id)} | {duration_part}"
            f"KDA {kda}"
        )

    return (
        match_rows,
        champion_counts,
        total_kills,
        total_deaths,
        total_assists,
        complete_stat_matches,
    )


def get_store_prices(session, base_url):
    payload = request_json(session, f"{base_url}/lol-store/v1/catalog")
    prices = {}
    if not isinstance(payload, list):
        return prices

    for item in payload:
        if item.get("inventoryType") != "CHAMPION":
            continue
        item_id = champion_id(item.get("itemId"))
        for price in item.get("prices", []):
            if str(price.get("currency", "")).upper() in {"IP", "BE", "BLUE_ESSENCE"}:
                try:
                    prices[item_id] = int(price.get("cost"))
                except (TypeError, ValueError):
                    pass
                break
    return prices


def fetch_tft_history(session, base_url, puuid, max_games):
    """Fetch TFT match placements from the LCU match-history product."""
    games_by_id = {}
    for start in range(0, max_games, 20):
        count = min(20, max_games - start)
        url = (
            f"{base_url}/lol-match-history/v1/products/tft/{puuid}/matches"
            f"?begin={start}&count={count}"
        )
        payload = request_json(session, url)
        if payload is None:
            return [], None, "TFT-Match-Historie konnte nicht geladen werden."

        games = payload.get("games", [])
        if not isinstance(games, list) or not games:
            break

        new_games = 0
        for record in games:
            if not isinstance(record, dict):
                continue
            metadata = record.get("metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            game_id = metadata.get("match_id") or record.get("gameId")
            key = str(game_id) if game_id is not None else repr(record)
            if key not in games_by_id:
                games_by_id[key] = record
                new_games += 1
        if new_games == 0:
            break

    rows = []
    placements = []
    skipped = 0
    for record in list(games_by_id.values())[:max_games]:
        if not isinstance(record, dict):
            skipped += 1
            continue
        raw_game = record.get("json", {})
        try:
            game = json.loads(raw_game) if isinstance(raw_game, str) else raw_game
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(game, dict):
            skipped += 1
            continue

        participants = game.get("participants", [])
        if not isinstance(participants, list):
            skipped += 1
            continue
        participant = next(
            (
                player
                for player in participants
                if isinstance(player, dict)
                if player.get("puuid") == puuid
            ),
            None,
        )
        if participant is None:
            skipped += 1
            continue

        try:
            placement = int(participant.get("placement"))
        except (TypeError, ValueError):
            skipped += 1
            continue
        if not 1 <= placement <= 8:
            skipped += 1
            continue

        metadata = record.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        timestamp = (
            game.get("gameCreation")
            or game.get("game_datetime")
            or metadata.get("timestamp")
        )
        played_at = (
            datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M")
            if isinstance(timestamp, (int, float)) and timestamp > 0
            else "Zeit unbekannt"
        )
        game_id = (
            game.get("gameId")
            or game.get("game_id")
            or metadata.get("match_id")
            or "unbekannt"
        )
        queue_info = metadata.get("queue_info", {})
        queue_info = queue_info if isinstance(queue_info, dict) else {}
        mode = game.get("gameMode") or game.get("gameType") or metadata.get("game_mode")
        queue_id = game.get("queueId") or queue_info.get("queueId")
        mode = mode or (f"Queue {queue_id}" if queue_id is not None else "")
        mode_part = f"Mode {mode} | " if mode else ""
        rows.append(
            f"{played_at} | Match {game_id} | {mode_part}Platz {placement}/8"
        )
        placements.append(placement)

    if skipped:
        note = f"{skipped} TFT-Spiel(e) ohne lesbare Platzierung übersprungen."
    else:
        note = None
    return rows, placements, note


def fetch_player_data(max_games=100):
    port, auth_token = get_lcu_credentials()
    base_url = f"https://127.0.0.1:{port}"
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Basic {auth_token}",
            "Accept": "application/json",
        }
    )
    session.verify = False

    output_lines = []

    def log(text=""):
        print(text)
        output_lines.append(text)

    summoner = request_json(
        session, f"{base_url}/lol-summoner/v1/current-summoner", required=True
    )
    puuid = summoner.get("puuid")
    summoner_id = summoner.get("summonerId")
    if not puuid or summoner_id is None:
        raise RuntimeError("Der League Client hat unvollständige Kontodaten geliefert.")

    champ_map = get_champion_mapping()
    log("=== ACCOUNT DETAILS ===")
    game_name = summoner.get("gameName")
    tagline = summoner.get("tagLine")
    riot_id = f"{game_name}#{tagline}" if game_name and tagline else None
    display_name = riot_id or summoner.get("displayName") or summoner.get("name") or "Unbekannt"
    log(f"Spieler: {display_name}")
    log(f"Beschwörer-Level: {summoner.get('summonerLevel', 'Unbekannt')}")
    log(f"Profilbild-ID: {summoner.get('profileIconId', '')}")
    log(f"PUUID: {puuid}")

    honor = request_json(session, f"{base_url}/lol-honor-v2/v1/profile")
    if isinstance(honor, dict):
        log(f"Ehrenstufe: {honor.get('honorLevel', 'Unbekannt')}")

    ranked = request_json(session, f"{base_url}/lol-ranked/v1/ranked-stats/{puuid}")
    if isinstance(ranked, dict) and ranked.get("queues"):
        log("\n--- RANKED ---")
        for queue in ranked["queues"]:
            wins = int(queue.get("wins", 0) or 0)
            losses = int(queue.get("losses", 0) or 0)
            total = wins + losses
            win_rate = wins / total * 100 if total else 0
            log(
                f"{queue.get('queueType', 'Unbekannt')} | "
                f"{queue.get('tier', '')} {queue.get('division', '')} "
                f"({queue.get('leaguePoints', 0)} LP) | "
                f"{wins} Siege / {losses} Niederlagen ({win_rate:.1f} % WR)"
            )

    inventory = request_json(
        session,
        f"{base_url}/lol-champions/v1/inventories/{summoner_id}/champions",
    )
    if isinstance(inventory, list):
        for champion in inventory:
            if not isinstance(champion, dict):
                continue
            champ_id = champion_id(champion.get("id"))
            name = champion.get("name") or champion.get("alias")
            if champ_id is not None and champ_id > 0 and name:
                champ_map.setdefault(champ_id, str(name))

    mastery_payload = request_json(
        session,
        f"{base_url}/lol-champion-mastery/v1/local-player/champion-mastery",
    )
    masteries = mastery_payload if isinstance(mastery_payload, list) else []
    mastery_by_id = {
        champion_id(mastery.get("championId")): mastery
        for mastery in masteries
        if isinstance(mastery, dict)
        and champion_id(mastery.get("championId")) is not None
        and champion_id(mastery.get("championId")) > 0
    }

    mastery_rows = []
    champion_ids = set(mastery_by_id)
    ownership_by_id = {}
    if isinstance(inventory, list):
        for champion in inventory:
            if not isinstance(champion, dict):
                continue
            champ_id = champion_id(champion.get("id"))
            if champ_id is None or champ_id <= 0:
                continue
            if str(champion.get("alias", "")).startswith("Jade_"):
                continue
            champion_ids.add(champ_id)
            ownership = champion_owned(champion)
            if ownership is not None:
                ownership_by_id[champ_id] = ownership
    try:
        if isinstance(inventory, list):
            collection = []
            for champion in inventory:
                if not isinstance(champion, dict):
                    continue
                champ_id = champion_id(champion.get("id"))
                if champ_id is None or champ_id <= 0:
                    continue
                collection.append({"id": champ_id, "owned": champion_owned(champion)})
            APP_DIR.mkdir(parents=True, exist_ok=True)
            temporary_path = CHAMPION_COLLECTION_PATH.with_suffix(".json.tmp")
            with temporary_path.open("w", encoding="utf-8") as handle:
                json.dump({"champions": collection}, handle, indent=2)
            temporary_path.replace(CHAMPION_COLLECTION_PATH)
    except OSError as error:
        print(f"[Warnung] Champion-Sammlung konnte nicht gespeichert werden: {error}")

    lane_map = get_champion_lane_map_with_manual_overrides()
    for champ_id in champion_ids:
        champion_name = get_champion_name(champ_map, champ_id)
        mastery = mastery_by_id.get(champ_id, {})
        score = int(mastery.get("championPoints", 0) or 0)
        mastery_rank = mastery.get("championLevel", 0)
        grade = mastery.get("highestGrade") or "-"
        owned = ownership_by_id.get(champ_id)
        ownership_text = "Owned" if owned is True else "Unowned" if owned is False else "Unknown"
        lanes = lane_map.get(normalize_champion_name(champion_name), [])
        if not lanes:
            mastery_rows.append(
                ("Unassigned", champion_name.casefold(), champion_name, score, ownership_text, mastery_rank, grade)
            )
        for lane in lanes:
            mastery_rows.append(
                (lane, champion_name.casefold(), champion_name, score, ownership_text, mastery_rank, grade)
            )

    mastery_report_lines = ["=== CHAMPION MASTERY BY LANE ==="]
    for lane in [*LANE_CHAMPIONS, "Unassigned"]:
        lane_rows = sorted(
            (row for row in mastery_rows if row[0] == lane),
            key=lambda row: (-row[3], row[1]),
        )
        if not lane_rows:
            continue
        mastery_report_lines.extend(
            [
                "",
                f"--- {lane} ---",
                "Champion | Mastery score | Ownership | Mastery rank | Grade",
            ]
        )
        mastery_report_lines.extend(
            f"{name} | {score} | {ownership} | {rank} | {grade}"
            for _, _, name, score, ownership, rank, grade in lane_rows
        )

    MASTERY_REPORT_PATH.write_text("\n".join(mastery_report_lines) + "\n", encoding="utf-8")
    print(f"[INFO] Champion-Mastery-Bericht gespeichert: {MASTERY_REPORT_PATH}")

    unowned = []
    if isinstance(inventory, list):
        unowned = [
            champion
            for champion in inventory
            if isinstance(champion, dict)
            if champion_id(champion.get("id")) is not None
            and champion_id(champion.get("id")) > 0
            and champion_owned(champion) is False
        ]

    store_prices = get_store_prices(session, base_url)
    log("\n--- FEHLENDE CHAMPIONS ---")
    if not isinstance(inventory, list):
        log("Champion-Inventar konnte nicht geladen werden.")
    elif not unowned:
        log("Keine fehlenden Champions gefunden.")
    else:
        priced_total = 0
        unknown_prices = 0
        for champion in sorted(
            unowned, key=lambda item: str(item.get("name", "")).casefold()
        ):
            champ_id = champion_id(champion.get("id"))
            price = store_prices.get(champ_id)
            if price is None:
                unknown_prices += 1
                price_text = "Preis nicht verfügbar"
            else:
                priced_total += price
                price_text = f"{price:,} BE"
            log(f"- {get_champion_name(champ_map, champ_id)} ({price_text})")
        if unknown_prices:
            log(
                f"Bekannte Preise zusammen: {priced_total:,} BE; "
                f"Preis für {unknown_prices} Champion(s) fehlt, Gesamtbetrag unvollständig."
            )
        else:
            log(f"Benötigte Blaue Essenzen gesamt: {priced_total:,} BE")

    (
        rows,
        champion_counts,
        total_kills,
        total_deaths,
        total_assists,
        complete_stat_matches,
    ) = fetch_matches(session, base_url, puuid, summoner_id, max_games, champ_map)
    sync_match_history_csv(session, base_url, puuid, summoner_id, champ_map, max_games=max_games)
    print(f"[INFO] Match-History-CSV gespeichert: {MATCH_HISTORY_CSV_PATH}")

    log(f"\n--- TOP 5 CHAMPIONS NACH SPIELEN (LETZTE {max_games}) ---")
    if champion_counts:
        for rank, (champ_id, games_played) in enumerate(
            champion_counts.most_common(5), start=1
        ):
            mastery = mastery_by_id.get(champ_id)
            if mastery:
                mastery_rank = (
                    f"Stufe {mastery.get('championLevel', 'unbekannt')} "
                    f"({int(mastery.get('championPoints', 0) or 0):,} Punkte)"
                )
                season_grade = mastery.get("highestGrade") or "unbekannt"
            else:
                mastery_rank = "keine Mastery-Daten"
                season_grade = "unbekannt"
            log(
                f"{rank}. {get_champion_name(champ_map, champ_id)} | "
                f"{games_played} Spiele | Meisterschaftswertung: {mastery_rank} | "
                f"Topwertung: {season_grade}"
            )
    else:
        log("Keine auswertbaren Matches gefunden.")

    log(
        f"\n--- MATCH-HISTORIE ({len(rows)} auswertbare Spiele; "
        f"{complete_stat_matches} mit vollständigen K/D/A-Werten) ---"
    )
    log(f"Matches mit vollständigen K/D/A-Werten: {complete_stat_matches}")
    if rows:
        for row in rows:
            log(row)
    if complete_stat_matches:
        average_kills = total_kills / complete_stat_matches
        average_deaths = total_deaths / complete_stat_matches
        average_assists = total_assists / complete_stat_matches
        overall_ratio = (
            f"{(total_kills + total_assists) / total_deaths:.2f}:1"
            if total_deaths
            else "Perfekt (0 Tode)"
        )
        log("\n--- GESAMT- UND DURCHSCHNITTLICHE KDA ---")
        log(f"Gesamt K/D/A: {total_kills}/{total_deaths}/{total_assists}")
        log(
            f"Durchschnitt pro Spiel K/D/A: "
            f"{average_kills:.2f}/{average_deaths:.2f}/{average_assists:.2f}"
        )
        log(f"Gesamtes KDA-Verhältnis (K+A)/D: {overall_ratio}")
    else:
        log("Keine Match-Statistiken verfügbar; KDA kann nicht berechnet werden.")

    tft_rows, tft_placements, tft_note = fetch_tft_history(
        session, base_url, puuid, max_games
    )
    tft_lines = [f"=== TFT-MATCH-HISTORIE ({len(tft_rows)} Spiele) ===", ""]
    if tft_rows:
        tft_lines.extend(tft_rows)
        average_placement = sum(tft_placements) / len(tft_placements)
        tft_lines.extend(
            [
                "",
                f"Durchschnittliche Platzierung: {average_placement:.2f}/8",
            ]
        )
    else:
        tft_lines.append("Keine TFT-Platzierungen verfügbar.")
    if tft_note:
        tft_lines.extend(["", tft_note])
    APP_DIR.mkdir(parents=True, exist_ok=True)
    TFT_REPORT_PATH.write_text("\n".join(tft_lines) + "\n", encoding="utf-8")
    print(f"\n[INFO] TFT-Bericht gespeichert: {TFT_REPORT_PATH}")

    REPORT_PATH.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    print(f"\n[INFO] Vollständiger Bericht gespeichert: {REPORT_PATH}")


def load_match_history_csv(path):
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            return [row for row in reader if str(row.get("gameId", "")).strip()]
    except (OSError, ValueError):
        return []


MATCH_HISTORY_CSV_PATH = APP_DIR / "lcu_match_history.csv"


def merge_match_history_records(existing_records, new_records):
    merged = {}
    for record in existing_records:
        game_id = str(record.get("gameId", "")).strip()
        if game_id:
            merged[game_id] = dict(record)
    for record in new_records:
        game_id = str(record.get("gameId", "")).strip()
        if not game_id:
            continue
        if game_id in merged:
            merged[game_id].update({
                key: value for key, value in record.items() if value not in (None, "")
            })
        else:
            merged[game_id] = dict(record)
    ordered = sorted(
        merged.values(),
        key=lambda record: int(str(record.get("gameCreation") or 0)),
        reverse=True,
    )
    return ordered


def write_match_history_csv(path, records):
    fieldnames = [
        "gameId",
        "gameCreation",
        "date",
        "queueId",
        "mode",
        "championId",
        "championName",
        "result",
        "kills",
        "deaths",
        "assists",
        "gameDuration",
        "kda",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow({field: record.get(field, "") for field in fieldnames})


def sync_match_history_csv(session, base_url, puuid, summoner_id, champ_map, max_games=100):
    games_by_id = {}
    for start in range(0, max_games, 20):
        end = start + 20
        url = (
            f"{base_url}/lol-match-history/v1/products/lol/{puuid}/matches"
            f"?begIndex={start}&endIndex={end}"
        )
        payload = request_json(session, url)
        if payload is None:
            break
        matches = payload.get("games", {}).get("games", [])
        if not matches:
            break
        for game in matches:
            game_id = game.get("gameId")
            key = str(game_id) if game_id is not None else repr(game)
            if key not in games_by_id:
                games_by_id[key] = game
    recent_records = []
    for game in list(games_by_id.values())[:max_games]:
        participant_id = get_local_player_participant(game, puuid, summoner_id)
        if participant_id is None:
            continue
        participant = next(
            (
                player
                for player in game.get("participants", [])
                if str(player.get("participantId")) == str(participant_id)
            ),
            None,
        )
        if participant is None:
            continue
        stats = participant.get("stats", {})
        stats = stats if isinstance(stats, dict) else {}
        kills = nonnegative_int(stats.get("kills"))
        deaths = nonnegative_int(stats.get("deaths"))
        assists = nonnegative_int(stats.get("assists"))
        complete_stats = all(value is not None for value in (kills, deaths, assists))
        champion_id_value = participant.get("championId")
        champion_name = get_champion_name(champ_map, champion_id_value)
        queue_id = champion_id(game.get("queueId"))
        mode = QUEUE_NAMES.get(queue_id, f"Spielmodus {queue_id or 'unbekannt'}")
        timestamp = game.get("gameCreation")
        played_at = (
            datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M")
            if isinstance(timestamp, (int, float)) and timestamp > 0
            else "Zeit unbekannt"
        )
        recent_records.append({
            "gameId": game.get("gameId"),
            "gameCreation": timestamp,
            "date": played_at,
            "queueId": queue_id,
            "mode": mode,
            "championId": champion_id_value,
            "championName": champion_name,
            "result": "WIN" if stats.get("win") is True else "LOSS" if stats.get("win") is False else "UNKNOWN",
            "kills": kills if kills is not None else "",
            "deaths": deaths if deaths is not None else "",
            "assists": assists if assists is not None else "",
            "gameDuration": game.get("gameDuration", ""),
            "kda": f"{kills}/{deaths}/{assists}" if complete_stats else "",
        })
    if not recent_records:
        return []

    existing_records = load_match_history_csv(MATCH_HISTORY_CSV_PATH)
    merged_records = merge_match_history_records(existing_records, recent_records)
    write_match_history_csv(MATCH_HISTORY_CSV_PATH, merged_records)
    return merged_records


def reset_lane_manual_overrides_file():
    save_lane_manual_overrides({})
    return LANE_OVERRIDES_PATH


if __name__ == "__main__":
    fetch_player_data()
