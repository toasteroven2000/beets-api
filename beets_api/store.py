"""History of write operations, kept in the API's own SQLite file."""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        self._execute(SCHEMA)

    def _execute(self, sql: str, *params) -> int:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(sql, params).rowcount

    def create(self, kind: str, status: str, request: dict, result: dict) -> str:
        id_ = str(uuid.uuid4())
        now = _now()
        self._execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?)",
            id_, kind, status, json.dumps(request), json.dumps(result), now, now,
        )
        return id_

    def get(self, id_: str, kind: str) -> dict | None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ? AND kind = ?", (id_, kind)
            ).fetchone()
        if row is None:
            return None
        op = dict(row)
        return op | {
            "request": json.loads(op["request"]),
            "result": json.loads(op["result"]),
        }

    def update(self, id_: str, status: str, result: dict) -> None:
        self._execute(
            "UPDATE operations SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            status, json.dumps(result), _now(), id_,
        )

    def transition(self, id_: str, old: str, new: str) -> bool:
        """Change status only if it is still `old`, so a step can run only once."""
        return bool(self._execute(
            "UPDATE operations SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            new, _now(), id_, old,
        ))

    def mark_interrupted(self, statuses: tuple[str, ...]) -> None:
        """Called at startup: nothing is still running in a new process."""
        marks = ",".join("?" * len(statuses))
        self._execute(
            f"UPDATE operations SET status = 'interrupted', updated_at = ?"
            f" WHERE status IN ({marks})",
            _now(), *statuses,
        )
