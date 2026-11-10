"""Run with AstrBot stopped, then restore v0.1.12 source. Keep new captures."""
import argparse
import sqlite3
from pathlib import Path


def downgrade(database, backup):
    database, backup = Path(database), Path(backup)
    if not database.is_file() or backup.exists() or database.resolve() == backup.resolve():
        raise ValueError("Existing database and a new, different backup path required")
    db = sqlite3.connect(database)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5, "Expected schema 5"
        assert not db.execute("SELECT 1 FROM query_runs WHERE status='running'").fetchone(), "Stop AstrBot first"
        assert not db.execute("SELECT 1 FROM notifications WHERE status='sending'").fetchone(), "Resolve in-flight sends first"
        snapshot = sqlite3.connect(backup)
        try:
            db.backup(snapshot)
        finally:
            snapshot.close()
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE subscriptions SET enabled=0 WHERE reminder_type='days'")
            db.execute("ALTER TABLE subscriptions DROP COLUMN days_threshold")
            db.execute("ALTER TABLE subscriptions DROP COLUMN reminder_type")
            # Old v0.1.12 cannot replay a queued days-based notification.
            db.execute("UPDATE notifications SET status='cancelled' WHERE status='pending'")
            db.execute("PRAGMA user_version=4")
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not db.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("backup", help="New SQLite backup file, must not already exist")
    args = parser.parse_args()
    downgrade(args.database, args.backup)
    print("Schema 4 restored; amount reminders retained, days reminders disabled, capture data retained.")
