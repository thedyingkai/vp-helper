from __future__ import annotations

import contextlib
import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .models import VPError

FIELDS = ["start_time", "contest", "url", "solved", "penalty", "rank", "prize"]


def config_directory() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vp"


def state_directory() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "vp"


def desktop() -> Path:
    try:
        p = subprocess.run(["xdg-user-dir", "DESKTOP"], check=True, capture_output=True, text=True).stdout.strip()
        if p:
            return Path(p)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    return Path.home() / "Desktop"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".vp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (ValueError, OSError) as exc:
        raise VPError(f"Cannot read {path}: {exc}") from exc


@contextlib.contextmanager
def file_lock(path: Path, *, nonblocking: bool = False):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        except BlockingIOError as exc:
            raise VPError("This virtual contest is already being monitored.") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def update_history(path: Path, row: dict) -> None:
    with file_lock(path.with_name("." + path.name + ".lock")):
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(5):
            stamp = _stamp(path)
            rows = read_history(path)
            key = str(row["start_time"]), str(row["url"])
            matched = False
            for old in rows:
                if (old["start_time"], old["url"]) == key:
                    old.update({field: str(row.get(field, "")) for field in FIELDS})
                    matched = True
                    break
            if not matched:
                rows.append({field: str(row.get(field, "")) for field in FIELDS})
            fd, temp = tempfile.mkstemp(prefix=".vp-history-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)
                    f.flush()
                    os.fsync(f.fileno())
                # Re-read external edits made by an editor which does not hold
                # our advisory lock, instead of replacing a known stale file.
                if stamp != _stamp(path):
                    continue
                os.chmod(temp, 0o600)
                os.replace(temp, path)
                return
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        raise VPError("History CSV keeps changing externally; the pending update was preserved in VP state.")


def _stamp(path: Path) -> tuple | None:
    try:
        info = path.stat()
        return info.st_ino, info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != FIELDS:
            raise VPError(f"Unexpected CSV columns in {path}; expected {','.join(FIELDS)}")
        return list(reader)
