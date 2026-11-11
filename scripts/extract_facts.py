"""Backfill: note the facts (people, events, worries, wins...) of every entry that has none.

Run on the VM as the animus user:  .venv/bin/python -m scripts.extract_facts [--db PATH]
Prints counts only, never entry text.
"""
import argparse
import sys

from app import config, db, extract, jobs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.get_db_path())
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    db.init_db(conn)
    llm = jobs.resolve_llm(None)
    total = 0
    while extract.missing_count(conn):
        done = extract.fill_missing(conn, llm, limit=10)
        total += done
        print(f"facts noted for {total} entries so far", flush=True)
        if done == 0:
            break
    print(f"done: {total} noted, {extract.missing_count(conn)} without facts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
