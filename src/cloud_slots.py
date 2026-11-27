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
        with self.db(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS cloud_slots (
                id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE, wrapped BLOB
            )""")
            db.execute("CREATE TABLE IF NOT EXISTS cloud_invites (digest TEXT PRIMARY KEY, expires INTEGER)")
            if db.execute("SELECT 1 FROM cloud_slots WHERE id>? AND user_id IS NOT NULL", (count,)).fetchone():
                raise ValueError("Cannot remove a slot that is in use")
            db.executemany("INSERT OR IGNORE INTO cloud_slots(id) VALUES (?)", ((i,) for i in range(1, count + 1)))

    @contextmanager
    def db(self, *, write=False):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

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
