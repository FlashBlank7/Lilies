from __future__ import annotations

import asyncio
import ipaddress
import json
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

import websockets
from pydantic import BaseModel, Field, field_validator

from .db import connect as _sqlite_connect
from .platform_harness import PlatformHarness


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("event time must include a timezone")
    return parsed.astimezone(timezone.utc)


def value_at_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(f"WebSocket message has no path: {path}")
    return current


def message_matches(message: dict[str, Any], expected: dict[str, Any]) -> bool:
    for path, value in expected.items():
        try:
            actual = value_at_path(message, path)
        except KeyError:
            return False
        if actual != value:
            return False
    return True


class EventSubscriptionCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120, pattern=r"^[A-Za-z0-9_.-]+$")
    application_id: str = Field(min_length=1, max_length=160)
    websocket_url: str = Field(min_length=8, max_length=2_000)
    allowed_hosts: list[str] = Field(min_length=1, max_length=20)
    greeting_match: dict[str, Any] = Field(default_factory=dict)
    authentication_message: dict[str, Any] = Field(default_factory=dict)
    authentication_response_match: dict[str, Any] = Field(default_factory=dict)
    subscription_message: dict[str, Any] = Field(min_length=1)
    subscription_response_match: dict[str, Any] = Field(default_factory=dict)
    event_match: dict[str, Any] = Field(default_factory=dict)
    event_identity_path: str = Field(min_length=1, max_length=500)
    input_mapping: dict[str, str] = Field(min_length=1, max_length=100)
    static_inputs: dict[str, Any] = Field(default_factory=dict)
    workspace_path: str = Field(min_length=1, max_length=2_000)
    reconnect_seconds: float = Field(default=1.0, ge=0.05, le=300)
    enabled: bool = True

    @field_validator("websocket_url")
    @classmethod
    def valid_websocket_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("event subscription requires a ws or wss URL")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return value
        if address.is_link_local or address.is_multicast or address.is_unspecified:
            raise ValueError(
                "event subscription rejects link-local, multicast, and unspecified addresses"
            )
        return value


class DurableEventTimerConfig(BaseModel):
    operation: Any
    timer_key: Any
    subject_id: Any
    event_id: Any
    occurred_at: Any
    hold_for_seconds: Any = 300
    due_inputs: Any = Field(default_factory=dict)


class DurableEventTimerRequest(BaseModel):
    operation: Literal["schedule", "cancel", "complete"]
    timer_key: str = Field(min_length=1, max_length=240)
    subject_id: str = Field(min_length=1, max_length=240)
    event_id: str = Field(min_length=1, max_length=240)
    occurred_at: str
    hold_for_seconds: float = Field(default=300, ge=0.05, le=86_400)
    due_inputs: dict[str, Any] = Field(default_factory=dict)


RunCallback = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]


