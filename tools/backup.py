"""Consistent local SQLite backup, including committed WAL transactions."""

import argparse
import sqlite3
from pathlib import Path


def backup(source: Path, destination: Path):
    source = source.resolve(strict=True)
    destination = destination.resolve()
    # Exclusive creation prevents accidental replacement of an existing backup.
    with destination.open("xb"):
        pass
    reader = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)
        if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
    finally:
        reader.close()
        writer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    backup(args.database, args.output)
    print(f"Backup verified: {args.output}")
