import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .contracts import PolicyError
from .security import canonical, clean


class Store:
    """Local control-plane ledger. SQLite transactions serialize budget/approval claims."""

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                  id TEXT PRIMARY KEY, tenant TEXT, goal TEXT, scenario TEXT, status TEXT,
                  max_calls INTEGER, max_units INTEGER, calls INTEGER DEFAULT 0,
                  units INTEGER DEFAULT 0, created REAL, updated REAL,
                  proposal_digest TEXT, approval_expires REAL, error TEXT);
                CREATE TABLE IF NOT EXISTS events (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, at REAL,
                  kind TEXT, agent TEXT, payload TEXT);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS cache (
                  key TEXT PRIMARY KEY, tenant TEXT, expires REAL, response TEXT);
                CREATE TABLE IF NOT EXISTS actions (
                  run_id TEXT PRIMARY KEY, tenant TEXT, proposal_digest TEXT, result TEXT);
            """)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, run_id, tenant, request):
        now = time.time()
        with self.db() as db:
            db.execute(
                "INSERT INTO runs(id,tenant,goal,scenario,status,max_calls,max_units,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    tenant,
                    request.goal,
                    request.scenario,
                    "running",
                    request.max_calls,
                    request.max_units,
                    now,
                    now,
                ),
            )

    def get(self, run_id, tenant):
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE id=? AND tenant=?", (run_id, tenant)
            ).fetchone()
        if row is None:
            raise PolicyError("run_not_found")
        return dict(row)

    def list(self, tenant):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM runs WHERE tenant=? ORDER BY created DESC LIMIT 50", (tenant,)
                )
            ]

    def update(self, run_id, status, error=None, proposal_digest=None):
        with self.db() as db:
            db.execute(
                "UPDATE runs SET status=?, error=?, updated=?, proposal_digest=COALESCE(?,proposal_digest), approval_expires=CASE WHEN ?='awaiting_approval' THEN ? ELSE approval_expires END WHERE id=?",
                (status, error, time.time(), proposal_digest, status, time.time() + 900, run_id),
            )

    def reserve(self, run_id, tenant, units):
        with self.db() as db:
            cursor = db.execute(
                "UPDATE runs SET calls=calls+1, units=units+? WHERE id=? AND tenant=? AND calls<max_calls AND units+?<=max_units",
                (units, run_id, tenant, units),
            )
            if cursor.rowcount != 1:
                raise PolicyError("budget_exhausted")

    def claim_approval(self, run_id, tenant, proposal_digest):
        with self.db() as db:
            cursor = db.execute(
                "UPDATE runs SET status='resuming', updated=? WHERE id=? AND tenant=? AND status='awaiting_approval' AND proposal_digest=? AND approval_expires>?",
                (time.time(), run_id, tenant, proposal_digest, time.time()),
            )
            if cursor.rowcount != 1:
                raise PolicyError("approval_conflict_or_expired")

    def event(self, run_id, kind, agent, payload):
        with self.db() as db:
            db.execute(
                "INSERT INTO events(run_id,at,kind,agent,payload) VALUES(?,?,?,?,?)",
                (run_id, time.time(), kind, agent, canonical(clean(payload))),
            )

    def events(self, run_id, tenant):
        self.get(run_id, tenant)
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def cached(self, key, tenant):
        with self.db() as db:
            row = db.execute(
                "SELECT response FROM cache WHERE key=? AND tenant=? AND expires>?",
                (key, tenant, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put_cache(self, key, tenant, response, ttl=300):
        with self.db() as db:
            db.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES(?,?,?,?)",
                (key, tenant, time.time() + ttl, canonical(response)),
            )
            db.execute(
                "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires DESC LIMIT -1 OFFSET 1000)"
            )

    def simulate(self, run_id, tenant, proposal_digest):
        # This local action is transactionally idempotent; it never deploys a real release.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT result FROM actions WHERE run_id=? AND tenant=?", (run_id, tenant)
            ).fetchone()
            if row:
                return json.loads(row[0])
            result = {
                "simulation": True,
                "baseline_p95_ms": 420,
                "canary_p95_ms": 280,
                "rollback_available": True,
            }
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,?)",
                (run_id, tenant, proposal_digest, canonical(result)),
            )
            return result
