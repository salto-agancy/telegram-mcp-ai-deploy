"""Shared slot allocation, with short transactions across the bot and facade."""

import os
import sqlite3
from contextlib import closing, contextmanager


def slot_count():
    count = int(os.getenv("CLOUD_SLOTS", "10"))
    if not 1 <= count <= 100:
        raise ValueError("CLOUD_SLOTS must be between 1 and 100")
    return count


class Slots:
    def __init__(self, path, count):
        self.path, self.count = path, count
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        with self.db(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cloud_slots (
                id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, wrapped BLOB
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS cloud_invites (digest TEXT PRIMARY KEY, expires INTEGER)")
            db.execute("""CREATE TABLE IF NOT EXISTS cloud_launches (
                signature TEXT PRIMARY KEY, nonce_hash TEXT UNIQUE, user_id INTEGER,
                start_param TEXT, expires INTEGER NOT NULL
            )""")
            if db.execute("SELECT 1 FROM cloud_slots WHERE id>? AND user_id IS NOT NULL", (count,)).fetchone():
                raise ValueError("Cannot remove a slot that is in use")
            db.executemany("INSERT OR IGNORE INTO cloud_slots(id) VALUES (?)", ((i,) for i in range(1, count + 1)))

    @contextmanager
    def db(self, *, write=False):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("PRAGMA secure_delete=ON")
                db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def clear_launches(db, user):
        # Retain the spent signature until expiry even if the slot is reclaimed.
        db.execute("UPDATE cloud_launches SET user_id=NULL, start_param=NULL, nonce_hash=NULL WHERE user_id=?", (user,))

    def lookup(self, user_id):
        with self.db() as db:
            return db.execute("SELECT * FROM cloud_slots WHERE user_id=?", (user_id,)).fetchone()

    def claim(self, db, user_id):
        row = db.execute("SELECT id FROM cloud_slots WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return row["id"]
        row = db.execute("SELECT id FROM cloud_slots WHERE user_id IS NULL AND id<=? ORDER BY id LIMIT 1",
                         (self.count,)).fetchone()
        if row:
            db.execute("UPDATE cloud_slots SET user_id=? WHERE id=?", (user_id, row["id"]))
            return row["id"]
        return None
