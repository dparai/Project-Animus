"""Off-site copies of the Animus journal on Google Drive, via rclone.

Run nightly by the animus-offsite systemd timer as the animus user:

    python -m scripts.offsite

Two independent halves; one failing never stops the other, and the exit code is 1 if either failed:

1. Readable copy: the owner's ENTRIES ONLY (no reflections, chats or memory notes), one HTML file per
   month, uploaded with rclone's HTML import so Drive turns each into a Google Doc. The first run
   (no state file) exports every month; later runs only the current and previous month.
2. Encrypted copy: the nightly database backups, mirrored with `rclone sync` to an rclone `crypt`
   remote, so Drive only ever sees encrypted names and contents.

Reads ANIMUS_DB_PATH and ANIMUS_BACKUP_DIR like scripts/backup.py, plus ANIMUS_RCLONE_DOCS_REMOTE
(default "gdrive:Animus Journal") and ANIMUS_RCLONE_BACKUP_REMOTE (default "gdrive-crypt:").
RCLONE_CONFIG points rclone at its config. Logs only counts and reason codes, never entry text.
"""
import html
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config
from app.tz import get_timezone

log = logging.getLogger("animus.offsite")

DEFAULT_DOCS_REMOTE = "gdrive:Animus Journal"
DEFAULT_BACKUP_REMOTE = "gdrive-crypt:"
STATE_FILE = "offsite_state.json"
RCLONE_TIMEOUT = 3600
MAX_DELETE = "5"

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
SHORT_MONTHS = [m[:3] for m in MONTHS]
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class OffsiteError(Exception):
    """A half failed; `code` is a short reason code safe to log."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def open_readonly(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _to_local(created_at: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(created_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _heading(row, tz: ZoneInfo) -> str:
    dt = _to_local(row["created_at"], tz)
    parts = [f"{DAYS[dt.weekday()]} {dt.day} {SHORT_MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M}"]
    if row["mood"]:
        parts.append(f"mood: {row['mood']}")
    if row["energy_score"] is not None:
        mark = "~" if row["energy_is_approximate"] else ""
        parts.append(f"energy {mark}{row['energy_score']}/10")
    return " · ".join(parts)


def _paragraphs(text: str) -> str:
    out = []
    for block in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        block = block.strip("\n")
        if block.strip():
            out.append("<p>" + html.escape(block).replace("\n", "<br>\n") + "</p>")
    return "\n".join(out)


def render_month(month: str, rows, tz: ZoneInfo) -> str:
    year, mon = month.split("-")
    title = f"Animus Journal {MONTHS[int(mon) - 1]} {year}"
    ordered = sorted(rows, key=lambda r: (_to_local(r["created_at"], tz), r["id"]))
    parts = ["<!DOCTYPE html>", '<html lang="en">', '<head><meta charset="utf-8">',
             f"<title>{html.escape(title)}</title></head>", "<body>", f"<h1>{html.escape(title)}</h1>"]
    for row in ordered:
        parts.append("<section>")
        parts.append(f"<h2>{html.escape(_heading(row, tz))}</h2>")
        parts.append(_paragraphs(row["text"]))
        parts.append("</section>")
    parts += ["</body>", "</html>", ""]
    return "\n".join(parts)


def read_months(db_path) -> tuple[dict, ZoneInfo]:
    """Entries grouped by local_date month ("YYYY-MM"), plus the stored timezone."""
    conn = open_readonly(db_path)
    try:
        tz = ZoneInfo(get_timezone(conn))
        rows = conn.execute(
            "SELECT id, created_at, local_date, text, mood, energy_score, energy_is_approximate "
            "FROM entries ORDER BY local_date, created_at, id"
        ).fetchall()
    finally:
        conn.close()
    months: dict = {}
    for row in rows:
        months.setdefault(row["local_date"][:7], []).append(row)
    return months, tz


def _recent_months(now: datetime) -> set:
    cur = f"{now.year}-{now.month:02d}"
    prev = f"{now.year - 1}-12" if now.month == 1 else f"{now.year}-{now.month - 1:02d}"
    return {cur, prev}


def _state_path(db_path) -> Path:
    return Path(db_path).resolve().parent / STATE_FILE


def _has_full_export(db_path) -> bool:
    try:
        data = json.loads(_state_path(db_path).read_text(encoding="utf-8"))
        return bool(data.get("docs_full_at"))
    except (OSError, ValueError, AttributeError):
        return False


def _save_full_export(db_path, now: datetime) -> None:
    path = _state_path(db_path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"docs_full_at": now.isoformat()}), encoding="utf-8")
    os.replace(tmp, path)


def _rclone(*args: str) -> bool:
    """Run rclone; its output is dropped (it could name files). True on exit 0."""
    try:
        proc = subprocess.run(["rclone", *args], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=RCLONE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def export_docs(db_path, remote: str, now: datetime | None = None) -> int:
    """Upload the monthly HTML files; return how many were uploaded."""
    try:
        months, tz = read_months(db_path)
    except (sqlite3.Error, ValueError, OSError, KeyError):
        raise OffsiteError("docs_failed") from None
    now = now or datetime.now(tz)
    full = not _has_full_export(db_path)
    wanted = sorted(months) if full else sorted(m for m in months if m in _recent_months(now))
    remote = remote.rstrip("/")
    uploaded = failed = 0
    tmpdir = tempfile.mkdtemp(prefix="animus-offsite-")
    try:
        for month in wanted:
            name = f"Animus Journal {month}.html"
            path = Path(tmpdir) / name
            path.write_text(render_month(month, months[month], tz), encoding="utf-8")
            if _rclone("copyto", str(path), f"{remote}/{name}", "--drive-import-formats", "html", "--drive-export-formats", "html"):
                uploaded += 1
            else:
                failed += 1
            path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if failed:
        log.error("offsite docs_uploaded=%d docs_failed=%d", uploaded, failed)
        raise OffsiteError("docs_failed")
    if full:
        try:
            _save_full_export(db_path, now)
        except OSError:
            log.warning("offsite state_unwritable")
    return uploaded


def sync_backups(backup_dir, remote: str) -> None:
    folder = Path(backup_dir)
    if not folder.is_dir() or not any(p.is_file() for p in folder.glob("animus-*.db")):
        raise OffsiteError("backup_dir_empty")
    if not _rclone("sync", str(folder), remote, "--include", "animus-*.db",
                   "--max-delete", MAX_DELETE, "--immutable"):
        raise OffsiteError("backup_sync_failed")


def main(now: datetime | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if shutil.which("rclone") is None:
        log.error("offsite failed reason=rclone_missing")
        return 1
    docs_remote = os.environ.get("ANIMUS_RCLONE_DOCS_REMOTE") or DEFAULT_DOCS_REMOTE
    backup_remote = os.environ.get("ANIMUS_RCLONE_BACKUP_REMOTE") or DEFAULT_BACKUP_REMOTE
    backup_dir = os.environ.get("ANIMUS_BACKUP_DIR") or str(config.APP_DIR / "backups")
    status = 0
    try:
        n = export_docs(config.get_db_path(), docs_remote, now)
        log.info("offsite docs_uploaded=%d", n)
    except OffsiteError as exc:
        log.error("offsite failed reason=%s", exc.code)
        status = 1
    except Exception:
        log.error("offsite failed reason=unexpected")
        status = 1
    try:
        sync_backups(backup_dir, backup_remote)
        log.info("offsite backups_synced")
    except OffsiteError as exc:
        log.error("offsite failed reason=%s", exc.code)
        status = 1
    except Exception:
        log.error("offsite failed reason=unexpected")
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
