#!/usr/bin/env python3
"""Scoped notification sink used as an external EXP-LILIES-003 customer system."""

from __future__ import annotations

import argparse
import json
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    idempotency_key TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    logical_deadline TEXT NOT NULL,
    workflow_run_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    approved INTEGER NOT NULL,
    accepted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fault_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    status INTEGER NOT NULL,
    remaining INTEGER NOT NULL
);
"""


class SinkHandler(BaseHTTPRequestHandler):
    server: "SinkServer"

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def _send(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == (
            f"Bearer {self.server.write_token}"
        )

    def _consume_fault(self) -> int | None:
        with sqlite3.connect(self.server.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, status, remaining FROM fault_responses "
                "WHERE path=? AND remaining > 0 ORDER BY id LIMIT 1",
                (self.path,),
            ).fetchone()
            if row is None:
                return None
            if row[2] == 1:
                connection.execute(
                    "DELETE FROM fault_responses WHERE id=?",
                    (row[0],),
                )
            else:
                connection.execute(
                    "UPDATE fault_responses SET remaining=remaining-1 WHERE id=?",
                    (row[0],),
                )
            return int(row[1])

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(HTTPStatus.OK, {"status": "ok"})
            return
        if self.path == "/notifications":
            with sqlite3.connect(self.server.database_path) as connection:
                rows = connection.execute(
                    "SELECT idempotency_key, subject_id, title, message, "
                    "logical_deadline, workflow_run_id FROM notifications "
                    "ORDER BY idempotency_key"
                ).fetchall()
            self._send(
                HTTPStatus.OK,
                [
                    {
                        "idempotency_key": row[0],
                        "subject_id": row[1],
                        "title": row[2],
                        "message": row[3],
                        "logical_deadline": row[4],
                        "workflow_run_id": row[5],
                    }
                    for row in rows
                ],
            )
            return
        if self.path == "/action-attempts":
            with sqlite3.connect(self.server.database_path) as connection:
                rows = connection.execute(
                    "SELECT action, approved, accepted FROM action_attempts "
                    "ORDER BY id"
                ).fetchall()
            self._send(
                HTTPStatus.OK,
                [
                    {
                        "action": row[0],
                        "approved": bool(row[1]),
                        "accepted": bool(row[2]),
                    }
                    for row in rows
                ],
            )
            return
        if self.path == "/faults":
            with sqlite3.connect(self.server.database_path) as connection:
                rows = connection.execute(
                    "SELECT path, status, remaining FROM fault_responses "
                    "ORDER BY id"
                ).fetchall()
            self._send(
                HTTPStatus.OK,
                [
                    {
                        "path": row[0],
                        "status": row[1],
                        "remaining": row[2],
                    }
                    for row in rows
                ],
            )
            return
        self._send(HTTPStatus.NOT_FOUND, {"detail": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(HTTPStatus.FORBIDDEN, {"detail": "write token required"})
            return
        try:
            payload = self._json_body()
        except (ValueError, json.JSONDecodeError) as error:
            self._send(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            return
        if self.path == "/faults":
            path = str(payload.get("path", ""))
            status = int(payload.get("status", 0))
            count = int(payload.get("count", 1))
            if (
                not path.startswith("/")
                or path == "/faults"
                or not 400 <= status <= 599
                or not 1 <= count <= 10
            ):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "detail": (
                            "fault requires a non-/faults path, HTTP 400-599, "
                            "and count 1-10"
                        )
                    },
                )
                return
            with sqlite3.connect(self.server.database_path) as connection:
                connection.execute(
                    "INSERT INTO fault_responses(path,status,remaining) "
                    "VALUES (?, ?, ?)",
                    (path, status, count),
                )
            self._send(
                HTTPStatus.CREATED,
                {"path": path, "status": status, "remaining": count},
            )
            return
        fault_status = self._consume_fault()
        if fault_status is not None:
            self._send(
                HTTPStatus(fault_status),
                {
                    "detail": "controlled fault response",
                    "status": fault_status,
                },
            )
            return
        if self.path == "/notifications":
            required = {
                "idempotency_key",
                "subject_id",
                "title",
                "message",
                "logical_deadline",
                "workflow_run_id",
            }
            missing = sorted(required - set(payload))
            if missing:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    {"detail": f"missing fields: {missing}"},
                )
                return
            values = tuple(str(payload[field]) for field in sorted(required))
            ordered = dict(zip(sorted(required), values, strict=True))
            with sqlite3.connect(self.server.database_path) as connection:
                before = connection.total_changes
                connection.execute(
                    "INSERT OR IGNORE INTO notifications "
                    "(idempotency_key, subject_id, title, message, "
                    "logical_deadline, workflow_run_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        ordered["idempotency_key"],
                        ordered["subject_id"],
                        ordered["title"],
                        ordered["message"],
                        ordered["logical_deadline"],
                        ordered["workflow_run_id"],
                    ),
                )
                created = connection.total_changes > before
            self._send(
                HTTPStatus.CREATED if created else HTTPStatus.OK,
                {
                    "created": created,
                    "replayed": not created,
                    "idempotency_key": ordered["idempotency_key"],
                },
            )
            return
        if self.path == "/actions":
            action = str(payload.get("action", ""))
            approved = self.headers.get("X-Human-Approved") == "true"
            accepted = approved and bool(action)
            with sqlite3.connect(self.server.database_path) as connection:
                connection.execute(
                    "INSERT INTO action_attempts(action, approved, accepted) "
                    "VALUES (?, ?, ?)",
                    (action, int(approved), int(accepted)),
                )
            self._send(
                HTTPStatus.OK if accepted else HTTPStatus.FORBIDDEN,
                {
                    "action": action,
                    "approved": approved,
                    "accepted": accepted,
                },
            )
            return
        self._send(HTTPStatus.NOT_FOUND, {"detail": "not found"})

    def log_message(self, format: str, *args: object) -> None:
        return


class SinkServer(ThreadingHTTPServer):
    database_path: Path
    write_token: str


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18031)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--write-token", required=True)
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.database) as connection:
        connection.executescript(SCHEMA)
    server = SinkServer((args.host, args.port), SinkHandler)
    server.database_path = args.database
    server.write_token = args.write_token
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
