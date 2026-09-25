"""Local Codex adapter. No provider calls, terminal scraping, or shared agent state."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable


class CodexError(RuntimeError):
    pass


async def inspect_executable(path: str) -> dict[str, Any]:
    executable = shutil.which(path) if not Path(path).is_absolute() else path
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ValueError("没有找到可执行程序，请指定本机 Agent 的完整路径")
    process = await asyncio.create_subprocess_exec(
        executable, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), 10)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError("读取 Agent 版本超时") from None
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
                 auth_file: Path | None = None, subscription_only: bool = False, allow_model_calls: bool = True) -> None:
        self.allow_model_calls = allow_model_calls
        self.auth_file = auth_file
        self.subscription_only = subscription_only
        self.executable = executable
        self.runtime_dir = runtime_dir
        self.model = model
        self.thinking = thinking
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self.reader: asyncio.Task | None = None
        self.stderr_reader: asyncio.Task | None = None
        self.requests: set[asyncio.Task] = set()
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.finished: asyncio.Future | None = None
        self.on_event: Callable[[str, dict], Awaitable[None]] | None = None
        self.on_tool: Callable[[str, dict], Awaitable[Any]] | None = None
        self.last_activity = 0.0

    async def connect(self) -> None:
        """Initialize transport without creating a thread or spending model tokens."""
        if self.process and self.process.returncode is None:
            return
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
        argv = [self.executable, "app-server", "--stdio"]
        for key, value in config.items():
            argv += ["-c", f"{key}={json.dumps(value)}"]
        self.process = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        self.stderr_reader = asyncio.create_task(self._drain_stderr())
        await self.request("initialize", {
            "clientInfo": {"name": "lilies", "title": "Lilies Workflow Studio", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self._send({"method": "initialized"})

    async def start(self, tools: list[dict], instructions: str, thread_id: str | None = None) -> str:
        await self.connect()
        if self.subscription_only:
            account = (await self.request('account/read', {'refreshToken': False})).get('account')
            if not account or account.get('type') != 'chatgpt':
                raise CodexError('官方智能体需要订阅账号登录，不能使用 API Key')
        cwd = self.runtime_dir / 'empty-workspace'
        codex_home = self.runtime_dir / 'codex-home'
        config = self.config
        params = {"cwd": str(cwd.resolve()), "approvalPolicy": "never", "sandbox": "read-only",
                  "baseInstructions": instructions, "developerInstructions": instructions,
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
                    'description': 'Additional project tools: independent modeling and project progress.',
                    'tools': deferred})
            result = await self.request("thread/start", {
                **params, "environments": [], "selectedCapabilityRoots": [],
                "dynamicTools": dynamic_tools, "allowProviderModelFallback": False,
            })
        if result.get("instructionSources"):
            raise CodexError("Codex 加载了项目外指令，已停止接入")
        self.thread_id = result["thread"]["id"]
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
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise CodexError(f"Codex 请求 {method} 长时间无响应，已保留会话，可点击继续重新连接") from None
        finally:
            self.pending.pop(request_id, None)

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        error: Exception = CodexError("本机 Codex 连接已断开，可点击继续重新连接")
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                self.last_activity = asyncio.get_running_loop().time()
                if "method" in message and "id" in message:
                    task = asyncio.create_task(self._server_request(message))
                    self.requests.add(task)
                    task.add_done_callback(self.requests.discard)
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CodexError(str(message["error"].get("message", "Codex 请求失败"))))
                        else:
                            future.set_result(message.get("result", {}))
                else:
                    method, params = message.get("method", ""), message.get("params", {})
                    if method == "turn/started":
                        self.turn_id = params["turn"]["id"]
                    if self.on_event:
                        await self.on_event(method, params)
                    if method == "turn/completed" and self.finished and not self.finished.done():
                        self.finished.set_result(params["turn"])
        except (ValueError, OSError, RuntimeError) as cause:
            error = CodexError(f"Codex 协议连接失败：{cause}")
        finally:
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
            if params.get("threadId") != self.thread_id:
                raise ValueError("工具请求不属于当前项目会话")
            result = await self.on_tool(params["tool"], params["arguments"])
            response = {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as error:
            response = {"success": False, "contentItems": [{"type": "inputText", "text": str(error)}]}
        try:
            await self._send({"id": message["id"], "result": response})
        except (CodexError, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.last_activity = asyncio.get_running_loop().time()

    async def turn(self, message: str, on_event, on_tool, *, timeout: float = 900) -> dict:
        """Wait for completion, timing out only when the agent stops responding.

        Project tools have their own execution limits. A pending host tool or
        continued protocol activity must not terminate productive building work.
        Explicit stop still cancels this coroutine and its owned tool calls.
        """
        if not self.allow_model_calls:
            raise CodexError('模型出口已关闭；请管理员在获准环境中启用后再运行')
        self.on_event, self.on_tool = on_event, on_tool
        loop = asyncio.get_running_loop()
        self.finished = loop.create_future()
        self.last_activity = loop.time()
        try:
            result = await self.request("turn/start", {
                "threadId": self.thread_id, "input": [{"type": "text", "text": message}],
                "environments": [],
                **({"effort": "none" if self.thinking == "off" else self.thinking}
                   if self.thinking != "default" else {}),
            })
            self.turn_id = result["turn"]["id"]
            while not self.finished.done():
                remaining = timeout - (loop.time() - self.last_activity)
                if remaining <= 0:
                    if not self.requests:
                        raise CodexError("Codex 长时间无响应，已保留会话和工作进度，可点击继续重新连接")
                    remaining = timeout
                # asyncio.wait leaves the completion future intact between
                # idle checks, unlike wait_for which cancels it on timeout.
                await asyncio.wait({self.finished}, timeout=remaining)
            return self.finished.result()
        finally:
            self.turn_id = None
            if not self.finished.done():
                self.finished.cancel()

    async def steer(self, message: str) -> None:
        if not self.turn_id:
            raise CodexError("Agent 正在连接，请稍后发送")
        await self.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                        "input": [{"type": "text", "text": message}]})

    async def interrupt(self) -> None:
        if self.turn_id and self.thread_id:
            await self.request('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id}, timeout=5)

    async def close(self) -> None:
        for task in self.requests:
            task.cancel()
        if self.requests:
            await asyncio.gather(*self.requests, return_exceptions=True)
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

    async def _drain_stderr(self) -> None:
        # Codex may log account/request metadata. Do not forward raw stderr to the UI.
        assert self.process and self.process.stderr
        while await self.process.stderr.read(65536):
            pass


def validate_codex_version(version: str) -> None:
    match = re.match(r"codex-cli (\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (0, 153, 0):
        raise ValueError("需要 Codex CLI 0.153.0 或更新版本，以支持关闭原生环境访问的项目会话")
