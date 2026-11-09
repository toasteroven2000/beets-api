"""History of write operations, kept in the API's own SQLite file."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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


def _decode(row: sqlite3.Row) -> dict:
    op = dict(row)
    return op | {"request": json.loads(op["request"]), "result": json.loads(op["result"])}


class Store:
    def __init__(self, path: str):
        self.path = path
        self._execute(SCHEMA)

    def _execute(self, sql: str, *params) -> sqlite3.Cursor:
        with closing(sqlite3.connect(self.path)) as conn, conn:
            return conn.execute(sql, params)

    def create(self, kind: str, status: str, request: dict, result: dict) -> int:
        now = _now()
        return self._execute(
            "INSERT INTO operations (kind, status, request, result, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            kind, status, json.dumps(request), json.dumps(result), now, now,
        ).lastrowid

    def get(self, id_: int, kind: str) -> dict | None:
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM operations WHERE id = ? AND kind = ?", (id_, kind)
            ).fetchone()
        return None if row is None else _decode(row)

    def page(
        self, kind: str, statuses: list[str], limit: int, offset: int
    ) -> tuple[int, list[dict]]:
        """Newest first."""
        where, params = "kind = ?", [kind]
        if statuses:
            where += f" AND status IN ({','.join('?' * len(statuses))})"
            params += statuses
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            count_sql = f"SELECT count(*) FROM operations WHERE {where}"
            total = conn.execute(count_sql, params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM operations WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return total, [_decode(row) for row in rows]

    def update(self, id_: int, status: str, result: dict) -> None:
        self._execute(
            "UPDATE operations SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            status, json.dumps(result), _now(), id_,
        )

    def transition(self, id_: int, old: str, new: str) -> bool:
        """Change status only if it is still `old`, so a step can run only once."""
        return bool(self._execute(
            "UPDATE operations SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            new, _now(), id_, old,
        ).rowcount)

    def mark_interrupted(self, statuses: tuple[str, ...]) -> None:
        """Called at startup: nothing is still running in a new process."""
        marks = ",".join("?" * len(statuses))
        self._execute(
            f"UPDATE operations SET status = 'interrupted', updated_at = ?"
            f" WHERE status IN ({marks})",
            _now(), *statuses,
        )
