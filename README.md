# Riftboard

Riftboard is a PySide6 League companion. It reads League Client data from the
local client, uses Data Dragon and CommunityDragon for champion reference data
and icons, and keeps LoLdle in the existing embedded web view.

## Local data and privacy

On Windows, preferences, reports, cached champion reference data, and manual
lane overrides are stored under `%APPDATA%\Riftdata`. Preferences and manual
overrides are separate from downloaded source data. Existing report files,
legacy lane override files, and the LoLdle browser profile are not cleared by
ordinary startup or settings changes.

The League Client lockfile contains local authentication credentials. Riftboard
reads it only to connect to the local client; credentials are not written to
application settings or emitted in status messages. The client connection uses
Riot's local client interface, which is not a documented public API and may
change without notice.

## Automatic launch

Enable **Automatically start when the League Client lockfile becomes available**
in Settings to start a small watcher process through Riftboard's normal entry
point. It polls for a readable lockfile, starts or reuses the companion as soon
as the client creates one, and remains active after the main window closes.
Disable the setting to stop the watcher. It does not enable Windows-login
startup; after Windows restarts, start Riftboard once to run the enabled
watcher again.

The watcher and GUI use a per-user local socket to reuse an already-running
Riftboard instance. A lockfile becoming available is the trigger; champion
select or a game is not required.

Champion catalog data is refreshed from CommunityDragon/Data Dragon and cached
separately from user settings. Newly listed champions appear in the collection
and lane assigner when that data refreshes. If the existing standard lane data
does not cover a new champion, it is shown as unassigned until the user assigns
lanes.
