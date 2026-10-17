"""Local Codex adapter. No provider calls, terminal scraping, or shared agent state."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from .sandbox import SandboxCleanupError

TOOL_RESPONSE_TIMEOUT_SECONDS = 30


class CodexError(RuntimeError):
    pass


async def inspect_executable(path: str) -> dict[str, Any]:
    executable = shutil.which(path) if not Path(path).is_absolute() else path
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ValueError("没有找到可执行程序，请指定本机 Agent 的完整路径")
    creating = asyncio.create_task(asyncio.create_subprocess_exec(
        executable, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    try:
        process = await asyncio.shield(creating)
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 10)
        except TimeoutError:
            raise ValueError("读取 Agent 版本超时") from None
    finally:
        async def cleanup():
            try:
                child = await creating
            except Exception:
                return  # Startup failure is already reported by the caller.
            if child.returncode is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            await child.communicate()

        # Cancellation can arrive before process creation returns its handle.
        # Keep ownership until both the child and its pipes have closed, even
        # if stop and shutdown cancel the same inspection more than once.
        cleaning = asyncio.create_task(cleanup())
        cancelled = False
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                cancelled = True
        cleaning.result()
        if cancelled:
            raise asyncio.CancelledError
    version = out.decode(errors="replace").strip()[:200]
    if process.returncode or not version:
        raise ValueError("无法读取 Agent 版本")
    return {"path": str(Path(executable).absolute()), "version": version}


async def discover_agents() -> list[dict[str, Any]]:
    result = []
    for name, command in (("codex", "codex"), ("claude", "claude"), ("kimi", "kimi")):
        path = shutil.which(command)
        if path:
            try:
                result.append({"id": name, "detected": True, "supported": True,
                               **await inspect_executable(path)})
            except ValueError as error:
                result.append({"id": name, "detected": False, "supported": False, "error": str(error)})
        else:
            result.append({"id": name, "detected": False, "supported": True})
    return result


class CodexAppServer:
    """One process and thread per project; all business I/O uses host-owned tools.

    Empty environments disables native filesystem/shell access. A private Codex
    home prevents loading personal sessions, plugins, memories and instructions.
    Only the installed CLI's existing login is linked, never read by the model.
    """

    def __init__(self, executable: str, runtime_dir: Path, *, model: str = "", thinking: str = "medium",
                 auth_file: Path | None = None, subscription_only: bool = False, allow_model_calls: bool = True,
                 auto_compact_token_limit: int | None = None) -> None:
        self.allow_model_calls = allow_model_calls
        self.auth_file = auth_file
        self.subscription_only = subscription_only
        self.auto_compact_token_limit = auto_compact_token_limit
        self.executable = executable
        self.runtime_dir = runtime_dir
        self.model = model
        self.thinking = thinking
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self.reader: asyncio.Task | None = None
        self.stderr_reader: asyncio.Task | None = None
        self.requests: dict[int | str, tuple[dict, asyncio.Task]] = {}
        self.cleanup_error = ''
        self.transport_error = ''
        self.closing = False
        self.accepting_tools = False
        self.seen_threads: set[str] = set()
        self.ended_turns: set[tuple[str, str]] = set()
        self.child_revision = 0
        self.child_changed = asyncio.Event()
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.thread_parents: dict[str, str | None] = {}
        self.turn_start_request_id: int | None = None
        self.early_turn_events: list[tuple[str, dict]] = []
        self.usage_totals: dict[str, int] = {}
        self.usage_turns: set[tuple[str, str]] = set()
        self.usage_tail: asyncio.Task | None = None
        self.steering: set[asyncio.Future] = set()
        self.finished: asyncio.Future | None = None
        self.on_event: Callable[[str, dict], Awaitable[None]] | None = None
        self.on_tool: Callable[[str, dict], Awaitable[Any]] | None = None
        self._start_context: tuple[list[dict], str] | None = None
        self.last_activity = 0.0

    async def connect(self) -> None:
        """Initialize transport without creating a thread or spending model tokens."""
        if self.process and self.process.returncode is None:
            if not self.closing:
                return
            await self.close()
        self.closing = False
        self.usage_totals.clear()
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        codex_home = self.runtime_dir / "codex-home"
        codex_home.mkdir(exist_ok=True, mode=0o700)
        login = self.auth_file or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        target = codex_home / "auth.json"
        if self.subscription_only and login.is_file() and target.exists() and not target.is_symlink() and login.absolute() != target.absolute():
            target.unlink()
        if login.is_file() and target.is_symlink() and target.resolve() != login.resolve():
            # A restored project may still point to the previous machine's login.
            target.unlink()
        if login.is_file() and not target.exists() and login.absolute() != target.absolute():
            target.symlink_to(login.resolve())
        cwd = self.runtime_dir / "empty-workspace"
        cwd.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k in {
            "HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "SYSTEMROOT",
            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE",
            "https_proxy", "http_proxy", "all_proxy", "no_proxy",
        }}
        env["CODEX_HOME"] = str(codex_home.resolve())
        config = self.config = {
            "web_search": "disabled", "project_doc_max_bytes": 0,
            "features.apps": False, "features.plugins": False,
            "features.remote_plugin": False, "features.multi_agent": False,
            "features.memories": False, "features.shell_tool": False,
            "features.unified_exec": False, "tools.view_image": False,
            "features.code_mode.enabled": False,
            "check_for_update_on_startup": False,
            **({"forced_login_method": "chatgpt", "cli_auth_credentials_store": "file"}
               if self.subscription_only else {}),
        }
        if self.auto_compact_token_limit is not None:
            config['model_auto_compact_token_limit'] = self.auto_compact_token_limit
        argv = [self.executable, "app-server", "--stdio"]
        for key, value in config.items():
            argv += ["-c", f"{key}={json.dumps(value)}"]
        creating = asyncio.create_task(asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024,
        ))
        try:
            self.process = await asyncio.shield(creating)
            self.reader = asyncio.create_task(self._read())
            self.stderr_reader = asyncio.create_task(self._drain_stderr())
            await self.request("initialize", {
                "clientInfo": {"name": "lilies", "title": "Lilies Workflow Studio", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            })
            await self._send({"method": "initialized"})
        except BaseException:
            async def cleanup():
                try:
                    self.process = await creating
                except Exception:
                    return
                await self.close()

            # A cancelled spawn may already own a live process. Obtain its
            # handle and finish cleanup before the owner can publish a stop.
            cleaning = asyncio.create_task(cleanup())
            while not cleaning.done():
                try:
                    await asyncio.shield(cleaning)
                except asyncio.CancelledError:
                    pass
            cleaning.result()
            raise

    async def start(self, tools: list[dict], instructions: str, thread_id: str | None = None) -> str:
        await self.connect()
        self.thread_parents.clear()
        if self.subscription_only:
            account = (await self.request('account/read', {'refreshToken': False})).get('account')
            if not account or account.get('type') != 'chatgpt':
                raise CodexError('官方智能体需要订阅账号登录，不能使用 API Key')
        cwd = self.runtime_dir / 'empty-workspace'
        codex_home = self.runtime_dir / 'codex-home'
        config = self.config
        delegation = (
            'For native subagents working in this Lilies project, use fork_turns="all" '
            '(or omit fork_turns), and omit model and reasoning_effort overrides. '
            'They inherit the current model, reasoning effort and project-scoped dynamic tools. '
            'Do not use fork_turns="none" for project work: that fresh context does not inherit '
            'the host project tools. Delegate through the inherited project tools. '
            'Wait for delegated work and collect its results before your final delivery.'
        ) if tools else ''
        params = {"cwd": str(cwd.resolve()), "approvalPolicy": "never", "sandbox": "read-only",
                  "baseInstructions": instructions, "developerInstructions": delegation,
                  "modelProvider": "openai", "config": config}
        if self.model:
            params["model"] = self.model
        if thread_id:
            # Session events are already stored by Lilies. Rehydrating all Codex
            # turns makes a long building session exceed the stdio frame limit.
            resume = {**params, "threadId": thread_id, "excludeTurns": True}
            sessions = codex_home / 'sessions'
            paths = [p.resolve() for p in sessions.rglob('rollout-*.jsonl')
                     if p.name.endswith('-' + thread_id + '.jsonl')
                     and not p.is_symlink() and p.resolve().is_relative_to(sessions.resolve())]
            if len(paths) > 1:
                raise CodexError('原会话有多个保存文件，请检查恢复目录后继续')
            if paths:
                # The CLI state DB may retain an old absolute rollout path after restore.
                # Use the supported experimental resume path, not private DB mutations.
                resume['path'] = str(paths[0])
            result = await self.request("thread/resume", resume)
        else:
            # The app-server requires deferred tools to belong to a namespace.
            # Keep the application tool names/validation unchanged for API sessions.
            deferred = [tool for tool in tools if tool.get('deferLoading')]
            dynamic_tools = [tool for tool in tools if not tool.get('deferLoading')]
            if deferred:
                dynamic_tools.append({'type': 'namespace', 'name': 'lilies',
                    # This prefix is repeated in every searchable tool description.
                    # Listing all capabilities here makes unrelated tools match.
                    'description': 'Additional project capabilities. Discover a tool by its name or specific purpose.',
                    'tools': deferred})
            result = await self.request("thread/start", {
                **params, "environments": [], "selectedCapabilityRoots": [],
                "dynamicTools": dynamic_tools, "allowProviderModelFallback": False,
            })
        if result.get("instructionSources"):
            raise CodexError("Codex 加载了项目外指令，已停止接入")
        self.thread_id = result["thread"]["id"]
        self._start_context = (tools, instructions)
        return self.thread_id

    async def _send(self, message: dict) -> None:
        if not self.process or self.process.returncode is not None or not self.process.stdin:
            raise CodexError("本机 Codex 进程已退出")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 30) -> dict:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        if method == 'turn/start':
            self.turn_start_request_id = request_id
        try:
            # The request deadline covers backpressure as well as the reply.
            async with asyncio.timeout(timeout):
                await self._send({"id": request_id, "method": method, "params": params})
                return await future
        except TimeoutError:
            raise CodexError(f"Codex 请求 {method} 长时间无响应，已保留会话，可点击继续重新连接") from None
        except (BrokenPipeError, ConnectionResetError):
            raise CodexError('本机 Codex 连接已断开，可点击继续重新连接') from None
        finally:
            self.pending.pop(request_id, None)
            # Writing may fail/cancel after the reader has settled this reply,
            # before wait_for ever observes it. Do not leak that pending/error state.
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
            if self.turn_start_request_id == request_id:
                self.turn_start_request_id = None

    def _remember_thread(self, thread: dict) -> None:
        # Only server metadata establishes ancestry; a caller-supplied parent,
        # shared cwd/session, or forkedFromId does not grant project access.
        parent = thread.get('parentThreadId')
        source = thread.get('source')
        if not parent and isinstance(source, dict):
            child = source.get('subAgent')
            if isinstance(child, dict):
                parent = child.get('thread_spawn', {}).get('parent_thread_id')
        if isinstance(thread.get('id'), str):
            self.thread_parents[thread['id']] = parent if isinstance(parent, str) else None
            if self.accepting_tools and thread['id'] != self.thread_id:
                self.seen_threads.add(thread['id'])

    async def _is_project_thread(self, thread_id: str | None) -> bool:
        seen = set()
        while thread_id and thread_id not in seen:
            if thread_id == self.thread_id:
                return True
            seen.add(thread_id)
            if thread_id not in self.thread_parents:
                # Resumed descendants need not emit thread/started again. This
                # runs in the tool task, never the protocol reader awaiting itself.
                result = await self.request('thread/read', {'threadId': thread_id, 'includeTurns': False})
                thread = result.get('thread', {})
                if thread.get('id') != thread_id:
                    return False
                self._remember_thread(thread)
            thread_id = self.thread_parents.get(thread_id)
        return False

    def _queue_usage(self, params: dict) -> None:
        thread_id, turn_id = params.get('threadId'), params.get('turnId')
        total = ((params.get('tokenUsage') or {}).get('total') or {}).get('totalTokens')
        if not thread_id or not isinstance(total, int):
            return
        previous = self.usage_totals.get(thread_id, 0)
        self.usage_totals[thread_id] = max(previous, total)
        # Resume replays the last finished turn's total. It establishes a
        # baseline, not new consumption. Duplicate/reordered totals add nothing.
        if (not self.accepting_tools or (thread_id, turn_id) not in self.usage_turns
                or total <= previous or not self.on_event):
            return
        preceding, callback = self.usage_tail, self.on_event

        async def deliver():
            if preceding:
                await preceding
            # Unknown descendants require metadata I/O outside the reader.
            if await self._is_project_thread(thread_id):
                await callback('thread/tokenUsage/updated', {**params, 'usageDelta': total - previous})

        self.usage_tail = asyncio.create_task(deliver())

    async def _flush_usage(self) -> None:
        while self.usage_tail:
            tail = self.usage_tail
            await asyncio.shield(tail)
            if tail is self.usage_tail:
                return

    async def _notification(self, method: str, params: dict) -> None:
        if method == 'thread/started':
            self._remember_thread(params.get('thread', {}))
            return
        thread_id = params.get('threadId')
        if method == 'thread/tokenUsage/updated':
            if (thread_id == self.thread_id and self.turn_id is None and self.turn_start_request_id is not None
                    and self.finished and not self.finished.done()):
                self.early_turn_events.append((method, params))
            else:
                self._queue_usage(params)
            return
        if self.accepting_tools and thread_id and thread_id != self.thread_id:
            self.seen_threads.add(thread_id)
            if method == 'turn/started' and params.get('turn', {}).get('id'):
                self.usage_turns.add((thread_id, params['turn']['id']))
            if method in {'turn/started', 'turn/completed', 'thread/status/changed', 'thread/closed'}:
                self.child_revision += 1
                self.child_changed.set()
        if method == 'serverRequest/resolved':
            if thread_id and params.get('requestId') is not None:
                await self._cancel_requests(thread_id=thread_id, request_id=params['requestId'], wait=False)
            return
        completes_root = (method == 'turn/completed' and thread_id == self.thread_id
                          and self.turn_id is not None and params.get('turn', {}).get('id') == self.turn_id)
        if completes_root:
            # The platform can still be running child work, but this native
            # turn can no longer receive steering. New messages stay queued.
            self.turn_id = None
        if method == 'turn/completed' and thread_id and params.get('turn', {}).get('id'):
            self.ended_turns.add((thread_id, params['turn']['id']))
            # A terminated child no longer waits for its tool result. Stop the
            # matching host call before hiding child events or ending the parent.
            if completes_root and params['turn'].get('status') != 'completed':
                # Abort the whole tree together. Waiting for the root tool's
                # cleanup first would let descendants keep writing meanwhile.
                self.accepting_tools = False
                await self._cancel_requests(wait=False)
            else:
                await self._cancel_requests(thread_id=thread_id, turn_id=params['turn']['id'], wait=False)
        if thread_id is not None and thread_id != self.thread_id:
            # Descendant messages and turn state cannot finish/steer the parent.
            return
        if completes_root:
            if self.finished and not self.finished.done():
                self.finished.set_result(params['turn'])
            return
        if method in {'turn/started', 'turn/completed'}:
            if thread_id != self.thread_id:
                return
            if self.turn_id is None:
                if self.turn_start_request_id is not None and self.finished and not self.finished.done():
                    self.early_turn_events.append((method, params))
                return
            if params.get('turn', {}).get('id') != self.turn_id:
                return
        if self.on_event:
            await self.on_event(method, params)

    async def _wait_for_children(self, timeout: float) -> None:
        # Read current runtime status without loading potentially huge histories.
        # Run outside the protocol reader so replies and child tools keep flowing.
        loop = asyncio.get_running_loop()
        while True:
            if self.cleanup_error:
                # The parent future may already be completed. Fail the drain
                # so turn() stops the other owned tools instead of waiting on them.
                raise CodexError(self.cleanup_error)
            if self.closing:
                raise CodexError('Codex 连接已结束，已保留会话和结果，可继续重新连接')
            self.child_changed.clear()
            revision, seen = self.child_revision, set(self.seen_threads)
            active = False
            for identity in seen:
                if not await self._is_project_thread(identity):
                    continue
                response = await self.request('thread/read', {'threadId': identity, 'includeTurns': False})
                thread = response.get('thread', {})
                status = thread.get('status', {}).get('type')
                if thread.get('id') != identity or status not in {'active', 'idle', 'notLoaded'}:
                    raise CodexError('无法确认子任务已结束，已保留现有结果，可继续检查')
                active |= status == 'active'
            if (not self.closing and not active and not self.requests and not self.steering
                    and seen == self.seen_threads and revision == self.child_revision):
                return
            remaining = timeout - (loop.time() - self.last_activity)
            if remaining <= 0:
                if not self.requests and not self.steering:
                    raise CodexError('子任务长时间无响应，已保留会话和结果，可继续重新连接')
                remaining = timeout
            try:
                await asyncio.wait_for(self.child_changed.wait(), min(1, remaining))
            except TimeoutError:
                pass

    def _request_finished(self, identity: int | str) -> None:
        self.requests.pop(identity, None)
        self.child_changed.set()

    async def _cancel_requests(self, *, thread_id: str | None = None,
                               turn_id: str | None = None, request_id: int | str | None = None,
                               wait: bool = True) -> None:
        tasks = [task for identity, (params, task) in self.requests.items()
                 if (thread_id is None or params.get('threadId') == thread_id)
                 and (turn_id is None or params.get('turnId') == turn_id)
                 and (request_id is None or identity == request_id)]
        for task in tasks:
            # Another stop/transport failure may reach the same tool while its
            # finally block is removing a container. Do not interrupt that cleanup.
            if not task.cancelling():
                task.cancel()
        # The reader must keep handling terminal notifications and RPC replies
        # while tools clean up. Requests remain tracked until done; turn draining
        # and transport close still wait for them before publishing a result.
        if tasks and wait:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        error: Exception = CodexError("本机 Codex 连接已断开，可点击继续重新连接")
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                # Status-read responses are observations, not agent progress.
                if 'method' in message:
                    self.last_activity = asyncio.get_running_loop().time()
                if "method" in message and "id" in message:
                    if self.closing:
                        await self._send({'id': message['id'], 'error': {
                            'code': -32000, 'message': '会话正在关闭，已停止接受新的工具调用',
                        }})
                        continue
                    task = asyncio.create_task(self._server_request(message))
                    self.requests[message['id']] = (message.get('params', {}), task)
                    task.add_done_callback(lambda _, identity=message['id']: self._request_finished(identity))
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CodexError(str(message["error"].get("message", "Codex 请求失败"))))
                        else:
                            if message['id'] == self.turn_start_request_id:
                                # Bind the authoritative parent turn before reading
                                # further notifications, including child starts.
                                self.turn_id = message['result']['turn']['id']
                                self.usage_turns.add((self.thread_id, self.turn_id))
                            future.set_result(message.get("result", {}))
                            if message['id'] == self.turn_start_request_id:
                                early, self.early_turn_events = self.early_turn_events, []
                                for method, params in early:
                                    await self._notification(method, params)
                else:
                    method, params = message.get("method", ""), message.get("params", {})
                    await self._notification(method, params)
        except (ValueError, OSError, RuntimeError) as cause:
            error = CodexError(f"Codex 协议连接失败：{cause}")
        finally:
            if not self.closing:
                # Keep the failure before waiting for tool cleanup: the owner
                # may receive a stop while this reader is still draining tools.
                self.transport_error = str(error)
            self.closing = True
            self.child_changed.set()
            await self._cancel_requests()
            for future in [*self.pending.values(), self.finished]:
                if future and not future.done():
                    future.set_exception(error)

    async def _server_request(self, message: dict) -> None:
        try:
            if message["method"] != "item/tool/call" or not self.on_tool:
                await self._send({"id": message["id"], "error": {
                    "code": -32601, "message": "此项目只开放 Lilies 项目工具；未授权其他操作",
                }})
                return
            params = message["params"]
            # Apply already received usage before allowing the next side effect.
            await self._flush_usage()
            if not self.accepting_tools or (params.get('threadId'), params.get('turnId')) in self.ended_turns:
                raise ValueError('本轮已结束，工具调用已停止')
            if not await self._is_project_thread(params.get('threadId')):
                raise ValueError("工具请求不属于当前项目会话")
            if params.get('threadId') != self.thread_id:
                self.seen_threads.add(params['threadId'])
                self.child_revision += 1
                self.child_changed.set()
            result = await self.on_tool(params["tool"], params["arguments"])
            response = {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as error:
            if asyncio.current_task().cancelling() or isinstance(error, SandboxCleanupError):
                # A cleanup failure is fatal to the owning run. The cancelled
                # model call may no longer consume this tool's error response.
                self.cleanup_error = self.cleanup_error or '工具清理失败：' + str(error)[:2000]
                self.accepting_tools = False
                # Completion may already be known while status/ancestry RPCs
                # are still pending. Wake those waiters to enter error cleanup.
                for future in [*self.pending.values(), self.finished]:
                    if future and not future.done():
                        future.set_exception(CodexError(self.cleanup_error))
            response = {"success": False, "contentItems": [{"type": "inputText", "text": str(error)}]}
        try:
            async with asyncio.timeout(TOOL_RESPONSE_TIMEOUT_SECONDS):
                await self._send({"id": message["id"], "result": response})
        except (CodexError, BrokenPipeError, ConnectionResetError, TimeoutError):
            # A finished calculation with an undeliverable result cannot keep
            # the run alive or turn into a successful completion. Stop this
            # transport without awaiting close() from one of its owned tools.
            self.closing = True
            self.accepting_tools = False
            self.child_changed.set()
            self.transport_error = self.transport_error or '工具结果未能发送，已保留已有文件，可继续重新连接'
            error = CodexError(self.transport_error)
            for future in [*self.pending.values(), self.finished]:
                if future and not future.done():
                    future.set_exception(error)
            if self.process and self.process.returncode is None:
                self.process.terminate()
        finally:
            self.last_activity = asyncio.get_running_loop().time()

    async def _close_after_turn(self) -> None:
        # Another stop must not cancel the gather that is waiting for tool
        # cleanup. Finish this close before publishing the original outcome.
        self.closing = True
        closing = asyncio.create_task(self.close())
        while True:
            try:
                await asyncio.shield(closing)
                return
            except asyncio.CancelledError:
                if closing.cancelled():
                    raise

    async def turn_prepared(self, prepare_message: Callable[[], str], on_event, on_tool, *, timeout: float = 900) -> dict:
        """Consume pending input only after any connection recovery succeeds."""
        return await self.turn(prepare_message, on_event, on_tool, timeout=timeout)

    async def turn(self, message: str | Callable[[], str], on_event, on_tool, *, timeout: float = 900) -> dict:
        """Wait for completion, timing out only when the agent stops responding.

        Project tools have their own execution limits. A pending host tool or
        continued protocol activity must not terminate productive building work.
        Explicit stop still cancels this coroutine and its owned tool calls.
        """
        if not self.allow_model_calls:
            raise CodexError('模型出口已关闭；请管理员在获准环境中启用后再运行')
        if self._start_context is not None and (
                self.closing or self.process is None or self.process.returncode is not None
                or self.process.stdin is None or self.process.stdin.is_closing()):
            # A live process can have a closed input pipe. Resume before sending
            # the new turn; never retry a turn whose delivery has already begun.
            self.on_event, self.on_tool = None, None
            try:
                await self.close()
                await self.start(*self._start_context, thread_id=self.thread_id)
            except (Exception, asyncio.CancelledError):
                await self._close_after_turn()
                raise
        self.on_event, self.on_tool = on_event, on_tool
        self.cleanup_error = ''
        self.transport_error = ''
        self.usage_turns.clear()
        self.accepting_tools = True
        self.seen_threads.clear()
        self.child_revision = 0
        loop = asyncio.get_running_loop()
        self.turn_id = None
        self.early_turn_events.clear()
        self.finished = loop.create_future()
        self.last_activity = loop.time()
        try:
            if callable(message):
                message = message()
            await self.request("turn/start", {
                "threadId": self.thread_id, "input": [{"type": "text", "text": message}],
                "environments": [],
                **({"effort": "none" if self.thinking == "off" else self.thinking}
                   if self.thinking != "default" else {}),
            })
            while not self.finished.done():
                remaining = timeout - (loop.time() - self.last_activity)
                if remaining <= 0:
                    if not self.requests:
                        raise CodexError("Codex 长时间无响应，已保留会话和工作进度，可点击继续重新连接")
                    remaining = timeout
                # asyncio.wait leaves the completion future intact between
                # idle checks, unlike wait_for which cancels it on timeout.
                await asyncio.wait({self.finished}, timeout=remaining)
            result = self.finished.result()
            if result.get('status') == 'completed':
                # A steer may lose the race with completion. Let its caller
                # record delivery while draining children, so a cleanup failure
                # can still stop owned tools before that acknowledgement arrives.
                await self._wait_for_children(timeout)
                await self._flush_usage()
            else:
                await self._close_after_turn()
            if self.transport_error or self.cleanup_error:
                original = (result.get('error') or {}).get('message', '')
                raise CodexError('；'.join(value for value in (original,self.transport_error,self.cleanup_error) if value))
            if self.on_event:
                await self.on_event('turn/completed', {'threadId': self.thread_id, 'turn': result})
            return result
        except (Exception, asyncio.CancelledError) as cause:
            # A timed-out/cancelled caller must not leave this transport accepting
            # late tool calls. Keep the thread files/id for an explicit reconnect.
            native_error = ''
            if (self.finished.done() and not self.finished.cancelled()
                    and self.finished.exception() is None):
                # The reader can receive a failure before this coroutine resumes.
                # A later stop must not replace that failure with cancellation.
                terminal = self.finished.result()
                if terminal.get('status') == 'failed':
                    native_error = (terminal.get('error') or {}).get('message') or '模型会话执行失败'
            if not self.finished.done():
                self.finished.cancel()
            await self._close_after_turn()
            details = [value for value in (native_error,self.transport_error,self.cleanup_error)
                       if value and value not in str(cause)]
            if details:
                raise CodexError('；'.join(value for value in (str(cause),*details) if value)) from cause
            raise
        finally:
            self.accepting_tools = False
            self.turn_id = None
            if not self.finished.done():
                self.finished.cancel()
            elif not self.finished.cancelled():
                # Startup can fail before this completion future is awaited.
                # Consume its duplicate error after reporting the turn failure.
                self.finished.exception()

    async def steer(self, message: str) -> bool:
        if not self.turn_id:
            raise CodexError("Agent 正在连接，请稍后发送")
        settled = asyncio.get_running_loop().create_future()
        self.steering.add(settled)
        try:
            await self.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                            "input": [{"type": "text", "text": message}]})
            return True
        except CodexError as error:
            # Only this explicit rejection establishes non-delivery. Never
            # replay on a timeout/disconnect or hide an unrelated rejection.
            if str(error).strip().casefold() == 'no active turn to steer':
                return False
            raise
        finally:
            settled.set_result(None)
            self.steering.discard(settled)
            self.child_changed.set()

    async def interrupt(self) -> None:
        if self.turn_id and self.thread_id:
            await self.request('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id}, timeout=5)

    async def close(self) -> None:
        self.closing = True
        await self._cancel_requests()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
        await asyncio.gather(*(x for x in (self.reader, self.stderr_reader) if x), return_exceptions=True)
        # Closing can cancel the reader while its finally block is joining the
        # same tools. Settle replies here too, even if that finalizer was cut short.
        for future in [*self.pending.values(), self.finished]:
            if future and not future.done():
                future.set_exception(CodexError('本机 Codex 连接已断开，可点击继续重新连接'))
        if self.usage_tail:
            await asyncio.gather(self.usage_tail, return_exceptions=True)
            self.usage_tail = None

    async def _drain_stderr(self) -> None:
        # Codex may log account/request metadata. Do not forward raw stderr to the UI.
        assert self.process and self.process.stderr
        while await self.process.stderr.read(65536):
            pass


def validate_codex_version(version: str) -> None:
    match = re.match(r"codex-cli (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (0, 153, 0):
        raise ValueError("需要 Codex CLI 0.153.0 或更新版本，以支持关闭原生环境访问的项目会话")
