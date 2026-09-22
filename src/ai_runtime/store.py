import json
import os
import sqlite3
import time
from pathlib import Path


class Store:
    """Small synchronous transactions, called only from the daemon event loop."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = (self.root / 'daemon.lock').open('a+b')
        self._lock.seek(0, 2)
        if not self._lock.tell():
            self._lock.write(b'0')
            self._lock.flush()
        self._lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise RuntimeError('another runtime owns this data directory') from None
        self.db = sqlite3.connect(self.root / "runtime.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, cwd TEXT, status TEXT, generation INTEGER,
                created_at REAL);
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY, session_id TEXT, request_id TEXT, code TEXT,
                timeout REAL, status TEXT, reason TEXT, created_at REAL,
                UNIQUE(session_id, request_id));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                execution_id TEXT, kind TEXT, payload TEXT, created_at REAL);
            CREATE INDEX IF NOT EXISTS events_session_seq ON events(session_id, seq);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY, session_id TEXT, path TEXT, mime TEXT, size INTEGER);
        """)
        # A daemon restart never replays code or pretends old memory survived.
        stale = self.db.execute("SELECT id FROM sessions WHERE status NOT IN ('closed', 'lost')").fetchall()
        self.db.execute("UPDATE sessions SET status='lost' WHERE status != 'closed'")
        self.db.execute("UPDATE executions SET status='unknown',reason='daemon restarted' WHERE status IN ('running','interrupting')")
        self.db.execute("UPDATE executions SET status='cancelled',reason='daemon restarted before dispatch' WHERE status='queued'")
        self.db.commit()
        for row in stale:
            self.event(row['id'], None, 'session.lost', {'reason': 'daemon restarted; kernel memory is not restored'})

    def execute(self, sql, params=()):
        result = self.db.execute(sql, params)
        self.db.commit()
        return result

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def event(self, session_id, execution_id, kind, payload):
        cur = self.execute('INSERT INTO events(session_id,execution_id,kind,payload,created_at) VALUES(?,?,?,?,?)',
                           (session_id, execution_id, kind, json.dumps(payload, ensure_ascii=False), time.time()))
        return cur.lastrowid

    def events(self, session_id, after=0, limit=200):
        rows = self.all('SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?', (session_id, after, limit))
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return rows

    def close(self):
        self.db.close()
        self._lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(self._lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
        self._lock.close()
