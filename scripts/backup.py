"""Consistent, pruned backups of the Animus database.

Run by the animus-backup systemd timer as the animus user:

    python -m scripts.backup

Reads ANIMUS_DB_PATH (default data/vault.db) and ANIMUS_BACKUP_DIR (default backups/ in the app
directory). Uses the SQLite backup API, so a backup taken while the app writes is still a valid
database. Logs only counts and reason codes, never any journal text. Exit code 1 on failure.
"""
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from app import config

log = logging.getLogger("animus.backup")

KEEP_DAILY = 14
KEEP_WEEKLY = 8
NAME_RE = re.compile(r"^animus-(\d{8})-(\d{6})(?:-(\d+))?\.db$")  # -N: second backup in one second


class BackupError(Exception):
    """A backup failed; `code` is a short reason code safe to log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def default_backup_dir() -> str:
    return os.environ.get("ANIMUS_BACKUP_DIR") or str(config.APP_DIR / "backups")


def _fsync_path(path, flags=os.O_RDONLY) -> None:
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path) -> None:
    if os.name == "nt":  # directories cannot be opened for fsync on Windows
        return
    _fsync_path(path)


def make_backup(db_path, out_dir, now: datetime) -> Path:
    src_path = Path(db_path)
    if not src_path.is_file():
        raise BackupError("source_missing")
    out = Path(out_dir)
    try:
        out.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise BackupError("out_dir_unwritable") from None
    base = f"animus-{now:%Y%m%d-%H%M%S}"
    # Unique per run, so two backups in one second never overwrite each other.
    tmp = out / f".{base}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
    except OSError:
        raise BackupError("write_failed") from None
    src = dst = None
    try:
        try:
            src = sqlite3.connect(str(src_path), timeout=30)
            dst = sqlite3.connect(str(tmp))
            src.backup(dst)
        except sqlite3.Error:
            raise BackupError("backup_failed") from None
        try:
            dst.execute("PRAGMA journal_mode=DELETE")
            result = dst.execute("PRAGMA integrity_check").fetchall()
        except sqlite3.Error:
            raise BackupError("integrity_failed") from None
        if result != [("ok",)]:
            raise BackupError("integrity_failed")
        dst.close()
        dst = None
        try:
            os.chmod(tmp, 0o600)
            _fsync_path(tmp, os.O_RDWR)  # data on disk before the file gets its final name
            final = _publish(tmp, out, base)
            _fsync_dir(out)  # and the new name on disk too
        except OSError:
            raise BackupError("write_failed") from None
    except BaseException:
        if dst is not None:
            dst.close()
            dst = None
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if src is not None:
            src.close()
        if dst is not None:
            dst.close()
    return final


def _publish(tmp: Path, out: Path, base: str) -> Path:
    """Give tmp its final name without ever replacing an existing backup (hard link, then unlink)."""
    for n in range(1000):
        final = out / (f"{base}.db" if n == 0 else f"{base}-{n}.db")
        try:
            os.link(tmp, final)
        except FileExistsError:
            continue
        tmp.unlink()
        return final
    raise OSError("no free backup name")


def _stamp(path: Path) -> datetime | None:
    m = NAME_RE.match(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _order(path: Path) -> tuple:
    m = NAME_RE.match(path.name)
    return (_stamp(path), int(m.group(3) or 0))


def prune(out_dir, keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY,
          now: datetime | None = None) -> list[Path]:
    """Delete old backups; return the deleted paths.

    Counted from `now`: keeps the newest copy of each day in the last `keep_daily` days and of each
    ISO week in the last `keep_weekly` weeks. The newest copy overall is never deleted. Only files
    named animus-YYYYMMDD-HHMMSS[-N].db are touched.
    """
    out = Path(out_dir)
    if not out.is_dir():
        return []
    now = now or datetime.now()
    today = now.date()
    this_monday = today - timedelta(days=today.weekday())
    found = [p for p in out.iterdir() if _stamp(p) is not None and p.is_file()]
    if not found:
        return []
    found.sort(key=_order, reverse=True)
    keep = {found[0]}
    days: set = set()
    weeks: set = set()
    for path in found:  # newest first, so the first file seen per bucket is its newest
        day = _stamp(path).date()
        monday = day - timedelta(days=day.weekday())
        if day not in days and (today - day).days < keep_daily:
            days.add(day)
            keep.add(path)
        if monday not in weeks and (this_monday - monday).days // 7 < keep_weekly:
            weeks.add(monday)
            keep.add(path)
    deleted = []
    for path in found:
        if path in keep:
            continue
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            log.warning("backup prune failed reason=unlink_failed")
    return deleted


def main(now: datetime | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    now = now or datetime.now()
    out_dir = default_backup_dir()
    try:
        make_backup(config.get_db_path(), out_dir, now)
    except BackupError as exc:
        log.error("backup failed reason=%s", exc.code)
        return 1
    except Exception:
        log.error("backup failed reason=unexpected")
        return 1
    deleted = prune(out_dir, now=now)
    remaining = sum(1 for p in Path(out_dir).iterdir() if NAME_RE.match(p.name))
    log.info("backup ok kept=%d pruned=%d", remaining, len(deleted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
