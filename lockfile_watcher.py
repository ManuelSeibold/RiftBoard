import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalSocket

import lol


INSTANCE_SERVER_NAME = "riftboard-companion-single-instance"
POLL_INTERVAL_SECONDS = 1.5
FALLBACK_SCAN_INTERVAL_SECONDS = 15


def _lockfile_signature(path):
    try:
        port = lol.validate_lockfile(path)
        fields = Path(path).read_text(encoding="utf-8").strip().split(":")
        return (str(Path(path).resolve()), fields[1], str(port))
    except (OSError, ValueError, IndexError):
        return None


def _find_path(configured_path, next_fallback_scan):
    if configured_path:
        return lol.find_lockfile_path(configured_path), next_fallback_scan
    for candidate in lol.default_lockfile_candidates():
        if candidate.is_file():
            return candidate, next_fallback_scan
    now = time.monotonic()
    if now >= next_fallback_scan:
        return lol.find_lockfile_path(), now + FALLBACK_SCAN_INTERVAL_SECONDS
    return None, next_fallback_scan


def _application_is_running():
    socket = QLocalSocket()
    socket.connectToServer(INSTANCE_SERVER_NAME)
    connected = socket.waitForConnected(150)
    if connected:
        socket.write(b"activate")
        socket.flush()
        socket.disconnectFromServer()
    return connected


def _launch_companion():
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    else:
        command = [sys.executable, str(Path(__file__).with_name("main.py"))]
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    subprocess.Popen(
        command,
        close_fds=True,
        creationflags=creation_flags,
        cwd=str(Path(__file__).resolve().parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_watcher():
    lock_path = lol.APP_DIR / "lockfile-watcher.lock"
    lock = QLockFile(str(lock_path))
    if not lock.tryLock(0):
        return 0

    last_signature = None
    next_fallback_scan = 0.0
    try:
        while True:
            preferences = lol.load_preferences()
            if not preferences.get("auto_start_on_lockfile", False):
                return 0
            configured_path = os.environ.get("LEAGUE_LOCKFILE") or str(
                preferences.get("lockfile_path") or ""
            ).strip()
            path, next_fallback_scan = _find_path(
                configured_path, next_fallback_scan
            )
            signature = _lockfile_signature(path) if path is not None else None
            if signature is None:
                if path is None or not path.exists():
                    last_signature = None
            elif signature != last_signature:
                try:
                    if not _application_is_running():
                        _launch_companion()
                except OSError:
                    pass
                last_signature = signature
            time.sleep(POLL_INTERVAL_SECONDS)
    finally:
        lock.unlock()


def start_watcher():
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--lockfile-watcher"]
    else:
        command = [
            sys.executable,
            str(Path(__file__).with_name("main.py")),
            "--lockfile-watcher",
        ]
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    try:
        return subprocess.Popen(
            command,
            close_fds=True,
            creationflags=creation_flags,
            cwd=str(Path(__file__).resolve().parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