class EventAutomationService:
    """Host-neutral WebSocket subscriptions and restart-safe per-event timers."""

    def __init__(
        self,
        database_path: Path,
        *,
        harness: PlatformHarness,
        timer_poll_seconds: float = 0.1,
    ) -> None:
        self.database_path = database_path
        self.harness = harness
        self.timer_poll_seconds = timer_poll_seconds
        self._run_callback: RunCallback | None = None
        self._subscriptions: dict[str, asyncio.Task[None]] = {}
        self._timer_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def bind_run_callback(self, callback: RunCallback) -> None:
        self._run_callback = callback

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with _sqlite_connect(self.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_subscriptions (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL UNIQUE,
                  application_id TEXT NOT NULL,
                  config_json TEXT NOT NULL,
                  enabled INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  reconnect_count INTEGER NOT NULL DEFAULT 0,
                  event_count INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_receipts (
                  subscription_id TEXT NOT NULL,
                  event_identity TEXT NOT NULL,
                  run_id TEXT,
                  received_at TEXT NOT NULL,
                  PRIMARY KEY(subscription_id, event_identity)
                );
                CREATE TABLE IF NOT EXISTS event_timers (
                  timer_key TEXT PRIMARY KEY,
                  application_id TEXT NOT NULL,
                  subject_id TEXT NOT NULL,
                  source_event_id TEXT NOT NULL,
                  source_occurred_at TEXT NOT NULL,
                  due_at TEXT NOT NULL,
                  due_inputs_json TEXT NOT NULL,
                  workspace_path TEXT NOT NULL,
                  status TEXT NOT NULL,
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  recovery_count INTEGER NOT NULL DEFAULT 0,
                  run_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_timer_history (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timer_key TEXT NOT NULL,
                  event_id TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  status TEXT NOT NULL,
                  detail_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_event_timers_due
                  ON event_timers(status,due_at);
                """
            )
            timer_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(event_timers)"
                ).fetchall()
            }
            if "recovery_count" not in timer_columns:
                connection.execute(
                    "ALTER TABLE event_timers ADD COLUMN recovery_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "UPDATE event_timers SET recovery_count=recovery_count+1,"
                "status=CASE WHEN status='dispatching' THEN 'pending' ELSE status END,"
                "updated_at=? WHERE status IN ('pending','dispatching')",
                (utc_now(),),
            )

    async def start(self) -> None:
        if self._run_callback is None:
            raise RuntimeError("event automation run callback is not bound")
        self._stopping.clear()
        self._timer_task = asyncio.create_task(
            self._timer_loop(),
            name="event-automation-timer-loop",
        )
        for subscription in await self.list_subscriptions():
            if subscription["enabled"]:
                self._start_subscription_task(str(subscription["id"]))

    async def stop(self) -> None:
        self._stopping.set()
        tasks = list(self._subscriptions.values())
        if self._timer_task is not None:
            tasks.append(self._timer_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._subscriptions.clear()
        self._timer_task = None

    async def create_subscription(
        self,
        request: EventSubscriptionCreateRequest,
    ) -> dict[str, Any]:
        host = urlparse(request.websocket_url).hostname
        if host is None or host.casefold() not in {
            item.casefold() for item in request.allowed_hosts
        }:
            raise ValueError("WebSocket host is outside allowed_hosts")
        self.harness.enforce_network_egress_policy(
            surface="event_subscription",
            hostname=host,
        )
        subscription_id = str(uuid4())
        created = await asyncio.to_thread(
            self._create_subscription_sync,
            subscription_id,
            request,
        )
        if request.enabled and self._timer_task is not None:
            self._start_subscription_task(subscription_id)
        return created

    def _create_subscription_sync(
        self,
        subscription_id: str,
        request: EventSubscriptionCreateRequest,
    ) -> dict[str, Any]:
        now = utc_now()
        with _sqlite_connect(self.database_path) as connection:
            try:
                connection.execute(
                    "INSERT INTO event_subscriptions "
                    "(id,name,application_id,config_json,enabled,status,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        subscription_id,
                        request.name,
                        request.application_id,
                        request.model_dump_json(),
                        int(request.enabled),
                        "starting" if request.enabled else "disabled",
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    f"event subscription already exists: {request.name}"
                ) from error
        return self._get_subscription_sync(subscription_id)

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_subscription_sync,
            subscription_id,
        )

    def _get_subscription_sync(self, subscription_id: str) -> dict[str, Any]:
        with _sqlite_connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM event_subscriptions WHERE id=?",
                (subscription_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"event subscription not found: {subscription_id}")
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["config"] = json.loads(result.pop("config_json"))
        result["last_error"] = result["last_error"] or None
        return result

    async def list_subscriptions(
        self,
        *,
        application_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_subscriptions_sync,
            application_id,
        )

    def _list_subscriptions_sync(
        self,
        application_id: str | None,
    ) -> list[dict[str, Any]]:
        query = "SELECT id FROM event_subscriptions"
        parameters: tuple[Any, ...] = ()
        if application_id:
            query += " WHERE application_id=?"
            parameters = (application_id,)
        query += " ORDER BY created_at"
        with _sqlite_connect(self.database_path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._get_subscription_sync(str(row[0])) for row in rows]

    async def set_subscription_enabled(
        self,
        subscription_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        await asyncio.to_thread(
            self._set_subscription_enabled_sync,
            subscription_id,
            enabled,
        )
        task = self._subscriptions.pop(subscription_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if enabled and self._timer_task is not None:
            self._start_subscription_task(subscription_id)
        return await self.get_subscription(subscription_id)

    def _set_subscription_enabled_sync(
        self,
        subscription_id: str,
        enabled: bool,
    ) -> None:
        with _sqlite_connect(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE event_subscriptions SET enabled=?,status=?,last_error=NULL,"
                "updated_at=? WHERE id=?",
                (
                    int(enabled),
                    "starting" if enabled else "disabled",
                    utc_now(),
                    subscription_id,
                ),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"event subscription not found: {subscription_id}")

    def _start_subscription_task(self, subscription_id: str) -> None:
        current = self._subscriptions.get(subscription_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            self._subscription_loop(subscription_id),
            name=f"event-subscription:{subscription_id}",
        )
        self._subscriptions[subscription_id] = task

    async def _subscription_loop(self, subscription_id: str) -> None:
        while not self._stopping.is_set():
            subscription = await self.get_subscription(subscription_id)
            if not subscription["enabled"]:
                return
            config = EventSubscriptionCreateRequest.model_validate(
                subscription["config"]
            )
            try:
                await self._consume_subscription(subscription_id, config)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await asyncio.to_thread(
                    self._record_subscription_failure_sync,
                    subscription_id,
                    type(error).__name__,
                )
                await asyncio.sleep(config.reconnect_seconds)
            else:
                await asyncio.sleep(config.reconnect_seconds)

    async def _consume_subscription(
        self,
        subscription_id: str,
        config: EventSubscriptionCreateRequest,
    ) -> None:
        auth_message = await self.harness.inject_secret_references(
            owner_id=config.application_id,
            payload=config.authentication_message,
            allow_secret_references=True,
        )
        async with websockets.connect(
            config.websocket_url,
            open_timeout=30,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            if config.greeting_match:
                greeting = json.loads(await socket.recv())
                if not message_matches(greeting, config.greeting_match):
                    raise RuntimeError("WebSocket greeting did not match")
            if auth_message:
                await socket.send(json.dumps(auth_message, separators=(",", ":")))
            if config.authentication_response_match:
                authenticated = json.loads(await socket.recv())
                if not message_matches(
                    authenticated,
                    config.authentication_response_match,
                ):
                    raise PermissionError("WebSocket authentication was rejected")
            await socket.send(
                json.dumps(config.subscription_message, separators=(",", ":"))
            )
            if config.subscription_response_match:
                acknowledged = json.loads(await socket.recv())
                if not message_matches(
                    acknowledged,
                    config.subscription_response_match,
                ):
                    raise RuntimeError("WebSocket subscription was rejected")
            await asyncio.to_thread(
                self._set_subscription_connected_sync,
                subscription_id,
            )
            async for raw_message in socket:
                message = json.loads(raw_message)
                if not isinstance(message, dict) or not message_matches(
                    message,
                    config.event_match,
                ):
                    continue
                event_identity = str(
                    value_at_path(message, config.event_identity_path)
                )
                inputs = dict(config.static_inputs)
                for name, path in config.input_mapping.items():
                    inputs[name] = value_at_path(message, path)
                await self._dispatch_subscription_event(
                    subscription_id,
                    config,
                    event_identity,
                    inputs,
                )

    async def _dispatch_subscription_event(
        self,
        subscription_id: str,
        config: EventSubscriptionCreateRequest,
        event_identity: str,
        inputs: dict[str, Any],
    ) -> None:
        inserted = await asyncio.to_thread(
            self._reserve_event_receipt_sync,
            subscription_id,
            event_identity,
        )
        if not inserted:
            return
        if self._run_callback is None:
            raise RuntimeError("event automation run callback is not bound")
        try:
            run = await self._run_callback(
                config.application_id,
                {
                    **inputs,
                    "__event_automation": {
                        "subscription_id": subscription_id,
                        "event_identity": event_identity,
                    },
                },
                config.workspace_path,
            )
        except Exception:
            await asyncio.to_thread(
                self._release_event_receipt_sync,
                subscription_id,
                event_identity,
            )
            raise
        await asyncio.to_thread(
            self._complete_event_receipt_sync,
            subscription_id,
            event_identity,
            str(run["run_id"]),
        )

    def _reserve_event_receipt_sync(
        self,
        subscription_id: str,
        event_identity: str,
    ) -> bool:
        with _sqlite_connect(self.database_path) as connection:
            before = connection.total_changes
            connection.execute(
                "INSERT OR IGNORE INTO event_receipts "
                "(subscription_id,event_identity,received_at) VALUES (?,?,?)",
                (subscription_id, event_identity, utc_now()),
            )
            return connection.total_changes > before

    def _release_event_receipt_sync(
        self,
        subscription_id: str,
        event_identity: str,
    ) -> None:
        with _sqlite_connect(self.database_path) as connection:
            connection.execute(
                "DELETE FROM event_receipts WHERE subscription_id=? "
                "AND event_identity=? AND run_id IS NULL",
                (subscription_id, event_identity),
            )

    def _complete_event_receipt_sync(
        self,
        subscription_id: str,
        event_identity: str,
        run_id: str,
    ) -> None:
        with _sqlite_connect(self.database_path) as connection:
            connection.execute(
                "UPDATE event_receipts SET run_id=? WHERE subscription_id=? "
                "AND event_identity=?",
                (run_id, subscription_id, event_identity),
            )
            connection.execute(
                "UPDATE event_subscriptions SET event_count=event_count+1,"
                "updated_at=? WHERE id=?",
                (utc_now(), subscription_id),
            )

    def _set_subscription_connected_sync(self, subscription_id: str) -> None:
        with _sqlite_connect(self.database_path) as connection:
            connection.execute(
                "UPDATE event_subscriptions SET status='connected',last_error=NULL,"
                "updated_at=? WHERE id=?",
                (utc_now(), subscription_id),
            )

    def _record_subscription_failure_sync(
        self,
        subscription_id: str,
        error_code: str,
    ) -> None:
        with _sqlite_connect(self.database_path) as connection:
            connection.execute(
                "UPDATE event_subscriptions SET status='reconnecting',"
                "reconnect_count=reconnect_count+1,last_error=?,updated_at=? "
                "WHERE id=?",
                (error_code[:120], utc_now(), subscription_id),
            )

    async def apply_timer(
        self,
        application_id: str,
        workspace_path: str,
        request: DurableEventTimerRequest,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._apply_timer_sync,
            application_id,
            workspace_path,
            request,
        )

    def _apply_timer_sync(
        self,
        application_id: str,
        workspace_path: str,
        request: DurableEventTimerRequest,
    ) -> dict[str, Any]:
        occurred_at = parse_time(request.occurred_at)
        now = utc_now()
        with _sqlite_connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            existing = connection.execute(
                "SELECT * FROM event_timers WHERE timer_key=?",
                (request.timer_key,),
            ).fetchone()
            replayed = bool(
                existing is not None
                and str(existing["source_event_id"]) == request.event_id
            )
            stale = bool(
                existing is not None
                and parse_time(str(existing["source_occurred_at"])) > occurred_at
            )
            if replayed:
                result_status = "replayed"
            elif stale:
                result_status = "stale_ignored"
            elif request.operation == "schedule":
                due_at = occurred_at + timedelta(
                    seconds=request.hold_for_seconds
                )
                connection.execute(
                    "INSERT INTO event_timers "
                    "(timer_key,application_id,subject_id,source_event_id,"
                    "source_occurred_at,due_at,due_inputs_json,workspace_path,status,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(timer_key) DO UPDATE SET "
                    "application_id=excluded.application_id,"
                    "subject_id=excluded.subject_id,"
                    "source_event_id=excluded.source_event_id,"
                    "source_occurred_at=excluded.source_occurred_at,"
                    "due_at=excluded.due_at,"
                    "due_inputs_json=excluded.due_inputs_json,"
                    "workspace_path=excluded.workspace_path,status='pending',"
                    "attempt_count=0,recovery_count=0,run_id=NULL,"
                    "updated_at=excluded.updated_at",
                    (
                        request.timer_key,
                        application_id,
                        request.subject_id,
                        request.event_id,
                        occurred_at.isoformat(),
                        due_at.isoformat(),
                        json.dumps(
                            request.due_inputs,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        workspace_path,
                        "pending",
                        now,
                        now,
                    ),
                )
                result_status = "scheduled"
            elif request.operation == "cancel":
                if existing is None:
                    result_status = "no_pending_timer"
                else:
                    connection.execute(
                        "UPDATE event_timers SET source_event_id=?,"
                        "source_occurred_at=?,status='cancelled',updated_at=? "
                        "WHERE timer_key=?",
                        (
                            request.event_id,
                            occurred_at.isoformat(),
                            now,
                            request.timer_key,
                        ),
                    )
                    result_status = "cancelled"
            else:
                if existing is None:
                    result_status = "no_timer"
                else:
                    connection.execute(
                        "UPDATE event_timers SET status='completed',updated_at=? "
                        "WHERE timer_key=?",
                        (now, request.timer_key),
                    )
                    result_status = "completed"
            row = connection.execute(
                "SELECT * FROM event_timers WHERE timer_key=?",
                (request.timer_key,),
            ).fetchone()
            detail = {
                "replayed": replayed,
                "stale": stale,
                "result_status": result_status,
            }
            connection.execute(
                "INSERT INTO event_timer_history "
                "(timer_key,event_id,operation,status,detail_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    request.timer_key,
                    request.event_id,
                    request.operation,
                    result_status,
                    json.dumps(detail, separators=(",", ":")),
                    now,
                ),
            )
        return {
            "timer_key": request.timer_key,
            "subject_id": request.subject_id,
            "event_id": request.event_id,
            "operation": request.operation,
            "status": result_status,
            "replayed": replayed,
            "stale_ignored": stale,
            "due_at": str(row["due_at"]) if row is not None else None,
            "durable": True,
        }

    async def get_timer(self, timer_key: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_timer_sync, timer_key)

    def _get_timer_sync(self, timer_key: str) -> dict[str, Any]:
        with _sqlite_connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM event_timers WHERE timer_key=?",
                (timer_key,),
            ).fetchone()
            history_rows = connection.execute(
                "SELECT event_id,operation,status,detail_json,created_at "
                "FROM event_timer_history WHERE timer_key=? ORDER BY id",
                (timer_key,),
            ).fetchall()
        if row is None:
            raise KeyError(f"event timer not found: {timer_key}")
        result = dict(row)
        result["due_inputs"] = json.loads(result.pop("due_inputs_json"))
        result["history"] = [
            {
                "event_id": item["event_id"],
                "operation": item["operation"],
                "status": item["status"],
                "detail": json.loads(item["detail_json"]),
                "created_at": item["created_at"],
            }
            for item in history_rows
        ]
        return result

    async def list_timers(
        self,
        *,
        application_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_timers_sync,
            application_id,
            status,
        )

    def _list_timers_sync(
        self,
        application_id: str | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if application_id:
            clauses.append("application_id=?")
            parameters.append(application_id)
        if status:
            clauses.append("status=?")
            parameters.append(status)
        query = "SELECT timer_key FROM event_timers"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with _sqlite_connect(self.database_path) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._get_timer_sync(str(row[0])) for row in rows]

    async def _timer_loop(self) -> None:
        while not self._stopping.is_set():
            due = await asyncio.to_thread(self._claim_due_timers_sync)
            for timer in due:
                await self._dispatch_timer(timer)
            await asyncio.sleep(self.timer_poll_seconds)

    def _claim_due_timers_sync(self) -> list[dict[str, Any]]:
        now = utc_now()
        with _sqlite_connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM event_timers WHERE status='pending' AND due_at<=? "
                "ORDER BY due_at LIMIT 20",
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE event_timers SET status='dispatching',"
                    "attempt_count=attempt_count+1,updated_at=? "
                    "WHERE timer_key=? AND status='pending'",
                    (now, row["timer_key"]),
                )
            connection.commit()
        return [dict(row) for row in rows]

    async def _dispatch_timer(self, timer: dict[str, Any]) -> None:
        if self._run_callback is None:
            return
        inputs = json.loads(str(timer["due_inputs_json"]))
        inputs["__event_automation"] = {
            "timer_key": timer["timer_key"],
            "source_event_id": timer["source_event_id"],
            "due_at": timer["due_at"],
            "recovered_after_restart": int(timer["recovery_count"]) > 0,
            "recovery_count": int(timer["recovery_count"]),
        }
        try:
            run = await self._run_callback(
                str(timer["application_id"]),
                inputs,
                str(timer["workspace_path"]),
            )
        except Exception as error:
            await asyncio.to_thread(
                self._record_timer_dispatch_failure_sync,
                str(timer["timer_key"]),
                type(error).__name__,
            )
            return
        await asyncio.to_thread(
            self._record_timer_dispatched_sync,
            str(timer["timer_key"]),
            str(run["run_id"]),
        )

    def _record_timer_dispatch_failure_sync(
        self,
        timer_key: str,
        error_code: str,
    ) -> None:
        now = utc_now()
        with _sqlite_connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT attempt_count FROM event_timers WHERE timer_key=?",
                (timer_key,),
            ).fetchone()
            attempts = int(row[0]) if row else 3
            next_status = "failed" if attempts >= 3 else "pending"
            connection.execute(
                "UPDATE event_timers SET status=?,updated_at=? WHERE timer_key=?",
                (next_status, now, timer_key),
            )
            connection.execute(
                "INSERT INTO event_timer_history "
                "(timer_key,event_id,operation,status,detail_json,created_at) "
                "SELECT timer_key,source_event_id,'dispatch',?, ?, ? "
                "FROM event_timers WHERE timer_key=?",
                (
                    next_status,
                    json.dumps({"error_code": error_code}),
                    now,
                    timer_key,
                ),
            )

    def _record_timer_dispatched_sync(
        self,
        timer_key: str,
        run_id: str,
    ) -> None:
        now = utc_now()
        with _sqlite_connect(self.database_path) as connection:
            connection.execute(
                "UPDATE event_timers SET status='dispatched',run_id=?,updated_at=? "
                "WHERE timer_key=?",
                (run_id, now, timer_key),
            )
            connection.execute(
                "INSERT INTO event_timer_history "
                "(timer_key,event_id,operation,status,detail_json,created_at) "
                "SELECT timer_key,source_event_id,'dispatch','dispatched',?,? "
                "FROM event_timers WHERE timer_key=?",
                (
                    json.dumps({"run_id": run_id}),
                    now,
                    timer_key,
                ),
            )
