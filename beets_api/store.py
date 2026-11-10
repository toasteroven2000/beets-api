"""The API's own SQLite file: operations in progress, and the audit.

`operations` holds the changing state of Modifications and Imports.
`audit` is append-only: one Audit entry per change made to the Library,
written as it happens. `art_archive` keeps every replaced or removed
cover image, so art changes are never destructive. Triggers refuse any
UPDATE or DELETE on either.
"""

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
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    client TEXT,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    operation_id INTEGER,
    changes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_target ON audit (target);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;

CREATE TABLE IF NOT EXISTS art_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archived_at TEXT NOT NULL,
    album_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    data BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS art_archive_no_update BEFORE UPDATE ON art_archive
BEGIN SELECT RAISE(ABORT, 'art_archive is append-only'); END;
CREATE TRIGGER IF NOT EXISTS art_archive_no_delete BEFORE DELETE ON art_archive
BEGIN SELECT RAISE(ABORT, 'art_archive is append-only'); END;
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(row: sqlite3.Row) -> dict:
    op = dict(row)
    return op | {"request": json.loads(op["request"]), "result": json.loads(op["result"])}


class Store:
    def __init__(self, path: str):
        self.path = path
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(SCHEMA)

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

    def audit(
        self,
        action: str,
        target: str,
        changes: dict,
        client: str | None,
        operation_id: int | None = None,
    ) -> dict:
        """Append an Audit entry, e.g. ("album.modify", "album:5", {...})."""
        entry = {"at": _now(), "client": client, "action": action, "target": target,
                 "operation_id": operation_id, "changes": changes}
        entry["id"] = self._execute(
            "INSERT INTO audit (at, client, action, target, operation_id, changes)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            *(entry[k] for k in ("at", "client", "action", "target", "operation_id")),
            json.dumps(changes),
        ).lastrowid
        return entry

    def archive_art(self, album_id: int, path: str, data: bytes) -> int:
        return self._execute(
            "INSERT INTO art_archive (archived_at, album_id, path, data) VALUES (?, ?, ?, ?)",
            _now(), album_id, path, data,
        ).lastrowid

    def audit_page(
        self,
        limit: int,
        offset: int,
        target: str | None = None,
        actions: list[str] = (),
        operation_id: int | None = None,
    ) -> tuple[int, list[dict]]:
        """Newest first."""
        where, params = ["1"], []
        if actions:
            where.append(f"action IN ({','.join('?' * len(actions))})")
            params += actions
        for column, value in (("target", target), ("operation_id", operation_id)):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        clause = " AND ".join(where)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute(f"SELECT count(*) FROM audit WHERE {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM audit WHERE {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return total, [dict(row) | {"changes": json.loads(row["changes"])} for row in rows]

    def audit_entries(
        self, operation_id: int | None = None, targets: list[str] = ()
    ) -> list[dict]:
        """Every matching entry, oldest first: by Import or Modification, or by target."""
        where, params = ["1"], []
        if operation_id is not None:
            where.append("operation_id = ?")
            params.append(operation_id)
        if targets:
            where.append(f"target IN ({','.join('?' * len(targets))})")
            params += targets
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM audit WHERE {' AND '.join(where)} ORDER BY id", params
            ).fetchall()
        return [dict(row) | {"changes": json.loads(row["changes"])} for row in rows]

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
