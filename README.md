# Riftboard

Riftboard is a Python/PySide6 companion for League of Legends and Teamfight
Tactics. It connects to the running League Client, presents player and match
information in a desktop interface, and has a LoLdle integration
in an embedded browser.

## Capability showcase

### Player overview

- Shows the connected Riot ID or player name, profile icon, level, and client
  status when those details are available.
- Summarizes recent League matches with separate average kills, deaths, and
  assists. Incomplete K/D/A records are excluded from the averages.
- Displays the five highest-mastery champions and the player's collection
  status when ownership data is available.
- Shows the most frequently played lane based on the lane recorded in eligible
  League match reports. Only recognized Summoner's Rift queue IDs with a
  compatible game mode are included (for example, Normal Draft, Ranked,
  Quickplay, and Clash). ARAM, ARAM Mayhem, Arena, URF, and unknown queues are
  excluded. The player's recorded individual/team position takes priority over
  role metadata; unavailable lane data is not guessed. Non-lane modes are
  explicitly labeled “Lane nicht verfügbar” in the match report.
- Shows a separate mastery lane bias based on mastery points for champions
  assigned to each lane. A multi-lane champion contributes to each assigned
  lane, and ties are shown together.

### Champion mastery and lane assignments

- Lists mastery points, levels, seasonal best grade where returned by the
  client, and effective lanes.
- Supports search, sorting, and filtering by lane. Multiple assigned lanes are
  displayed together, separated by ` / `, and a champion with several lanes is
  included in each corresponding lane filter.
- Opens the Champion Lane Assigner as a separate reusable window from Settings
  or the context menu.
- Lets users independently assign any combination of Top, Jungle, Mid, Bot,
  and Support. The fixed action bar provides Cancel, Close, Apply, and Reset to
  Standard. Changes remain pending until Apply succeeds.
- Reset to Standard confirms first, refreshes the champion roster from
  Data Dragon (falling back to CommunityDragon), and stages standard lane
  checkboxes without saving until Apply. Data Dragon does not publish lane-role
  recommendations, so lane defaults come from Riftboard's maintained standard
  assignments; a failed reference-data refresh leaves the current assignments
  untouched.
- Resolves lanes in priority order: saved manual override, standard/community
  assignment, then unassigned fallback. An explicitly empty manual assignment
  remains empty.

### Match history

- Keeps standard League and TFT history separate.
- Shows available League match details including champion, outcome, K/D/A,
  recorded lane, duration, queue, and date. Reports saved before lane display
  was added remain readable and show an unavailable lane.
- Shows TFT placement, mode, and date. Average placement uses only valid
  placements from 1 through 8; supported summary rates are calculated from the
  same valid history.

### Champion data and LoLdle

- Uses CommunityDragon champion reference data and Data Dragon as a fallback
  source, with cached catalog data and champion portraits.
- Uses a dedicated persistent LoLdle browser profile for cookies, local site
  storage, and disk cache. On Windows it is stored under
  `%APPDATA%\Riftdata\loldle-browser`; normal refreshes do not clear it.

### Settings and refresh controls

- English and German interface language selection, persisted across restarts.
- Configurable League Client lockfile path, automatic path detection, a
  connection test, and an option to restore automatic detection.
- A confirmed **Reset all local data** action. It disables the background
  lockfile watcher, closes Riftboard, and removes app-owned settings, reports,
  champion caches, and the LoLdle browser profile before the next launch. The
  next launch opens first-start settings.
- Separate actions for refreshing data and refreshing the display. **Refresh**
  fetches current data from the League Client. **Refresh display** rereads the
  saved local reports and redraws the UI without fetching League data.
- Optional automatic companion startup when the League Client lockfile becomes
  available; it is disabled by default.

## Install and run

Riftboard requires Python 3.10 or newer and a running League Client for live
player data.

```powershell
python -m pip install -r requirements.txt
python main.py
```

The packaged Windows build is created with:

```powershell
python -m PyInstaller --clean --noconfirm Riftboard.spec
```

The onedir executable is written to `dist\Riftboard\Riftboard.exe`.

Run the focused service and UI tests with:

```powershell
python -m unittest discover -s tests -v
```

## Local data, storage, and privacy

On Windows, user settings, generated reports, catalog cache, and lane overrides
are stored under `%APPDATA%\Riftdata`. Champion portraits are cached separately
under the user's local cache directory. The main files include:

| File | Purpose |
| --- | --- |
| `preferences.json` | Language, lockfile selection, and other user settings |
| `champion_lanes_manual.json` | User lane overrides, separate from reference data |
| `cache\champion-catalog.json` | Cached CommunityDragon/Data Dragon champion catalog |
| `lcu_combined_account_report.txt` | Player overview and League match report |
| `lcu_tft_match_history.txt` | TFT match history and placement summary |
| `lcu_champion_mastery.txt` | Generated mastery report |
| `lcu_match_history.csv` | Accumulated League match records |
| `champion_collection.json` | Ownership data when available from the client |

Downloaded reference data is not used to store user preferences. Ordinary
startup and refreshes do not clear settings, reports, cached reference data, or
the LoLdle browser profile.

The Settings page's explicit full-reset action is destructive: after
confirmation, Riftboard closes and clears its owned storage on next launch so
the embedded browser has time to release its profile files. It also clears the
separate champion-icon cache under the user's local cache directory. Files
outside Riftboard's documented storage/cache locations are not touched.

The League Client lockfile contains local authentication credentials. Riftboard
reads it only to authenticate to the local client; it does not save or display
the password. The client connection uses Riot's local client interface, which
is undocumented and may change without notice.

## Automatic startup behavior

Enable **Automatically start when the League Client lockfile becomes available**
in Settings to run a small watcher through Riftboard's normal entry point. It
polls for a readable lockfile and launches or reuses Riftboard when the file
appears; champion select or an in-game process is not required. If the companion
is closed while the client remains open, the watcher avoids repeatedly
relaunching it for that client session.

The watcher remains active after the main window closes. It does not enable
Windows-login startup and must be started again after Windows restarts. Disable
the setting to turn the watcher off. The watcher and GUI use a per-user local
socket to reuse an already-running companion instance.

New champions in the reference catalog appear after catalog refresh. Until a
standard lane assignment is available, they remain unassigned unless the user
sets a manual lane override.
