from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .models import PermissionDecision, PermissionMode


@dataclass(slots=True)
class PendingPermission:
    request_id: str
    session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    future: asyncio.Future[PermissionDecision]


class PermissionBroker:
    def __init__(self) -> None:
        self.pending: dict[str, PendingPermission] = {}

    async def request(
        self,
        *,
        session_id: str,
        mode: PermissionMode,
        tool_name: str,
        tool_input: dict[str, Any],
        dangerous: bool,
        mutating: bool,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> dict[str, Any]:
        if mode == PermissionMode.bypass:
            return tool_input
        if mode == PermissionMode.accept_edits and mutating and tool_name != "Bash":
            return tool_input
        if not dangerous and not mutating:
            return tool_input
        if mode == PermissionMode.plan:
            raise PermissionError(f"{tool_name} is blocked in plan mode")

        request_id = str(uuid4())
        future: asyncio.Future[PermissionDecision] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = PendingPermission(
            request_id=request_id,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            future=future,
        )
        await emit(
            "permission.requested",
            {"request_id": request_id, "tool": tool_name, "input": tool_input},
        )
        try:
            decision = await future
        finally:
            self.pending.pop(request_id, None)
        await emit(
            "permission.resolved",
            {"request_id": request_id, "behavior": decision.behavior},
        )
        if decision.behavior == "deny":
            raise PermissionError(decision.message or f"permission denied for {tool_name}")
        return decision.updated_input or tool_input

    def resolve(self, request_id: str, decision: PermissionDecision, session_id: str) -> None:
        pending = self.pending.get(request_id)
        if not pending or pending.session_id != session_id:
            raise KeyError("permission request not found")
        if not pending.future.done():
            pending.future.set_result(decision)

