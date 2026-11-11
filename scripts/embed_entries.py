"""One-off backfill: embed every entry that has no embedding yet.

Run on the VM as the animus user:  .venv/bin/python -m scripts.embed_entries [--db PATH]
Prints counts only, never entry text.
"""
import argparse
import os
import sys

from app import config, db, embeddings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.get_db_path())
    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    db.init_db(conn)
    embedder = embeddings.Embedder()
    total = 0
    while True:
        done = embeddings.fill_missing(conn, embedder, limit=200)
        total += done
        if done == 0:
            break
        print(f"embedded {total} so far", flush=True)
    notes = embeddings.fill_missing_notes(conn, embedder, limit=500)
    print(f"memory notes embedded: {notes}")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM entries e LEFT JOIN entry_embeddings v ON v.entry_id = e.id WHERE v.entry_id IS NULL"
    ).fetchone()[0]
    print(f"done: {total} embedded, {remaining} without embedding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
