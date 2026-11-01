"""Copy the database to a new place safely (used once to move vault.db into data/).

    python -m scripts.move_db SRC DST

Copies with the SQLite backup API, checks integrity, verifies the row count of every table, and
only then puts the copy at DST. SRC is never changed or deleted; an existing DST is never
overwritten. Prints table names and counts only. Stop the animus service first so no write
lands between the copy and the switch.
"""
import os
import sqlite3
import sys
from pathlib import Path


class MoveError(Exception):
    """The move was refused or failed; the message is a short reason code."""


def _tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [r[0] for r in rows]


def _count(conn: sqlite3.Connection, table: str) -> int:
    quoted = '"' + table.replace('"', '""') + '"'
    return conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]


def move(src, dst) -> dict[str, int]:
    src_path, dst_path = Path(src), Path(dst)
    if not src_path.is_file():
        raise MoveError("source_missing")
    if dst_path.exists():
        raise MoveError("destination_exists")
    dst_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = dst_path.with_name(f".{dst_path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    s = d = None
    try:
        s = sqlite3.connect(str(src_path), timeout=30)
        d = sqlite3.connect(str(tmp))
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
        if d.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise MoveError("integrity_failed")
        if _tables(d) != _tables(s):
            raise MoveError("tables_differ")
        counts = {}
        for table in _tables(s):
            counts[table] = _count(s, table)
            if _count(d, table) != counts[table]:
                raise MoveError("count_mismatch")
        d.close()
        d = None
        os.chmod(tmp, 0o600)
        os.link(tmp, dst_path)  # fails if DST appeared meanwhile; never overwrites
        tmp.unlink()
        return counts
    except sqlite3.Error:
        tmp.unlink(missing_ok=True)
        raise MoveError("copy_failed") from None
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if d is not None:
            d.close()
        if s is not None:
            s.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m scripts.move_db SRC DST")
        return 2
    try:
        counts = move(argv[0], argv[1])
    except MoveError as exc:
        print(f"move refused: {exc}")
        return 1
    except FileExistsError:
        print("move refused: destination_exists")
        return 1
    for table, n in counts.items():
        print(f"{table}: {n}")
    print("Copied and verified. The source file was left in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
