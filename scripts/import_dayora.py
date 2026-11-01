"""Import journal entries from a Dayora JSON export into the vault."""
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from app import config, db, tz
from app.entries import create_entry, local_date

ENERGY_MAP = {"low": 3, "medium": 5, "high": 8}
MOOD_SCORE_MAP = {4: "good", 3: "okay", 2: "rough", 1: "low"}
VALID_MOODS = {"good", "okay", "rough", "low", "sad"}


@dataclass
class ImportReport:
    imported: int
    skipped_duplicates: int
    skipped_empty: int
    mood_filled: int


class _NoCommit:
    """Wraps a connection so create_entry's commit() is deferred to the caller."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args):
        return self._conn.execute(*args)

    def commit(self):
        pass


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _day(iso: str, zone: str) -> str:
    return local_date(_parse(iso).strftime("%Y-%m-%dT%H:%M:%SZ"), zone)


def import_dayora(conn, path: str, tz: str) -> ImportReport:
    with open(path, encoding="utf-8") as f:
        data = json.load(f).get("data", {})

    score_by_day: dict[str, str] = {}
    for m in sorted(data.get("moodEntries") or [], key=lambda m: m["date"]):
        mood = MOOD_SCORE_MAP.get(m.get("moodScore"))
        if mood:
            score_by_day[_day(m["date"], tz)] = mood

    imported = dup = empty = filled = 0
    wrapped = _NoCommit(conn)
    try:
        for e in data.get("entries") or []:
            text = (e.get("content") or "").strip()
            if not text:
                empty += 1
                continue
            if conn.execute(
                "SELECT 1 FROM entries WHERE source_id = ?", (e["id"],)
            ).fetchone():
                dup += 1
                continue
            when = _parse(e["date"])
            mood = e.get("mood") if e.get("mood") in VALID_MOODS else None
            if mood is None:
                mood = score_by_day.get(_day(e["date"], tz))
                if mood:
                    filled += 1
            energy = ENERGY_MAP.get(e.get("energyLevel"))
            create_entry(
                wrapped,
                text=text,
                mood=mood,
                energy=energy,
                now=when,
                tz=tz,
                source_id=e["id"],
                energy_is_approximate=energy is not None,
            )
            imported += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ImportReport(imported, dup, empty, filled)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m scripts.import_dayora PATH", file=sys.stderr)
        return 2
    conn = db.connect(config.get_db_path())
    db.init_db(conn)
    report = import_dayora(conn, argv[1], tz.get_timezone(conn))
    print(report)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
